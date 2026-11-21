"""CodeBuild에서만 실행. PREPARE는 계획만 저장, APPLY는 신규 배스천 생성/검증."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import boto3
from botocore.exceptions import ClientError

REGION = "ap-northeast-2"
ACCOUNT = "123456789012"
OLD = "i-0000000000000c871"
WEB = "i-000000000000071f8"
NORMAL_SG = "sg-000000000000039bc"
METRICS_SG = "sg-0000000000000bd52"
SUBNET = "subnet-0000000000000960f"
VPC = "vpc-000000000000021c2"
AMI = "ami-00000000000000cac"
LOG_GROUP = "/project/security/bastion-auth"
ROOT = Path(__file__).resolve().parent
TF = ROOT / "terraform"


def check_plan(plan):
    changes = [c for c in plan.get("resource_changes", []) if c.get("mode") == "managed"]
    if len(changes) != 1 or changes[0]["address"] != "aws_instance.replacement":
        raise RuntimeError("예상하지 않은 Terraform 관리 리소스입니다.")
    if changes[0]["change"]["actions"] != ["create"]:
        raise RuntimeError("새 배스천 1대 생성 계획만 허용합니다. 기존 자동화 배스천을 먼저 확인하세요.")
    after = changes[0]["change"]["after"]
    if after["ami"] != AMI or after["subnet_id"] != SUBNET:
        raise RuntimeError("AMI 또는 서브넷이 승인한 구성과 다릅니다.")
    if set(after["vpc_security_group_ids"]) != {NORMAL_SG, METRICS_SG}:
        raise RuntimeError("새 배스천의 보안 그룹이 예상과 다릅니다.")


def run(*args, capture=False):
    return subprocess.run(args, cwd=TF, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def main():
    session = boto3.Session(region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise RuntimeError("계정 불일치")
    mode = os.environ["MODE"]
    execution = os.environ["EXECUTION_ID"]
    if mode not in ("PREPARE", "APPLY") or not execution.startswith(
        f"arn:aws:states:{REGION}:{ACCOUNT}:execution:ec2-isolation-workflow:"
    ):
        raise RuntimeError("실행 모드 또는 부모 실행 ARN이 잘못되었습니다.")
    bucket, table = os.environ["BUCKET"], os.environ["LOCK_TABLE"]
    quarantine = os.environ["QUARANTINE_SG"]
    token = hashlib.sha256(execution.encode()).hexdigest()
    prefix = f"runs/{token}/"
    ec2, s3, ddb = (session.client(x) for x in ("ec2", "s3", "dynamodb"))
    locked = False

    def put(name, data):
        if not isinstance(data, bytes):
            data = json.dumps(data, indent=2, default=str).encode()
        s3.put_object(Bucket=bucket, Key=prefix + name, Body=data)

    def get(name):
        return s3.get_object(Bucket=bucket, Key=prefix + name)["Body"].read()

    def instance(iid):
        return ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]

    def init():
        run("terraform", "init", "-input=false", "-no-color",
            f"-backend-config=bucket={bucket}",
            "-backend-config=key=terraform/bastion.tfstate",
            f"-backend-config=region={REGION}",
            "-backend-config=encrypt=true", "-backend-config=use_lockfile=true")

    try:
        if mode == "PREPARE":
            ddb.put_item(TableName=table, Item={
                "LockId": {"S": "response"}, "Owner": {"S": execution},
                "Created": {"S": str(int(time.time()))}
            }, ConditionExpression="attribute_not_exists(LockId)")
            locked = True
            old = instance(OLD)
            if old["State"]["Name"] != "running" or old["VpcId"] != VPC:
                raise RuntimeError("운영 배스천이 실행 상태 또는 지정 VPC가 아닙니다.")
            if len(old["NetworkInterfaces"]) != 1:
                raise RuntimeError("운영 배스천의 ENI가 1개가 아닙니다. 격리 방식을 재검토하세요.")
            if {g["GroupId"] for g in old["SecurityGroups"]} != {NORMAL_SG}:
                raise RuntimeError("운영 배스천의 보안 그룹이 변경되었거나 이미 격리되었습니다.")
            web = instance(WEB)
            if web["State"]["Name"] != "running" or len(web["NetworkInterfaces"]) != 1:
                raise RuntimeError("웹 서버 상태 또는 ENI 구성이 예상과 다릅니다.")
            if {g["GroupId"] for g in web["SecurityGroups"]} != {"sg-00000000000001a89"}:
                raise RuntimeError("웹 서버가 정상 보안 그룹 상태가 아닙니다. 중복 격리를 중단합니다.")
            image = ec2.describe_images(ImageIds=[AMI])["Images"]
            if len(image) != 1 or image[0]["State"] != "available" or image[0]["OwnerId"] != "123456789012":
                raise RuntimeError("배스천 AMI가 존재하지 않거나 사용할 수 없습니다.")
            groups = ec2.describe_security_groups(GroupIds=[NORMAL_SG, METRICS_SG, quarantine])["SecurityGroups"]
            byid = {g["GroupId"]: g for g in groups}
            if any(g["VpcId"] != VPC for g in groups):
                raise RuntimeError("보안 그룹의 VPC 불일치")
            if byid[quarantine]["IpPermissions"] or byid[quarantine]["IpPermissionsEgress"]:
                raise RuntimeError("격리 보안 그룹에 허용 규칙이 있습니다.")
            if not any(p.get("IpProtocol") == "tcp" and p.get("FromPort") == 9100
                       and p.get("ToPort") == 9100
                       and any(g.get("GroupId") == "sg-00000000000008247" for g in p.get("UserIdGroupPairs", []))
                       for p in byid[METRICS_SG]["IpPermissions"]):
                raise RuntimeError("Prometheus에서 9100 접근을 허용한 규칙이 없습니다.")
            profile = session.client("iam").get_instance_profile(InstanceProfileName="team5-bastion-role")["InstanceProfile"]
            if not profile["Roles"]:
                raise RuntimeError("배스천 인스턴스 프로파일에 IAM 역할이 없습니다.")
            ec2.describe_key_pairs(KeyNames=["team5-key"])
            subnet = ec2.describe_subnets(SubnetIds=[SUBNET])["Subnets"][0]
            if subnet["VpcId"] != VPC or subnet["AvailableIpAddressCount"] < 1:
                raise RuntimeError("서브넷 또는 가용 IP를 확인하세요.")
            init()
            run("terraform", "validate", "-no-color")
            run("terraform", "plan", "-input=false", "-no-color", "-lock-timeout=60s", "-out=bastion.tfplan")
            plan = json.loads(run("terraform", "show", "-json", "bastion.tfplan", capture=True))
            check_plan(plan)
            # 실제 생성 요청의 권한을 격리 전에 검사합니다. 용량/할당량 성공까지 보장하지는 않습니다.
            try:
                ec2.run_instances(
                    DryRun=True, ImageId=AMI, InstanceType="t3.micro", MinCount=1, MaxCount=1,
                    KeyName="team5-key", IamInstanceProfile={"Name": "team5-bastion-role"},
                    NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": SUBNET,
                        "AssociatePublicIpAddress": True, "DeleteOnTermination": True,
                        "Groups": [NORMAL_SG, METRICS_SG]}],
                    MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled", "HttpPutResponseHopLimit": 2},
                    TagSpecifications=[{"ResourceType": "instance", "Tags": [
                        {"Key": "ManagedBy", "Value": "team5-bastion-integration"}
                    ]}]
                )
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "DryRunOperation":
                    raise
            else:
                raise RuntimeError("EC2 DryRun 응답이 예상과 다릅니다.")
            binary = (TF / "bastion.tfplan").read_bytes()
            put("plan.bin", binary)
            put("terraform.lock.hcl", (TF / ".terraform.lock.hcl").read_bytes())
            put("manifest.json", {
                "execution": execution, "plan_sha256": hashlib.sha256(binary).hexdigest(),
                "original_instance": OLD,
                "original_groups": [g["GroupId"] for g in old["SecurityGroups"]],
                "source_version": os.environ.get("CODEBUILD_RESOLVED_SOURCE_VERSION")
            })
            print("BASTION_PREPARE_OK: 격리/생성은 아직 실행하지 않았습니다.")
        else:
            lock = ddb.get_item(TableName=table, Key={"LockId": {"S": "response"}}, ConsistentRead=True).get("Item", {})
            if lock.get("Owner", {}).get("S") != execution:
                raise RuntimeError("실행 잠금 소유자 불일치")
            manifest = json.loads(get("manifest.json"))
            if manifest["execution"] != execution or manifest["original_instance"] != OLD:
                raise RuntimeError("계획 소유자 불일치")
            old = instance(OLD)
            if {g["GroupId"] for g in old["SecurityGroups"]} != {quarantine}:
                raise RuntimeError("운영 배스천의 격리 보안 그룹 적용을 확인할 수 없습니다.")
            (TF / ".terraform.lock.hcl").write_bytes(get("terraform.lock.hcl"))
            binary = get("plan.bin")
            if hashlib.sha256(binary).hexdigest() != manifest["plan_sha256"]:
                raise RuntimeError("저장한 생성 계획의 해시 불일치")
            (TF / "bastion.tfplan").write_bytes(binary)
            init()
            check_plan(json.loads(run("terraform", "show", "-json", "bastion.tfplan", capture=True)))
            run("terraform", "apply", "-input=false", "-no-color", "-lock-timeout=60s", "bastion.tfplan")
            outputs = json.loads(run("terraform", "output", "-json", capture=True))
            iid = outputs["instance_id"]["value"]
            if iid in (OLD, "i-0000000000000216c", WEB):
                raise RuntimeError("새 배스천 ID가 기존 서버와 같습니다.")
            put("created.json", outputs)
            ssm = session.client("ssm")
            for _ in range(90):
                info = ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [iid]}])["InstanceInformationList"]
                if info and info[0]["PingStatus"] == "Online":
                    break
                time.sleep(10)
            else:
                raise RuntimeError("새 배스천 SSM Online 대기 시간 초과")
            probe = f"TEAM5_BASTION_READY_{token[:24]}"
            command = "\n".join([
                "set -eu", "cloud-init status --wait",
                "grep -F BASTION_BOOTSTRAP_OK /var/log/cloud-init-output.log",
                "grep -F BASTION_MONITORING_BOOTSTRAP_OK /var/log/cloud-init-output.log",
                "systemctl is-active --quiet amazon-ssm-agent rsyslog amazon-cloudwatch-agent node_exporter",
                "systemctl is-enabled amazon-ssm-agent rsyslog amazon-cloudwatch-agent node_exporter",
                "/usr/sbin/sshd -t",
                "curl -fsS --max-time 10 http://127.0.0.1:9100/metrics -o /tmp/team5-ready-metrics",
                "grep '^node_exporter_build_info' /tmp/team5-ready-metrics",
                f"logger -p authpriv.notice -t team5-bastion-recovery {probe}",
                "echo BASTION_SERVICE_CHECK_OK"
            ])
            cid = ssm.send_command(InstanceIds=[iid], DocumentName="AWS-RunShellScript",
                Parameters={"commands": [command], "executionTimeout": ["900"]}, TimeoutSeconds=120)["Command"]["CommandId"]
            for _ in range(110):
                try:
                    result = ssm.get_command_invocation(CommandId=cid, InstanceId=iid)
                except ClientError as exc:
                    if exc.response["Error"]["Code"] != "InvocationDoesNotExist":
                        raise
                    time.sleep(10)
                    continue
                if result["Status"] == "Success":
                    break
                if result["Status"] not in ("Pending", "InProgress", "Delayed"):
                    put("verification-failed.json", result)
                    raise RuntimeError(f"SSM 검증 실패: {result['Status']}; 명령 ID {cid}")
                time.sleep(10)
            else:
                raise RuntimeError("SSM 검증 대기 시간 초과")
            logs = session.client("logs")
            for _ in range(36):
                try:
                    events = logs.filter_log_events(logGroupName=LOG_GROUP,
                        logStreamNames=[f"{iid}/secure"], filterPattern=f'"{probe}"')["events"]
                except logs.exceptions.ResourceNotFoundException:
                    events = []
                if events:
                    break
                time.sleep(5)
            else:
                raise RuntimeError("CloudWatch 인증 로그 전송을 확인하지 못했습니다.")
            final = {
                "Status": "SUCCEEDED", "OriginalInstance": OLD, "NewInstance": iid,
                "PrivateIP": outputs["private_ip"]["value"], "PublicIP": outputs["public_ip"]["value"],
                "SSMCommandId": cid, "CloudWatchVerified": True,
                "NodeExporterLocalVerified": True, "PrometheusScrapeVerified": False,
                "Quarantine": "security-group-only; established connections may persist"
            }
            put("result.json", final)
            print(json.dumps(final, ensure_ascii=False))
    except Exception as exc:
        if locked:
            # PREPARE에서만 해제: 격리는 아직 시작하지 않은 단계입니다.
            ddb.delete_item(TableName=table, Key={"LockId": {"S": "response"}},
                ConditionExpression="#owner = :owner", ExpressionAttributeNames={"#owner": "Owner"},
                ExpressionAttributeValues={":owner": {"S": execution}})
        raise


if __name__ == "__main__":
    main()
