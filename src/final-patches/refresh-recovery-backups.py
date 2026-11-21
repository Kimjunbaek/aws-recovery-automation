import argparse
import copy
import json
import time
from pathlib import Path
from datetime import datetime, timezone

ACCOUNT='123456789012'
REGION='ap-northeast-2'
VAULT='team5-backup-vault-iac'
AMI='ami-0000000000000e888'
EC2_POINT='arn:aws:ec2:ap-northeast-2::image/'+AMI
RDS_POINT='arn:aws:rds:ap-northeast-2:123456789012:snapshot:awsbackup:job-00000000-0000-4000-8000-869cf19c4a94'
NAMES=('team5-web-response-core','ec2-isolation-workflow')

def patch(definition, core):
    d=copy.deepcopy(definition)
    s=d['States']
    paths=[('PreflightEc2Image','ImageIds',[AMI],'ec2:describeImages'),
           ('PreflightEc2Point','RecoveryPointArn',EC2_POINT,'backup:describeRecoveryPoint'),
           ('PreflightRdsPoint','RecoveryPointArn',RDS_POINT,'backup:describeRecoveryPoint'),
           ('PreflightRdsSnapshot','DbSnapshotIdentifier',RDS_POINT,'rds:describeDBSnapshots')]
    for state,key,value,api in paths:
        if s[state]['Resource']!='arn:aws:states:::aws-sdk:'+api or key not in s[state]['Parameters']:
            raise RuntimeError('예상과 다른 백업 검사 구조: '+state)
        s[state]['Parameters'][key]=value
    if core:
        p=s['PrepareNewRecoveryInput']['Parameters']
        for key,value in [('Ec2RecoveryPointArn',EC2_POINT),('RdsRecoveryPointArn',RDS_POINT)]:
            if key not in p:raise RuntimeError('복원 입력 구조 불일치: '+key)
            p[key]=value
    return d

def save(path,obj):
    path.write_text(json.dumps(obj,indent=2,ensure_ascii=False,default=str),encoding='utf-8')

def main():
    import boto3
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=['prepare','apply'])
    mode=parser.parse_args().mode
    session=boto3.Session(region_name=REGION)
    if session.client('sts').get_caller_identity()['Account']!=ACCOUNT:
        raise RuntimeError('프로젝트 계정이 아닙니다.')
    backup,ec2,rds,sf=[session.client(n) for n in ('backup','ec2','rds','stepfunctions')]
    job=backup.describe_backup_job(BackupJobId='00000000-0000-4000-8000-869cf19c4a94')
    if job['State']!='COMPLETED' or job.get('RecoveryPointArn')!=RDS_POINT:
        raise RuntimeError('새 RDS 백업 완료/ARN 확인 실패')
    for point,source,kind in [(EC2_POINT,f'arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-000000000000071f8','EC2'),(RDS_POINT,f'arn:aws:rds:{REGION}:{ACCOUNT}:db:team5-rds-mysql','RDS')]:
        p=backup.describe_recovery_point(BackupVaultName=VAULT,RecoveryPointArn=point)
        if p.get('Status')!='COMPLETED' or p.get('ResourceArn')!=source or p.get('ResourceType')!=kind:
            raise RuntimeError('백업 상태/원본 확인 실패: '+point)
    images=ec2.describe_images(ImageIds=[AMI])['Images']
    if len(images)!=1 or images[0]['State']!='available':raise RuntimeError('EC2 AMI 사용 불가')
    snapshots=rds.describe_db_snapshots(DBSnapshotIdentifier=RDS_POINT)['DBSnapshots']
    if len(snapshots)!=1 or snapshots[0]['Status']!='available' or snapshots[0]['DBInstanceIdentifier']!='team5-rds-mysql':
        raise RuntimeError('RDS 스냅샷 사용 불가/원본 불일치')
    for name in NAMES+('team5-recovery-workflow',):
        arn=f'arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:{name}'
        if sf.list_executions(stateMachineArn=arn,statusFilter='RUNNING',maxResults=1)['executions']:
            raise RuntimeError('실행 중인 작업이 있습니다: '+name)
    root=Path.home()/'team5-recovery'/'backup-reference-update'
    root.mkdir(parents=True,exist_ok=True)
    pointer=root/'latest.json'
    if mode=='prepare':
        folder=root/datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
        folder.mkdir()
        records={}
        for name in NAMES:
            arn=f'arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:{name}'
            current=sf.describe_state_machine(stateMachineArn=arn)
            before=json.loads(current['definition'])
            after=patch(before,name=='team5-web-response-core')
            response=sf.validate_state_machine_definition(definition=json.dumps(after),type='STANDARD')
            if response['result']!='OK':raise RuntimeError(json.dumps(response.get('diagnostics'),ensure_ascii=False))
            records[name]={'arn':arn,'role':current['roleArn'],'before':before,'after':after}
            save(folder/(name+'-before.json'),before)
            save(folder/(name+'-after.json'),after)
        save(folder/'manifest.json',records)
        save(pointer,{'folder':str(folder)})
        print('백업 원본/완료 상태/AMI/스냅샷 및 AWS 정의 검증 통과.')
        print('검토 및 백업 폴더:',folder)
        print('아직 AWS 설정은 변경하지 않았습니다.')
        print('배포: python3 ~/refresh-recovery-backups.py apply')
        return
    folder=Path(json.loads(pointer.read_text())['folder'])
    records=json.loads((folder/'manifest.json').read_text())
    if set(records)!=set(NAMES):raise RuntimeError('배포 대상 불일치')
    for name in NAMES:
        rec=records[name]
        if rec['arn']!=f'arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:{name}' or patch(rec['before'],name=='team5-web-response-core')!=rec['after']:
            raise RuntimeError('검토 파일이 예상과 다릅니다.')
        current=sf.describe_state_machine(stateMachineArn=rec['arn'])
        if current['roleArn']!=rec['role'] or json.loads(current['definition']) not in (rec['before'],rec['after']):
            raise RuntimeError('준비 후 다른 변경이 있습니다. 덮어쓰지 않습니다: '+name)
        response=sf.validate_state_machine_definition(definition=json.dumps(rec['after']),type='STANDARD')
        if response['result']!='OK':raise RuntimeError(json.dumps(response.get('diagnostics')))
    applied=[]
    for name in NAMES:
        rec=records[name]
        current=sf.describe_state_machine(stateMachineArn=rec['arn'])
        definition=json.loads(current['definition'])
        if current['roleArn']!=rec['role'] or definition not in (rec['before'],rec['after']):raise RuntimeError('배포 중 외부 변경: '+name)
        if definition!=rec['after']:
            sf.update_state_machine(stateMachineArn=rec['arn'],definition=json.dumps(rec['after']))
        for _ in range(20):
            if json.loads(sf.describe_state_machine(stateMachineArn=rec['arn'])['definition'])==rec['after']:break
            time.sleep(1)
        else:raise RuntimeError('배포 후 일치 확인 실패: '+name)
        applied.append(name);save(folder/'applied.json',applied)
        print('백업 연결 변경/재조회 확인:',name)
    print('완료: 백업 참조만 변경했습니다. 격리/복원/ALB 전환은 실행하지 않았습니다.')
    print('고정 백업 방식입니다. 최신 백업 자동 선택은 아닙니다.')
    print('RDS 여유, 실제 프록시 전환 검증은 남아 있습니다. execute는 아직 실행하지 마세요.')
    print('기존 배스천 CloudFormation 스택 재배포 시 이 백업 참조와 태그 변경을 함께 반영해야 합니다.')

if __name__=='__main__':main()
