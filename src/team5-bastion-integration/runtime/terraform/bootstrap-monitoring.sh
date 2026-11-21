#!/bin/bash
set -euo pipefail
test "$(id -u)" -eq 0
test "$(uname -m)" = x86_64

dnf install -y rsyslog amazon-cloudwatch-agent logrotate tar gzip

# Send SSH authentication records to the authpriv facility.
printf 'SyslogFacility AUTHPRIV\nLogLevel INFO\n' > /etc/ssh/sshd_config.d/00-00-team5-auth.conf
/usr/sbin/sshd -t
systemctl reload sshd

# AL2023's packaged rsyslog configuration normally already routes authpriv.
# Add a rule only if no active rule writes /var/log/secure.
python3 <<'PY'
from pathlib import Path
files = [Path('/etc/rsyslog.conf'), *Path('/etc/rsyslog.d').glob('*.conf')]
rules = '\n'.join(line for p in files if p.exists()
                  for line in p.read_text().splitlines()
                  if not line.lstrip().startswith('#'))
if '/var/log/secure' not in rules:
    Path('/etc/rsyslog.d/30-team5-auth.conf').write_text('authpriv.* /var/log/secure\n')
PY
touch /var/log/secure
chown root:root /var/log/secure
chmod 0600 /var/log/secure
rsyslogd -N1
systemctl enable rsyslog
systemctl restart rsyslog

# Use the package log rotation rule if it already covers this file.
if ! grep -Rqs '/var/log/secure' /etc/logrotate.d; then
  cat > /etc/logrotate.d/team5-auth <<'ROTATE'
/var/log/secure {
  daily
  rotate 7
  missingok
  notifempty
  compress
  delaycompress
  create 0600 root root
  sharedscripts
  postrotate
    /usr/bin/systemctl kill -s HUP rsyslog.service >/dev/null 2>&1 || true
  endscript
}
ROTATE
fi

mkdir -p /opt/aws/amazon-cloudwatch-agent/etc
cat > /opt/aws/amazon-cloudwatch-agent/etc/team5-auth.json <<'JSON'
{
  "agent": {"region": "ap-northeast-2", "run_as_user": "root"},
  "logs": {
    "force_flush_interval": 5,
    "logs_collected": {"files": {"collect_list": [{
      "file_path": "/var/log/secure",
      "log_group_name": "/project/security/bastion-auth",
      "log_stream_name": "{instance_id}/secure"
    }]}}
  }
}
JSON
chmod 0600 /opt/aws/amazon-cloudwatch-agent/etc/team5-auth.json
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/team5-auth.json -s
systemctl enable amazon-cloudwatch-agent

# Pin exporter version and verify the archive against the release checksum.
node_version=1.9.1
archive="node_exporter-${node_version}.linux-amd64.tar.gz"
download_dir=$(mktemp -d /tmp/team5-node-exporter.XXXXXX)
(
  cd "$download_dir"
  base="https://github.com/prometheus/node_exporter/releases/download/v${node_version}"
  curl -fSL --retry 3 --connect-timeout 15 --max-time 180 -O "$base/$archive"
  curl -fSL --retry 3 --connect-timeout 15 --max-time 60 -O "$base/sha256sums.txt"
  awk -v file="$archive" '$2 == file {print}' sha256sums.txt > selected.sha256
  test "$(wc -l < selected.sha256)" -eq 1
  sha256sum -c selected.sha256
  tar -xzf "$archive"
  install -m 0755 "node_exporter-${node_version}.linux-amd64/node_exporter" /usr/local/bin/node_exporter
)
getent group node_exporter >/dev/null || groupadd --system node_exporter
id node_exporter >/dev/null 2>&1 || useradd --system --gid node_exporter --no-create-home --shell /sbin/nologin node_exporter
cat > /etc/systemd/system/node_exporter.service <<'UNIT'
[Unit]
Description=Prometheus Node Exporter
Wants=network-online.target
After=network-online.target

[Service]
User=node_exporter
Group=node_exporter
ExecStart=/usr/local/bin/node_exporter --web.listen-address=:9100
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable node_exporter
systemctl restart node_exporter
for attempt in $(seq 1 12); do
  if curl -fsS --max-time 5 http://127.0.0.1:9100/metrics -o /tmp/team5-node-metrics.txt; then
    break
  fi
  sleep 2
done
grep -q '^node_exporter_build_info' /tmp/team5-node-metrics.txt

probe="TEAM5_AUTH_PIPELINE_TEST_$(date -u +%Y%m%dT%H%M%SZ)"
logger -p authpriv.notice -t team5-bastion-test "$probe"
for attempt in $(seq 1 15); do
  if grep -Fq "$probe" /var/log/secure; then break; fi
  sleep 2
done
grep -F "$probe" /var/log/secure
systemctl is-active --quiet rsyslog amazon-cloudwatch-agent node_exporter
echo 'BASTION_MONITORING_BOOTSTRAP_OK'
echo 'CloudWatch delivery and Prometheus up=1 must be checked separately.'
