terraform {
  required_version = ">= 1.10.0, < 2.0.0"
  backend "s3" {}
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.69.1"
    }
  }
}
provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = ["123456789012"]
}

# 기존 테스트 디렉터리의 state, IAM 정책, 보안 그룹을 가져오지 않습니다.
# 공유 자원은 ID로 참조만 하며 자동화 전용 EC2 하나만 관리합니다.
resource "aws_instance" "replacement" {
  ami                         = "ami-00000000000000cac"
  instance_type               = "t3.micro"
  subnet_id                   = "subnet-0000000000000960f"
  vpc_security_group_ids      = ["sg-000000000000039bc", "sg-0000000000000bd52"]
  associate_public_ip_address = true
  key_name                    = "team5-key"
  iam_instance_profile        = "team5-bastion-role"
  user_data                   = join("\n", [file("${path.module}/bootstrap.sh"), file("${path.module}/bootstrap-monitoring.sh")])
  user_data_replace_on_change = true
  root_block_device {
    volume_size           = 8
    volume_type           = "gp3"
    iops                  = 3000
    throughput            = 125
    encrypted             = false
    delete_on_termination = true
  }
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
    http_protocol_ipv6          = "disabled"
    instance_metadata_tags      = "disabled"
  }
  tags = {
    Name       = "team5-bastion-auto-recovery"
    Project    = "team5"
    Role       = "Bastion"
    ManagedBy  = "team5-bastion-integration"
    Monitoring = "team5-bastion"
  }
  lifecycle {
    prevent_destroy = true
  }
}
output "instance_id" { value = aws_instance.replacement.id }
output "private_ip" { value = aws_instance.replacement.private_ip }
output "public_ip" { value = aws_instance.replacement.public_ip }
