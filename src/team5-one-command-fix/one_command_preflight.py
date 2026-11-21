"""CloudShell-only launch checks. Never isolate, restore, switch traffic, or delete resources."""
import json
from pathlib import Path
import subprocess
import sys

ACCOUNT='123456789012'
REGION='ap-northeast-2'
PREFIX=f'arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:'
WEB=PREFIX+'targetgroup/project-web-tg/0cc0a8e0ec60caec'
RECOVERY=PREFIX+'targetgroup/project-recovery-tg/097ceff4e9b210a0'
LISTENER=PREFIX+'listener/app/project-alb/10037eb6c994f7f5/14f140be8ffa4d1b'

def require(ok,msg):
    if not ok:raise RuntimeError('실행 전 점검 중단: '+msg+' (격리/복원 시작 안 함)')

def run_preflight(session):
    require(session.client('sts').get_caller_identity()['Account']==ACCOUNT,'계정 불일치')
    sf,ec2,rds,elb,backup=[session.client(n) for n in ('stepfunctions','ec2','rds','elbv2','backup')]
    definitions={}
    for name in ('ec2-isolation-workflow','team5-web-response-core','team5-recovery-workflow'):
        arn=f'arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:{name}'
        require(not sf.list_executions(stateMachineArn=arn,statusFilter='RUNNING',maxResults=1)['executions'],'다른 대응 실행 중')
        definitions[name]=json.loads(sf.describe_state_machine(stateMachineArn=arn)['definition'])
    for iid,sg in [('i-000000000000071f8','sg-00000000000001a89'),('i-0000000000000c871','sg-000000000000039bc')]:
        reservations=ec2.describe_instances(InstanceIds=[iid])['Reservations']
        data=[i for r in reservations for i in r['Instances']]
        require(len(data)==1 and data[0]['State']['Name']=='running' and {g['GroupId'] for g in data[0]['SecurityGroups']}=={sg},'원본 서버가 이미 격리됐거나 초기 상태가 아님: '+iid)
    actions=elb.describe_listeners(ListenerArns=[LISTENER])['Listeners'][0]['DefaultActions']
    require(len(actions)==1 and actions[0]['Type']=='forward' and actions[0].get('TargetGroupArn')==WEB,'현재 ALB가 기존 웹 서버를 서비스하지 않음. 현재 서비스를 자동으로 되돌리지 않음')
    groups=actions[0].get('ForwardConfig',{}).get('TargetGroups',[])
    require(not groups or (len(groups)==1 and groups[0]['TargetGroupArn']==WEB),'ALB 복수 대상 설정')
    targets=elb.describe_target_health(TargetGroupArn=WEB)['TargetHealthDescriptions']
    require(len(targets)==1 and targets[0]['Target']=={'Id':'i-000000000000071f8','Port':80} and targets[0]['TargetHealth']['State']=='healthy','기존 웹 대상 그룹 비정상')
    require(not elb.describe_target_health(TargetGroupArn=RECOVERY)['TargetHealthDescriptions'],'복구 대상 그룹이 비어 있지 않음')
    dbs=[db for page in rds.get_paginator('describe_db_instances').paginate() for db in page['DBInstances']]
    require(len(dbs)==1 and dbs[0]['DBInstanceIdentifier']=='team5-rds-mysql' and dbs[0]['DBInstanceStatus']=='available','이 프로젝트의 2개 DB 테스트 제한: 기존 DB 한 개만 남은 상태에서 시작해야 함. 자동 삭제 없음')
    parent=definitions['ec2-isolation-workflow']['States']
    core=definitions['team5-web-response-core']['States']
    require('PrepareBastion' in parent and 'TagOriginalIsolated' in core,'배스천/태그 연결 누락')
    recovery=definitions['team5-recovery-workflow']['States']['RunProxyRecovery']['Branches'][0]['States']
    require(recovery['VerifyListenerTarget']['Choices'][0]['Next']=='VerifyAlbService' and recovery['VerifyAlbServiceEvaluate']['Choices'][0]['Next']=='TagRecoveredActive','ALB 검증 후 active 태그 흐름 누락')
    request=core['PrepareNewRecoveryInput']['Parameters']
    for key,state,source,kind in [
        ('Ec2RecoveryPointArn','PreflightEc2Point',f'arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-000000000000071f8','EC2'),
        ('RdsRecoveryPointArn','PreflightRdsPoint',f'arn:aws:rds:{REGION}:{ACCOUNT}:db:team5-rds-mysql','RDS')]:
        point=request[key]
        for states in (parent,core):
            require(states[state]['Parameters']['RecoveryPointArn']==point,'부모/자식 백업 참조 불일치')
        record=backup.describe_recovery_point(BackupVaultName='team5-backup-vault-iac',RecoveryPointArn=point)
        require(record.get('Status')=='COMPLETED' and record.get('ResourceArn')==source and record.get('ResourceType')==kind,'백업 원본/완료 상태 확인 실패')
    ami=request['Ec2RecoveryPointArn'].rsplit('/',1)[1]
    for states in (parent,core):
        require(states['PreflightEc2Image']['Parameters']['ImageIds']==[ami],'AMI 검사 참조 불일치')
        require(states['PreflightRdsSnapshot']['Parameters']['DbSnapshotIdentifier']==request['RdsRecoveryPointArn'],'RDS 검사 참조 불일치')
    images=ec2.describe_images(ImageIds=[ami])['Images']
    require(len(images)==1 and images[0]['State']=='available','복원 AMI 사용 불가')
    snaps=rds.describe_db_snapshots(DBSnapshotIdentifier=request['RdsRecoveryPointArn'])['DBSnapshots']
    require(len(snaps)==1 and snaps[0]['Status']=='available','복원 RDS 스냅샷 사용 불가')
    helper=Path.home()/'team5-proxy-recovery-package'/'team5-proxy-recovery'/'proxy-recovery.py'
    require(helper.is_file(),'프록시 검사 도구 파일 없음')
    # Existing inspect action validates the proxy credentials/schema and rejects an active proxy transaction.
    result=subprocess.run([sys.executable,str(helper),'check'],capture_output=True,text=True)
    print(result.stdout,flush=True)
    require(result.returncode==0,'프록시 검사 실패. 단독 check 명령으로 상세 확인 필요')
    records=[]
    for line in result.stdout.splitlines():
        try:
            item=json.loads(line)
            if isinstance(item,dict) and item.get('Status')=='PROXY_INSPECT_OK':records.append(item)
        except json.JSONDecodeError:
            pass
    require(len(records)==1 and records[0].get('Backend')==dbs[0]['Endpoint']['Address'],'프록시가 기존 RDS를 바라보지 않음')
    print('실행 전 상태/백업/프록시 검사 통과. 통합 대응을 시작합니다.',flush=True)
