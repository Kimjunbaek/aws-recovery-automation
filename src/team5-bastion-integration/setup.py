"""CloudShell: prepare (읽기/검증), apply (인프라/연결 배포), disconnect (연결 원복).

이 프로그램은 start_execution, send_command, terraform apply를 실행하지 않습니다.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

from design import ACCOUNT, REGION, PREFIX, PARENT, CORE, BUCKET, PROJECT, template, wrapper

HERE = Path(__file__).resolve().parent
WORK = HERE / "deployment"


def save(name, obj):
    WORK.mkdir(exist_ok=True)
    (WORK / name).write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def read(name):
    return json.loads((WORK / name).read_text(encoding="utf-8"))


def source_zip():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted((HERE / "runtime").rglob("*")):
            relative = p.relative_to(HERE / "runtime")
            if p.is_file() and not any(part in ("__pycache__", ".terraform") for part in relative.parts):
                if p.suffix in (".tf", ".sh", ".py", ".yml"):
                    # ZIP 메타데이터를 고정해 prepare/apply 간 해시를 재현합니다.
                    info = zipfile.ZipInfo(relative.as_posix(), (2026, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    z.writestr(info, p.read_bytes())
    return stream.getvalue()


def no_running(sf):
    for arn in [PARENT, CORE, f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:team5-recovery-workflow"]:
        try:
            running = sf.list_executions(stateMachineArn=arn, statusFilter="RUNNING", maxResults=1)["executions"]
        except sf.exceptions.StateMachineDoesNotExist:
            continue
        if running:
            raise RuntimeError(f"실행 중인 작업이 있습니다: {running[0]['executionArn']}")


def validate(sf, definition):
    response = sf.validate_state_machine_definition(definition=json.dumps(definition), type="STANDARD")
    if response["result"] != "OK":
        raise RuntimeError(json.dumps(response, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "apply", "disconnect"])
    args = parser.parse_args()
    session = boto3.Session(region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise RuntimeError("프로젝트 계정 123456789012에서 실행하세요.")
    sf, cf, ec2 = (session.client(x) for x in ("stepfunctions", "cloudformation", "ec2"))
    no_running(sf)
    current = sf.describe_state_machine(stateMachineArn=PARENT)
    original = json.loads(current["definition"])
    if args.mode == "prepare":
        if (WORK / "deployed.json").exists():
            raise RuntimeError("기존 배포 기록이 있습니다. 다시 준비하지 말고 배포 상태부터 확인하세요.")
        if "PrepareBastion" in original["States"]:
            raise RuntimeError("이미 배스천 연결이 있습니다.")
        target = original["States"]["ValidateTarget"]["Choices"][0]["And"]
        if not any(c.get("StringEquals") == "i-000000000000071f8" for c in target):
            raise RuntimeError("현재 웹 대상이 예상과 다릅니다.")
        instances = ec2.describe_instances(InstanceIds=["i-0000000000000c871"])["Reservations"]
        old = instances[0]["Instances"][0]
        if old["VpcId"] != "vpc-000000000000021c2" or len(old["NetworkInterfaces"]) != 1:
            raise RuntimeError("운영 배스천 VPC 또는 ENI 구성이 예상과 다릅니다.")
        image = ec2.describe_images(ImageIds=["ami-00000000000000cac"])["Images"]
        if not image or image[0]["State"] != "available":
            raise RuntimeError("배스천 AMI를 사용할 수 없습니다.")
        ec2.describe_security_groups(GroupIds=["sg-000000000000039bc", "sg-0000000000000bd52"])
        profile = session.client("iam").get_instance_profile(InstanceProfileName="team5-bastion-role")["InstanceProfile"]
        bastion_role = profile["Roles"][0]["Arn"]
        candidate = wrapper(original, "sg-00000000000001a42")
        infra = template(original, current["roleArn"], bastion_role)
        validate(sf, candidate)
        cf.validate_template(TemplateBody=json.dumps(infra))
        runtime = source_zip()
        save("before.json", original)
        save("preview.json", candidate)
        save("infrastructure.json", infra)
        save("manifest.json", {
            "parent": PARENT, "revision": current["revisionId"], "role": current["roleArn"],
            "bastion_role": bastion_role, "source_sha256": hashlib.sha256(runtime).hexdigest(),
            "infrastructure_sha256": hashlib.sha256(json.dumps(infra, sort_keys=True).encode()).hexdigest(),
            "old_bastion": "i-0000000000000c871"
        })
        print("설계 및 AWS 정의 검증 완료. 리소스 생성/설정 변경/격리는 실행하지 않았습니다.")
        print(f"검토 파일: {WORK / 'preview.json'}")
        print("배포: python3 setup.py apply")
        print("배포 후에도 격리/EC2 생성은 실행하지 않습니다.")
        return

    manifest = read("manifest.json")
    if args.mode == "disconnect":
        deployed = read("deployed.json")
        if original != read("after.json"):
            raise RuntimeError("배포 후 다른 변경이 있습니다. 자동 원복을 중단합니다.")
        sf.update_state_machine(stateMachineArn=PARENT, definition=json.dumps(read("before.json")))
        print("부모 워크플로우 연결만 원복했습니다. 격리/인스턴스/인프라는 원복 또는 삭제하지 않았습니다.")
        return

    if current["revisionId"] != manifest["revision"]:
        raise RuntimeError("준비 후 부모 정의가 변경되었습니다. 배포를 중단합니다.")
    runtime = source_zip()
    if hashlib.sha256(runtime).hexdigest() != manifest["source_sha256"]:
        raise RuntimeError("준비 후 실행 코드가 변경되었습니다. prepare부터 다시 수행하세요.")
    infra = read("infrastructure.json")
    if hashlib.sha256(json.dumps(infra, sort_keys=True).encode()).hexdigest() != manifest["infrastructure_sha256"]:
        raise RuntimeError("준비 후 인프라 정의가 변경되었습니다.")
    try:
        existing = cf.describe_stacks(StackName=PREFIX)["Stacks"][0]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ValidationError" and "does not exist" in str(exc):
            existing = None
        else:
            raise
    params = dict(StackName=PREFIX, TemplateBody=json.dumps(infra), Capabilities=["CAPABILITY_NAMED_IAM"],
                  Tags=[{"Key": "ManagedBy", "Value": PREFIX}])
    if existing:
        if {t["Key"]: t["Value"] for t in existing.get("Tags", [])}.get("ManagedBy") != PREFIX:
            raise RuntimeError("동일 이름의 다른 스택이 있습니다.")
        try:
            cf.update_stack(**params)
        except ClientError as exc:
            if "No updates are to be performed" not in str(exc):
                raise
    else:
        cf.create_stack(**params)
    for _ in range(120):
        stack = cf.describe_stacks(StackName=PREFIX)["Stacks"][0]
        status = stack["StackStatus"]
        print(f"자동 실행 기반 배포: {status}", flush=True)
        if status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
            break
        if not status.endswith("_IN_PROGRESS"):
            raise RuntimeError("스택 배포 실패. CloudFormation 이벤트를 확인하세요: " + status)
        time.sleep(10)
    else:
        raise RuntimeError("배포 대기 시간 초과. 부모 정의는 아직 변경하지 않았습니다.")
    quarantine = next(o["OutputValue"] for o in stack["Outputs"] if o["OutputKey"] == "QuarantineSG")
    # EC2/CloudFormation이 생성한 기본 아웃바운드 허용 규칙도 제거합니다.
    group = ec2.describe_security_groups(GroupIds=[quarantine])["SecurityGroups"][0]
    if group["IpPermissions"]:
        raise RuntimeError("새 격리 보안 그룹에 예상하지 않은 인바운드 규칙이 있습니다.")
    if group["IpPermissionsEgress"]:
        ec2.revoke_security_group_egress(GroupId=quarantine, IpPermissions=group["IpPermissionsEgress"])
    group = ec2.describe_security_groups(GroupIds=[quarantine])["SecurityGroups"][0]
    if group["IpPermissionsEgress"]:
        raise RuntimeError("격리 보안 그룹의 아웃바운드 규칙이 남아 있습니다.")
    put = session.client("s3").put_object(Bucket=BUCKET, Key="source/runtime.zip", Body=runtime)
    session.client("codebuild").update_project(name=PROJECT, sourceVersion=put["VersionId"])
    after = wrapper(read("before.json"), quarantine)
    validate(sf, after)
    no_running(sf)
    latest = sf.describe_state_machine(stateMachineArn=PARENT)
    if latest["revisionId"] != manifest["revision"]:
        raise RuntimeError("배포 중 부모 정의가 변경되었습니다. 기반 리소스만 생성했고 부모 연결은 하지 않았습니다.")
    save("after.json", after)
    # revisionId는 UpdateStateMachine의 조건 인자가 아니므로 확인-수정 사이에 작은 경합 가능성이 있습니다.
    result = sf.update_state_machine(stateMachineArn=PARENT, definition=json.dumps(after))
    for _ in range(20):
        actual = json.loads(sf.describe_state_machine(stateMachineArn=PARENT)["definition"])
        if actual == after:
            break
        time.sleep(1)
    save("deployed.json", {"quarantine": quarantine, "source_version": put["VersionId"],
                           "revision": result.get("revisionId"), "observed_match": actual == after})
    if actual != after:
        raise RuntimeError("부모 갱신을 요청했으나 조회 결과 일치를 확인하지 못했습니다. 실제 실행은 하지 마세요.")
    print("운영 배스천 병렬 대응 연결 배포 완료. 격리/복구/새 EC2 생성은 실행하지 않았습니다.")
    print(f"원본 정의: {WORK / 'before.json'}")
    print("실행 전 웹 DB 프록시 연결, 백업 유효성, RDS 여유, 서비스 상태를 확인하세요.")
    print("실제 실행 명령: python3 run-response.py execute")


if __name__ == "__main__":
    main()
