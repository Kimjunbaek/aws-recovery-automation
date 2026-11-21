"""Executed by SSM as root on the designated ProxySQL EC2. No boto3 dependency."""
import fcntl
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path('/var/lib/team5-proxy-recovery')
PARAMETER = '/team5/recovery/proxysql/admin'
HOST = re.compile(r'^team5-rds-(?:mysql|restore-[a-z0-9-]+)\.[a-z0-9]+\.ap-northeast-2\.rds\.amazonaws\.com$')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def atomic(path, value):
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def check_rows(rows):
    require(len(rows) == 1, 'Expected exactly one ProxySQL backend; manual review required')
    row = rows[0]
    require(row['hostgroup_id'] == '1' and row['port'] == '3306', 'Unexpected backend group or port')
    require(HOST.fullmatch(row['hostname']) is not None, 'Unexpected backend hostname')
    require(row['status'] == 'ONLINE', 'Backend is not ONLINE')
    return row


def main():
    with ExitStack() as stack:
        execute(stack)


def execute(stack):
    os.umask(0o077)
    require(os.geteuid() == 0, 'Run through SSM as root')
    action = os.environ['SSM_Action']
    owner = os.environ['SSM_Owner']
    target = os.environ.get('SSM_TargetHost', '')
    require(action in ('inspect', 'switch', 'restore', 'resume', 'release'), 'Invalid action')
    require(owner.startswith('arn:aws:states:ap-northeast-2:123456789012:execution:team5-recovery-workflow:'), 'Unexpected execution owner')
    require(shutil.which('aws') and shutil.which('mysql'), 'aws and mysql CLI are required on proxy EC2')
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ROOT, 0o700)
    lock = stack.enter_context(open(ROOT / 'operation.lock', 'a'))
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    active_path = ROOT / 'active.json'
    record_path = ROOT / (hashlib.sha256(owner.encode()).hexdigest() + '.json')
    # stdout/stderr from secret retrieval and mysql never appear in SSM output.
    response = subprocess.run([
        'aws', 'ssm', 'get-parameter', '--name', PARAMETER, '--with-decryption',
        '--region', 'ap-northeast-2', '--output', 'json', '--no-cli-pager'
    ], capture_output=True, text=True, timeout=30)
    require(response.returncode == 0, 'Credential retrieval failed; check proxy IAM/KMS/network')
    secret = json.loads(json.loads(response.stdout)['Parameter']['Value'])
    require(secret.get('username') == 'radmin', 'Unexpected ProxySQL admin username')
    require(isinstance(secret.get('password'), str) and secret['password'], 'Missing admin password')
    with tempfile.TemporaryDirectory(prefix='auth-', dir=ROOT) as directory:
        defaults = Path(directory) / 'mysql.cnf'
        # MySQL option-file quoting. No password in argv, environment or logs.
        password = secret['password'].replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
        defaults.write_text('[client]\nuser=radmin\npassword="' + password + '"\nhost=127.0.0.1\nport=6032\nprotocol=TCP\n', encoding='utf-8')
        os.chmod(defaults, 0o600)

        def sql(query):
            process = subprocess.run([
                'mysql', '--defaults-file=' + str(defaults), '--batch', '--raw',
                '--connect-timeout=5', '-e', query
            ], capture_output=True, text=True, timeout=45)
            require(process.returncode == 0, 'ProxySQL SQL failed; check administrator write access/configuration')
            lines = process.stdout.strip().splitlines()
            if not lines:
                return []
            columns = lines[0].split('\t')
            return [dict(zip(columns, line.split('\t'))) for line in lines[1:]]

        columns = 'hostgroup_id,hostname,port,status,weight,compression,max_connections,max_replication_lag,use_ssl,max_latency_ms'

        def rows(table):
            return sql('SELECT ' + columns + ' FROM ' + table + ' ORDER BY hostgroup_id,hostname,port;')

        def snapshot():
            result = rows('mysql_servers')
            check_rows(result)
            require(result == rows('runtime_mysql_servers') == rows('disk.mysql_servers'), 'Memory/runtime/disk backends differ; resolve drift first')
            return result

        def apply_host(expected_hosts, desired, original):
            require(HOST.fullmatch(desired) is not None, 'Invalid destination')
            # Refuse overwriting another operator's topology or unrelated fields.
            for table in ('mysql_servers', 'runtime_mysql_servers', 'disk.mysql_servers'):
                current = rows(table)
                require(len(current) == 1, 'Backend topology changed; manual review required')
                row = current[0]
                require(row['hostname'] in expected_hosts, 'Backend changed by another operation')
                require(row['status'] in ('ONLINE', 'OFFLINE_HARD'), 'Unexpected backend status')
                require(all(row[k] == original[k] for k in original if k not in ('hostname', 'status')), 'Backend options changed by another operation')
            # Replacing the sole hostname removes the old backend at runtime.
            sql("UPDATE mysql_servers SET hostname='" + desired + "',status='ONLINE' WHERE hostgroup_id=1; LOAD MYSQL SERVERS TO RUNTIME; SAVE MYSQL SERVERS TO DISK;")
            current = snapshot()
            require(current[0]['hostname'] == desired, 'Proxy destination verification failed')

        active = json.loads(active_path.read_text()) if active_path.exists() else None
        record = json.loads(record_path.read_text()) if record_path.exists() else None
        if action == 'inspect':
            current = snapshot()
            require(active is None, 'A proxy recovery transaction is still active; rollback/release it first')
            # Other groups or query cache could invalidate the simple recovery check.
            require(not sql('SELECT rule_id FROM runtime_mysql_query_rules WHERE active=1 AND (cache_ttl>0 OR (destination_hostgroup IS NOT NULL AND destination_hostgroup<>1));'), 'Unsupported query routing/cache rules')
            require(not sql('SELECT destination_hostgroup FROM runtime_mysql_query_rules_fast_routing WHERE destination_hostgroup<>1;'), 'Unsupported fast routing')
            require(not sql('SELECT writer_hostgroup FROM runtime_mysql_replication_hostgroups;'), 'Replication hostgroup automation requires separate handling')
            require(not sql('SELECT hostname FROM runtime_proxysql_servers;'), 'ProxySQL cluster requires separate handling')
            require(not sql('SELECT id FROM scheduler WHERE active=1;'), 'Active ProxySQL scheduler requires inspection')
            print(json.dumps({'ProxyHost': '192.0.2.242', 'Backend': current[0]['hostname'], 'Status': 'PROXY_INSPECT_OK'}))
            return
        if action == 'switch':
            require(HOST.fullmatch(target) is not None and target.startswith('team5-rds-restore-'), 'Switch requires a restored RDS hostname')
            if active:
                require(active['owner'] == owner and record is not None, 'Another recovery owns the proxy')
                require(record['target'] == target and record['phase'] in ('switching', 'switched'), 'Transaction cannot be replayed')
            else:
                require(record is None, 'This execution has already been rolled back/released; start a new execution')
                current = snapshot()
                require(current[0]['hostname'] != target, 'Target already active without an owned snapshot')
                record = {'owner': owner, 'before': current, 'target': target, 'phase': 'switching'}
                atomic(record_path, record)
                atomic(active_path, {'owner': owner})
            original = record['before'][0]
            try:
                apply_host({original['hostname'], target}, target, original)
                record['phase'] = 'switched'
                atomic(record_path, record)
            except Exception:
                # Preserve the active lock even after compensation for operator review.
                apply_host({original['hostname'], target}, original['hostname'], original)
                record['phase'] = 'restored'
                atomic(record_path, record)
                raise
        elif action in ('restore', 'resume'):
            if record is None and active is None and action == 'restore':
                print('PROXY_NOT_CHANGED')
                return
            require(record is not None and active is not None and active['owner'] == owner, 'No owned active transaction')
            original = record['before'][0]
            desired = original['hostname'] if action == 'restore' else record['target']
            apply_host({original['hostname'], record['target']}, desired, original)
            record['phase'] = 'restored' if action == 'restore' else 'switched'
            atomic(record_path, record)
        elif action == 'release':
            require(record is not None and active is not None and active['owner'] == owner, 'No owned active transaction')
            require(record['phase'] == 'restored', 'Only a restored transaction can be released')
            require(snapshot() == record['before'], 'Proxy no longer matches its original backend')
            active_path.unlink()
            record['phase'] = 'released'
            atomic(record_path, record)
        print(json.dumps({'Action': action, 'BeforeHost': record['before'][0]['hostname'], 'TargetHost': record['target'], 'Phase': record['phase']}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Exception text is controlled; subprocess output and credentials are suppressed.
        print('PROXY_ACTION_FAILED:', str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
        raise SystemExit(1)
