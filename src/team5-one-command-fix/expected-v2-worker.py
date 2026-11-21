"""Configure only the selected recovery web EC2, then verify actual WordPress DB use."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

PROXY = '192.0.2.242'
BASE = Path('/home/ec2-user/wp')
CONTAINER = 'wp-wordpress-1'


def run(args, input=None, timeout=60):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Do not expose .env/wp-config.php contents or DB errors with credentials.
        raise RuntimeError('Command failed: ' + args[0])
    return result.stdout


def env_text(text):
    if len(re.findall(r'^DB_HOST=.*$', text, re.M)) != 1:
        raise RuntimeError('Expected exactly one DB_HOST in .env')
    return re.sub(r'^DB_HOST=.*$', 'DB_HOST=' + PROXY, text, flags=re.M)


CONFIGURE_PHP = r'''<?php
$path = '/var/www/html/wp-config.php';
$text = file_get_contents($path);
$pattern = '/^\h*define\s*\(\s*([\x27\x22])DB_HOST\1\s*,[^\r\n]*\)\s*;\h*$/m';
if (preg_match_all($pattern, $text) !== 1) { exit(21); }
$new = preg_replace_callback($pattern, function($m) {
    return "define( 'DB_HOST', getenv('WORDPRESS_DB_HOST') );";
}, $text);
if ($new !== $text) {
    $private = '/var/lib/team5-proxy-recovery';
    if (!is_dir($private) && !mkdir($private, 0700, true)) { exit(22); }
    $backup = $private . '/wp-config-before-' . bin2hex(random_bytes(8));
    if (!copy($path, $backup) || !chmod($backup, 0600)) { exit(22); }
    $tmp = $path . '.proxy-tmp-' . bin2hex(random_bytes(8));
    if (file_put_contents($tmp, $new) === false) { exit(23); }
    chmod($tmp, fileperms($path) & 0777);
    chown($tmp, fileowner($path)); chgrp($tmp, filegroup($path));
    if (!rename($tmp, $path)) { unlink($tmp); exit(24); }
}
echo "WORDPRESS_DB_HOST_SOURCE_UPDATED\n";
'''

VERIFY_PHP = r'''<?php
error_reporting(0);
ini_set('display_errors', '0');
define('SHORTINIT', true);
define('DISABLE_WP_CRON', true);
$_SERVER['HTTP_HOST'] = 'project-alb-148826606.ap-northeast-2.elb.amazonaws.com';
require '/var/www/html/wp-load.php';
if (!defined('DB_HOST') || DB_HOST !== '192.0.2.242:3306') { exit(31); }
global $wpdb;
$wpdb->suppress_errors(true);
$nonce = bin2hex(random_bytes(8));
$actual = $wpdb->get_var('SELECT @@server_uuid /* team5_' . $nonce . ' */');
if (!$actual) { exit(32); }
echo 'WORDPRESS_PROXY_SQL_OK ' . json_encode(['DB_HOST'=>DB_HOST, 'ObservedServerUuid'=>$actual]) . "\n";
'''


def main():
    os.umask(0o077)
    mode = os.environ['SSM_Mode']
    expected = os.environ['SSM_RestoreDbHost']
    if mode not in ('prepare', 'verify'):
        raise RuntimeError('Invalid mode')
    if not re.fullmatch(r'team5-rds-(mysql|restore-[a-z0-9-]+)\.[a-z0-9]+\.ap-northeast-2\.rds\.amazonaws\.com', expected):
        raise RuntimeError('Unexpected RDS hostname')
    if not BASE.is_dir() or not (BASE / 'docker-compose.yml').is_file():
        raise RuntimeError('Unexpected WordPress layout')
    compose = ['docker', 'compose', '--project-directory', str(BASE), '-f', str(BASE / 'docker-compose.yml')]
    if mode == 'prepare':
        env = BASE / '.env'
        text = env.read_text()
        changed = env_text(text)
        run(compose + ['config', '--quiet'])
        # Prevent recreating a container whose wp-config.php would disappear.
        mounts = json.loads(run(['docker', 'inspect', CONTAINER]))[0]['Mounts']
        if not any('/var/www/html/wp-config.php' == m['Destination'] or
                   '/var/www/html/wp-config.php'.startswith(m['Destination'].rstrip('/') + '/')
                   for m in mounts):
            raise RuntimeError('WordPress configuration is not on a persistent mount')
        # Check supported define syntax before changing the environment.
        current = run(['docker', 'exec', CONTAINER, 'cat', '/var/www/html/wp-config.php'])
        if len(re.findall(r'^\s*define\s*\(\s*[\'\"]DB_HOST[\'\"]\s*,[^\r\n]*\)\s*;\s*$', current, re.M)) != 1:
            raise RuntimeError('Unsupported DB_HOST definition; manual inspection required')
        backups = BASE / '.team5-proxy-backups'
        backups.mkdir(mode=0o700, exist_ok=True)
        os.chmod(backups, 0o700)
        backup_config = backups / ('wp-config-' + str(time.time_ns()) + '.php')
        backup_config.write_text(current)
        os.chmod(backup_config, 0o600)
        if changed != text:
            backup = BASE / ('.env.before-proxy-' + str(time.time_ns()))
            shutil.copy2(env, backup)
            os.chmod(backup, 0o600)
            # Keep existing ownership and mode.
            env.write_text(changed)
        run(compose + ['config', '--quiet'])
        run(compose + ['up', '-d', '--no-deps', 'wordpress'], timeout=180)
        actual = run(['docker', 'exec', CONTAINER, 'printenv', 'WORDPRESS_DB_HOST']).strip()
        if actual != PROXY + ':3306':
            raise RuntimeError('Compose must map DB_HOST to WORDPRESS_DB_HOST with port 3306')
        run(['docker', 'exec', '-i', CONTAINER, 'php'], input=CONFIGURE_PHP)
        run(['docker', 'exec', CONTAINER, 'php', '-l', '/var/www/html/wp-config.php'])
        # Restart PHP/Apache so an old OPcache value cannot survive the update.
        run(compose + ['restart', 'wordpress'], timeout=90)
        print('WEB_PROXY_CONFIGURATION_READY')
        return
    for attempt in range(12):
        try:
            output = run(['docker', 'exec', '-i', '-e', 'TEAM5_EXPECTED_RDS=' + expected,
                          CONTAINER, 'php'], input=VERIFY_PHP, timeout=20)
            if not output.startswith('WORDPRESS_PROXY_SQL_OK '):
                raise RuntimeError('WordPress validation marker missing')
            print(output.strip())
            break
        except (RuntimeError, subprocess.TimeoutExpired):
            if attempt == 11:
                raise RuntimeError('WordPress SQL via proxy failed; no direct RDS connection is required')
            time.sleep(5)
    hostname = 'project-alb-148826606.ap-northeast-2.elb.amazonaws.com'
    for attempt in range(6):
        try:
            code = run(['curl', '-sS', '--max-time', '10', '--noproxy', '*', '--resolve',
                        hostname + ':80:127.0.0.1', '-o', '/dev/null', '-w', '%{http_code}',
                        'http://' + hostname + '/'], timeout=15)
        except (RuntimeError, subprocess.TimeoutExpired):
            code = '000'
        if code == '200':
            print('HTTP_CHECK_OK\nRECOVERY_CONFIG_OK')
            return
        time.sleep(5)
    raise RuntimeError('Local WordPress HTTP verification failed')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('WEB_ACTION_FAILED:', str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
        raise SystemExit(1)
