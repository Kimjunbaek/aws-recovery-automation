"""실제 실행은 execute 인자를 명시해야 합니다. 상태 조회는 status."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import boto3
from design import ACCOUNT, REGION, PARENT, CORE, BUCKET, TABLE

p = argparse.ArgumentParser()
p.add_argument("mode", choices=["execute", "status"])
args = p.parse_args()
folder = Path(__file__).resolve().parent / "deployment"
session = boto3.Session(region_name=REGION)
if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
    raise RuntimeError("AWS 계정 불일치")
sf = session.client("stepfunctions")
if args.mode == "execute":
    if not (folder / "deployed.json").is_file():
        raise RuntimeError("setup.py apply 배포 기록이 없습니다.")
    current = json.loads(sf.describe_state_machine(stateMachineArn=PARENT)["definition"])
    if "PrepareBastion" not in current["States"]:
        raise RuntimeError("배스천 통합이 연결되지 않았습니다.")
    for arn in [PARENT, CORE, f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:team5-recovery-workflow"]:
        if sf.list_executions(stateMachineArn=arn, statusFilter="RUNNING", maxResults=1)["executions"]:
            raise RuntimeError("이미 실행 중인 대응/복구 작업이 있습니다.")
    lock = session.client("dynamodb").get_item(TableName=TABLE, Key={"LockId": {"S": "response"}}, ConsistentRead=True)
    if "Item" in lock:
        raise RuntimeError("대응 잠금이 있습니다. 이전 실행과 CodeBuild 상태부터 확인하세요.")
    data = {"source": "project.security.detection", "detail-type": "EC2 Security Incident", "detail": {
        "target_instance_id": "i-000000000000071f8", "event_type": "FILE_INTEGRITY_VIOLATION", "severity": "CRITICAL",
        "execute_isolation": True, "confirm_management_plan": True, "execute_recovery_test": True
    }}
    result = sf.start_execution(stateMachineArn=PARENT,
        name="web-bastion-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f"), input=json.dumps(data))
    (folder / "latest-execution.json").write_text(json.dumps(result, default=str, indent=2))
    print(json.dumps(result, default=str, indent=2))
else:
    arn = json.loads((folder / "latest-execution.json").read_text())["executionArn"]
    result = sf.describe_execution(executionArn=arn)
    print(json.dumps({k: result.get(k) for k in ("status", "error", "cause", "output")}, ensure_ascii=False, indent=2))
    key = "runs/" + hashlib.sha256(arn.encode()).hexdigest() + "/result.json"
    s3 = session.client("s3")
    try:
        print(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode())
    except s3.exceptions.NoSuchKey:
        print("배스천 최종 검증 결과는 아직 저장되지 않았습니다.")
