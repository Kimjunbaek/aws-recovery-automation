"""Proxy readiness and guarded DB+ALB rollback. Never deletes EC2/RDS."""
import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import boto3
from design import ACCOUNT, REGION, WORKFLOW, PROXY, WEB_INSTANCE, LISTENER, WEB_TG, RECOVERY_TG

HERE = Path(__file__).resolve().parent


def groups(actions):
    result = set()
    for action in actions:
        if action['Type'] != 'forward':
            raise RuntimeError('단순 전달 리스너가 아닙니다.')
        config = action.get('ForwardConfig', {}).get('TargetGroups', [])
        if config:
            result.update(t['TargetGroupArn'] for t in config if t.get('Weight', 1) > 0)
        elif action.get('TargetGroupArn'):
            result.add(action['TargetGroupArn'])
    return result


def send(ssm, doc, instance, parameters):
    result = ssm.send_command(DocumentName=doc['name'], DocumentVersion='1', InstanceIds=[instance],
                              Parameters={k: [v] for k, v in parameters.items()}, TimeoutSeconds=120)
    command_id = result['Command']['CommandId']
    print('SSM 명령:', command_id, '대상:', instance, flush=True)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        try:
            status = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance, PluginName='ConfigureAndVerify')
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(5)
            continue
        if status['Status'] in ('Pending', 'InProgress', 'Delayed', 'Cancelling'):
            time.sleep(5)
            continue
        output = status.get('StandardOutputContent', '')
        if status['Status'] != 'Success' or status.get('ResponseCode') != 0:
            raise RuntimeError('SSM 실패: ' + command_id + '\n' + output)
        print(output.strip())
        return output
    raise RuntimeError('SSM 상태 확인 시간 초과. 명령이 계속 실행될 수 있으므로 재실행 전에 확인하세요: ' + command_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['check', 'status', 'rollback'])
    parser.add_argument('execution', nargs='?')
    args = parser.parse_args()
    session = boto3.Session(region_name=REGION)
    if session.client('sts').get_caller_identity()['Account'] != ACCOUNT:
        raise RuntimeError('프로젝트 계정이 아닙니다.')
    sf, ssm, elb = [session.client(service) for service in ('stepfunctions', 'ssm', 'elbv2')]
    deployment = Path(json.loads((HERE / 'latest-deployment.json').read_text())['folder'])
    if not (deployment / 'applied.json').exists():
        raise RuntimeError('배포 완료 기록이 없습니다.')
    docs = json.loads((deployment / 'documents.json').read_text())
    if args.mode == 'check':
        owner = WORKFLOW.replace(':stateMachine:', ':execution:') + ':readiness-' + uuid.uuid4().hex
        send(ssm, docs['proxy'], PROXY, {'Action': 'inspect', 'Owner': owner})
        print('프록시 조회/인증/구조 검사 통과. 설정 쓰기 권한과 실제 복구는 아직 검증하지 않았습니다.')
        return
    if not args.execution:
        raise RuntimeError('복구 자식 실행 ARN을 지정하세요. 격리 부모 실행 ARN이 아닙니다.')
    execution = sf.describe_execution(executionArn=args.execution)
    if execution['stateMachineArn'] != WORKFLOW:
        raise RuntimeError('team5-recovery-workflow의 실행 ARN을 지정하세요.')
    data = json.loads(execution.get('output') or '{}')
    history = []
    commands = {}
    for page in sf.get_paginator('get_execution_history').paginate(executionArn=args.execution, reverseOrder=True):
        for event in page['events']:
            raw = event.get('stateEnteredEventDetails', {}).get('input')
            if raw:
                value = json.loads(raw)
                if isinstance(value, dict):
                    history.append(value)
                    for key, val in value.items():
                        if key.endswith('Command') and isinstance(val, dict) and val.get('CommandId'):
                            commands[val['CommandId']] = PROXY if key.startswith(('ProxyInspect', 'SwitchProxyBackend')) else value.get('Configuration', {}).get('InstanceId')
    if not data:
        data = next((x for x in history if x.get('Configuration')), history[0] if history else {})
    if args.mode == 'status':
        print(json.dumps({'Status': execution['status'], 'Error': execution.get('error'),
                          'EC2': data.get('Recovered'), 'Configuration': data.get('Configuration'),
                          'ProxySwitch': data.get('SwitchProxyBackendResult', {}).get('StandardOutputContent'),
                          'Checks': data.get('CommandResult', {}).get('StandardOutputContent'),
                          'Cutover': data.get('Cutover')}, indent=2, ensure_ascii=False))
        return
    if execution['status'] == 'RUNNING':
        raise RuntimeError('실행 중에는 복귀할 수 없습니다.')
    if sf.list_executions(stateMachineArn=WORKFLOW, statusFilter='RUNNING', maxResults=1)['executions']:
        raise RuntimeError('다른 복구 실행이 진행 중입니다.')
    for command_id, instance in commands.items():
        if not instance:
            raise RuntimeError('SSM 대상 식별 실패. 수동 점검이 필요합니다.')
        state = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance, PluginName='ConfigureAndVerify')
        if state['Status'] in ('Pending', 'InProgress', 'Delayed', 'Cancelling'):
            raise RuntimeError('아직 종료되지 않은 SSM 명령이 있습니다: ' + command_id)
    baseline = next((x['ProxyListenerBefore']['Listeners'][0]['DefaultActions']
                     for x in [data] + history if x.get('ProxyListenerBefore')), None)
    if not baseline or groups(baseline) != {WEB_TG}:
        raise RuntimeError('이 실행 시작 시 ALB가 기존 웹을 가리켰다는 기록이 없습니다. 자동 복귀를 중단합니다.')
    def listener():
        return elb.describe_listeners(ListenerArns=[LISTENER])['Listeners'][0]['DefaultActions']
    current = listener()
    if groups(current) not in ({WEB_TG}, {RECOVERY_TG}):
        raise RuntimeError('다른 전달 대상이 설정돼 있습니다.')
    instance = session.client('ec2').describe_instances(InstanceIds=[WEB_INSTANCE])['Reservations'][0]['Instances'][0]
    if instance['State']['Name'] != 'running' or {sg['GroupId'] for sg in instance['SecurityGroups']} != {'sg-00000000000001a89'}:
        raise RuntimeError('기존 웹이 실행 중/정상 보안 그룹 상태가 아닙니다. 침해 조사 및 격리 해제는 담당자와 먼저 처리하세요.')
    targets = elb.describe_target_health(TargetGroupArn=WEB_TG)['TargetHealthDescriptions']
    if not any(t['Target']['Id'] == WEB_INSTANCE and t['Target'].get('Port') == 80 for t in targets):
        raise RuntimeError('기존 웹이 대상 그룹에 등록돼 있지 않습니다.')
    folder = HERE / 'rollback' / datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    folder.mkdir(parents=True)
    (folder / 'before.json').write_text(json.dumps({'ExecutionArn': args.execution, 'Listener': current}, indent=2))
    # A private random header makes the original TG InUse without switching default traffic.
    used = {int(r['Priority']) for r in elb.describe_rules(ListenerArn=LISTENER)['Rules'] if r['Priority'] != 'default'}
    priority = next(p for p in range(49000, 50000) if p not in used)
    rule = elb.create_rule(ListenerArn=LISTENER, Priority=priority,
                          Conditions=[{'Field': 'http-header', 'HttpHeaderConfig': {'HttpHeaderName': 'X-Team5-Rollback', 'Values': [uuid.uuid4().hex]}}],
                          Actions=[{'Type': 'forward', 'TargetGroupArn': WEB_TG}])['Rules'][0]['RuleArn']
    (folder / 'temporary-rule.txt').write_text(rule)
    proxy_restored = False
    listener_attempted = False
    try:
        output = send(ssm, docs['proxy'], PROXY, {'Action': 'restore', 'Owner': args.execution})
        if 'PROXY_NOT_CHANGED' in output:
            raise RuntimeError('이 실행은 프록시를 변경하지 않았습니다. ALB 복귀도 수행하지 않았습니다.')
        result = json.loads(output.strip().splitlines()[-1])
        proxy_restored = True
        send(ssm, docs['web'], WEB_INSTANCE, {'Mode': 'verify', 'RestoreDbHost': result['BeforeHost']})
        for attempt in range(60):
            targets = elb.describe_target_health(TargetGroupArn=WEB_TG, Targets=[{'Id': WEB_INSTANCE, 'Port': 80}])['TargetHealthDescriptions']
            if targets and targets[0]['TargetHealth']['State'] == 'healthy':
                break
            time.sleep(5)
        else:
            raise RuntimeError('기존 웹의 ALB 상태 검사를 통과하지 못했습니다.')
        if listener() != current:
            raise RuntimeError('다른 작업자가 ALB를 변경했습니다.')
        listener_attempted = True
        elb.modify_listener(ListenerArn=LISTENER, DefaultActions=baseline)
        if groups(listener()) != {WEB_TG}:
            raise RuntimeError('ALB 복귀 검증 실패')
        send(ssm, docs['proxy'], PROXY, {'Action': 'release', 'Owner': args.execution})
        print('프록시 RDS 대상과 ALB를 기존 환경으로 복귀했습니다. EC2/RDS는 삭제하지 않았습니다.')
    except Exception:
        if proxy_restored and not listener_attempted:
            print('검증 실패: ALB는 변경하지 않고 프록시를 이번 복구 RDS로 되돌립니다.')
            send(ssm, docs['proxy'], PROXY, {'Action': 'resume', 'Owner': args.execution})
        elif listener_attempted:
            print('ALB 변경을 요청한 뒤 오류가 발생했습니다. 프록시 잠금을 유지합니다. ALB/DB 상태를 직접 확인하세요.')
        raise
    finally:
        elb.delete_rule(RuleArn=rule)


if __name__ == '__main__':
    main()
