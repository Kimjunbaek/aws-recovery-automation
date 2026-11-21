"""Offline AWS SDK parameter checks. Does not connect to AWS."""
import ast
import json
from pathlib import Path
import botocore.session
from botocore.validate import validate_parameters
import design

root = Path(__file__).resolve().parent
session = botocore.session.get_session()
count = 0


def check(service, operation, parameters):
    global count
    validate_parameters(parameters, session.get_service_model(service).operation_model(operation).input_shape)
    count += 1


for path in root.glob('*.py'):
    ast.parse(path.read_text(encoding='utf-8'))
source = json.loads((root / 'reference-recovery.json').read_text())
docs = design.documents()
candidate = design.build(source, docs)
for doc in docs.values():
    check('ssm', 'CreateDocument', {'Name': doc['name'], 'DocumentType': 'Command', 'DocumentFormat': 'JSON', 'Content': json.dumps(doc['content'])})
check('ssm', 'SendCommand', {'DocumentName': docs['proxy']['name'], 'DocumentVersion': '1', 'InstanceIds': [design.PROXY],
                            'Parameters': {'Action': ['inspect'], 'Owner': ['arn:example']}, 'TimeoutSeconds': 120})
check('ssm', 'GetCommandInvocation', {'CommandId': '00000000-0000-4000-8000-08708dd8811e', 'InstanceId': design.PROXY, 'PluginName': 'ConfigureAndVerify'})
check('iam', 'PutRolePolicy', {'RoleName': 'test-role', 'PolicyName': 'team5-proxy-aware-recovery', 'PolicyDocument': json.dumps(design.policy(docs))})
check('stepfunctions', 'ValidateStateMachineDefinition', {'definition': json.dumps(candidate), 'type': 'STANDARD'})
check('stepfunctions', 'UpdateStateMachine', {'stateMachineArn': design.WORKFLOW, 'definition': json.dumps(candidate)})
check('elbv2', 'CreateRule', {'ListenerArn': design.LISTENER, 'Priority': 49000,
                            'Conditions': [{'Field': 'http-header', 'HttpHeaderConfig': {'HttpHeaderName': 'X-Team5-Rollback', 'Values': ['sample']}}],
                            'Actions': [{'Type': 'forward', 'TargetGroupArn': design.WEB_TG}]})
check('elbv2', 'ModifyListener', {'ListenerArn': design.LISTENER, 'DefaultActions': [{'Type': 'forward', 'TargetGroupArn': design.WEB_TG}]})
(root / 'preview-definition.json').write_text(json.dumps(candidate, indent=2) + '\n')
assert len(json.dumps(candidate).encode()) < 1048576
print(f'SDK shapes checked: {count}; Python syntax and preview generated. No AWS requests.')
