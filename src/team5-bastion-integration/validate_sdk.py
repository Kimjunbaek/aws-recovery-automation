"""AWS 호출 없이 botocore의 서비스 모델로 주요 요청 형식을 검사합니다."""
import json
from pathlib import Path
import botocore.session
from botocore.validate import validate_parameters
import yaml
import design

root = Path(__file__).resolve().parent
session = botocore.session.get_session()

def check(service, operation, params):
    shape = session.get_service_model(service).operation_model(operation).input_shape
    validate_parameters(params, shape)

source = json.loads((root / "reference-parent.json").read_text(encoding="utf-8"))
infra = design.template(source["definition"], source["roleArn"])
definition = design.wrapper(source["definition"], "sg-00000000000001a42")
check("cloudformation", "CreateStack", {
    "StackName": design.PREFIX, "TemplateBody": json.dumps(infra),
    "Capabilities": ["CAPABILITY_NAMED_IAM"], "Tags": [{"Key": "ManagedBy", "Value": design.PREFIX}]
})
check("codebuild", "UpdateProject", {"name": design.PROJECT, "sourceVersion": "sample-version"})
check("codebuild", "StartBuild", {"projectName": design.PROJECT, "environmentVariablesOverride": [
    {"name": "MODE", "value": "PREPARE", "type": "PLAINTEXT"},
    {"name": "EXECUTION_ID", "value": "arn:test", "type": "PLAINTEXT"}
]})
check("ec2", "ModifyInstanceAttribute", {"InstanceId": design.OLD, "Groups": ["sg-00000000000001a42"]})
check("ssm", "SendCommand", {
    "InstanceIds": ["i-00000000000000cae"], "DocumentName": "AWS-RunShellScript",
    "Parameters": {"commands": ["echo check"], "executionTimeout": ["900"]}, "TimeoutSeconds": 120
})
check("dynamodb", "DeleteItem", {
    "TableName": design.TABLE, "Key": {"LockId": {"S": "response"}},
    "ConditionExpression": "#owner = :owner", "ExpressionAttributeNames": {"#owner": "Owner"},
    "ExpressionAttributeValues": {":owner": {"S": "arn:test"}}
})
check("stepfunctions", "UpdateStateMachine", {"stateMachineArn": design.PARENT, "definition": json.dumps(definition)})
check("logs", "FilterLogEvents", {
    "logGroupName": "/project/security/bastion-auth", "logStreamNames": ["i-example/secure"],
    "filterPattern": '"TEAM5_BASTION_READY_test"'
})
buildspec = yaml.safe_load((root / "runtime/buildspec.yml").read_text())
assert buildspec["version"] == 0.2
assert len(buildspec["phases"]["install"]["commands"]) == 2
assert len(json.dumps(infra).encode()) < 51200
assert len(json.dumps(definition).encode()) < 1048576
print("SDK 요청 형식 8개, buildspec YAML, 정의 크기 제한 검사 통과 (AWS 호출 없음)")
