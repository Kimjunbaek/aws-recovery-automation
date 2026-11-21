"""CloudShell deployment. prepare is AWS read-only; apply never starts recovery."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import boto3
from design import ACCOUNT, REGION, WORKFLOW, PROXY, PROXY_IP, build, documents, policy

HERE = Path(__file__).resolve().parent
POLICY = 'team5-proxy-aware-recovery'


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')


def fingerprint():
    return {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
            for name in ('setup.py', 'design.py', 'proxy_worker.py', 'web_worker.py', 'proxy-recovery.py')}


def validate(sf, candidate):
    result = sf.validate_state_machine_definition(definition=json.dumps(candidate), type='STANDARD')
    if result['result'] != 'OK':
        raise RuntimeError(json.dumps(result['diagnostics'], ensure_ascii=False))


def no_running(sf):
    if sf.list_executions(stateMachineArn=WORKFLOW, statusFilter='RUNNING', maxResults=1)['executions']:
        raise RuntimeError('복구 워크플로우 실행 중입니다. 종료 후 배포하세요.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['prepare', 'apply'])
    args = parser.parse_args()
    session = boto3.Session(region_name=REGION)
    if session.client('sts').get_caller_identity()['Account'] != ACCOUNT:
        raise RuntimeError('프로젝트 계정이 아닙니다.')
    sf, ssm, iam = [session.client(name) for name in ('stepfunctions', 'ssm', 'iam')]
    no_running(sf)
    current = sf.describe_state_machine(stateMachineArn=WORKFLOW)
    if args.mode == 'prepare':
        instance = session.client('ec2').describe_instances(InstanceIds=[PROXY])['Reservations'][0]['Instances'][0]
        if instance['State']['Name'] != 'running' or instance['PrivateIpAddress'] != PROXY_IP:
            raise RuntimeError('프록시 인스턴스/IP가 예상과 다릅니다.')
        profile_name = instance['IamInstanceProfile']['Arn'].split('/')[-1]
        profile = iam.get_instance_profile(InstanceProfileName=profile_name)
        if [r['RoleName'] for r in profile['InstanceProfile']['Roles']] != ['team5-proxy-ssm-role-iac']:
            raise RuntimeError('프록시 IAM 역할이 변경되었습니다.')
        online = ssm.describe_instance_information(Filters=[{'Key': 'InstanceIds', 'Values': [PROXY]}])['InstanceInformationList']
        if not online or online[0]['PingStatus'] != 'Online':
            raise RuntimeError('프록시 SSM이 Online 상태가 아닙니다.')
        metadata = ssm.describe_parameters(ParameterFilters=[{'Key': 'Name', 'Option': 'Equals', 'Values': ['/team5/recovery/proxysql/admin']}])['Parameters']
        if len(metadata) != 1 or metadata[0]['Type'] != 'SecureString' or metadata[0].get('KeyId') != 'alias/aws/ssm':
            raise RuntimeError('예상한 SecureString/aws/ssm 키가 아닙니다. KMS 권한 확인이 필요합니다.')
        before = json.loads(current['definition'])
        docs = documents()
        candidate = build(before, docs)
        validate(sf, candidate)
        folder = HERE / 'deployment' / datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
        folder.mkdir(parents=True)
        role = current['roleArn'].split('/')[-1]
        try:
            previous_policy = iam.get_role_policy(RoleName=role, PolicyName=POLICY)['PolicyDocument']
        except iam.exceptions.NoSuchEntityException:
            previous_policy = None
        if previous_policy is not None:
            raise RuntimeError('동일 이름의 배포 정책이 이미 있습니다. 기존 배포 기록부터 확인하세요.')
        save(folder / 'before.json', before)
        save(folder / 'after.json', candidate)
        save(folder / 'documents.json', docs)
        save(folder / 'policy.json', policy(docs))
        save(folder / 'manifest.json', {'revision': current['revisionId'], 'role': role, 'fingerprint': fingerprint(), 'previous_policy': previous_policy})
        save(HERE / 'latest-deployment.json', {'folder': str(folder)})
        print('AWS 워크플로우 정의 검증 완료. 아직 AWS 설정을 변경하지 않았습니다.')
        print('검토 폴더:', folder)
        print('변경: 새 SSM 문서 2개 / 복구 역할 정책 1개 / 복구 워크플로우')
        print('공유 프록시 전환은 Cutover=true만 허용합니다. 실패 시 자동 ALB 복귀 대신 명시적 복귀 도구를 사용합니다.')
        print('배포: python3 setup.py apply')
        return
    folder = Path(json.loads((HERE / 'latest-deployment.json').read_text())['folder'])
    manifest = json.loads((folder / 'manifest.json').read_text())
    if manifest['fingerprint'] != fingerprint():
        raise RuntimeError('prepare 이후 코드가 변경되었습니다. prepare부터 다시 실행하세요.')
    if current['revisionId'] != manifest['revision']:
        raise RuntimeError('원격 워크플로우가 변경되었습니다. prepare부터 다시 실행하세요.')
    candidate = json.loads((folder / 'after.json').read_text())
    docs = json.loads((folder / 'documents.json').read_text())
    validate(sf, candidate)
    for item in docs.values():
        try:
            ssm.create_document(Name=item['name'], DocumentType='Command', DocumentFormat='JSON', Content=json.dumps(item['content']))
        except ssm.exceptions.DocumentAlreadyExists:
            existing = ssm.get_document(Name=item['name'], DocumentVersion='1', DocumentFormat='JSON')
            if json.loads(existing['Content']) != item['content']:
                raise RuntimeError('동일 이름의 SSM 문서 내용이 다릅니다.')
    iam.put_role_policy(RoleName=manifest['role'], PolicyName=POLICY,
                        PolicyDocument=(folder / 'policy.json').read_text())
    no_running(sf)
    if sf.describe_state_machine(stateMachineArn=WORKFLOW)['revisionId'] != manifest['revision']:
        raise RuntimeError('배포 도중 워크플로우가 변경되었습니다. 문서/정책은 생성됐지만 연결은 변경하지 않았습니다.')
    sf.update_state_machine(stateMachineArn=WORKFLOW, definition=json.dumps(candidate))
    actual = sf.describe_state_machine(stateMachineArn=WORKFLOW)
    if json.loads(actual['definition']) != candidate:
        raise RuntimeError('업데이트 결과를 즉시 확인하지 못했습니다. 콘솔/재조회로 확인하세요. 복구는 시작하지 마세요.')
    save(folder / 'applied.json', {'revision': actual['revisionId'], 'stateMachineArn': WORKFLOW})
    print('프록시 연동 설정 배포 완료. 격리·복원·DB 대상 변경·ALB 전환은 실행하지 않았습니다.')
    print('원본 정의:', folder / 'before.json')
    print('다음 확인: python3 proxy-recovery.py check')
    print('이후 기존 격리 워크플로우가 복구를 호출하면 새 로직이 실행됩니다.')


if __name__ == '__main__':
    main()
