#!/bin/bash
set -euo pipefail

# AMI 기본 사용자의 그룹 및 관리자 권한 구성
id ec2-user
usermod -aG adm,wheel,systemd-journal ec2-user

printf 'ec2-user ALL=(ALL) NOPASSWD: ALL\n' \
  > /etc/sudoers.d/90-team5-ec2-user
chmod 0440 /etc/sudoers.d/90-team5-ec2-user
visudo -cf /etc/sudoers.d/90-team5-ec2-user

# EC2 키 페어의 공개키는 시작 과정에서 기본 계정에 설정된다.
install -d -m 0700 -o ec2-user -g ec2-user /home/ec2-user/.ssh

if [ -f /home/ec2-user/.ssh/authorized_keys ]; then
  chown ec2-user:ec2-user /home/ec2-user/.ssh/authorized_keys
  chmod 0600 /home/ec2-user/.ssh/authorized_keys
fi

# 기존 서버에서 확인한 SSH 인증 설정
cat > /etc/ssh/sshd_config.d/00-team5-bastion.conf <<'SSH'
Port 22
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PasswordAuthentication no
AuthorizedKeysFile .ssh/authorized_keys
SSH

/usr/sbin/sshd -t
systemctl reload sshd

# 동일 AMI에 포함된 프로그램과 SSM Agent 확인
command -v aws
command -v python3
systemctl enable --now amazon-ssm-agent
systemctl is-active --quiet amazon-ssm-agent

echo "BASTION_BOOTSTRAP_OK"
