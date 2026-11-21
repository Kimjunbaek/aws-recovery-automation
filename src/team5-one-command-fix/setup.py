"""Install post-cutover HTTP verification and guarded one-command launcher. Never starts recovery."""
import argparse,base64,copy,json,re,time,hashlib
from pathlib import Path
from datetime import datetime,timezone
HERE=Path(__file__).resolve().parent
ACCOUNT='123456789012';REGION='ap-northeast-2'
WF=f'arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:team5-recovery-workflow'
DOC='team5-web-recovery-f60d67aa4a32'
VERSION_NAME='one-command-alb-check-20261026'

def require(ok,msg):
    if not ok:raise RuntimeError(msg)

def save(path,value):path.write_text(json.dumps(value,indent=2,ensure_ascii=False,default=str),encoding='utf-8')

def make_document(old,worker):
    d=copy.deepcopy(old)
    require(d['parameters']['Mode']['allowedValues'] in (['prepare','verify'],['prepare','verify','verify-alb']),'예상과 다른 SSM 모드')
    d['parameters']['Mode']['allowedValues']=['prepare','verify','verify-alb']
    payload=base64.b64encode(worker.encode()).decode()
    d['mainSteps'][0]['inputs']['runCommand']=["set -eu\numask 077\npython3 -c \"import base64; exec(compile(base64.b64decode('"+payload+"'), '<team5-worker>', 'exec'))\""]
    return d

def patch_definition(before,version):
    d=copy.deepcopy(before)
    s=d['States']['RunProxyRecovery']['Branches'][0]['States']
    require(s['VerifyProxyWordPress']['Parameters']['DocumentName']==DOC,'검증 문서가 예상과 다릅니다.')
    require(s['VerifyListenerTarget']['Choices'][0]['Next'] in ('TagRecoveredActive','VerifyAlbService'),'태그 전환 구조 불일치')
    require(s['SwitchDefaultTarget']['Next'] in ('ReadListenerAfterCutover','InitializeListenerRetry'),'ALB 전환 구조 불일치')
    s['VerifyProxyWordPress']['Parameters']['DocumentVersion']=version
    # Preserve automatic proxy switch/verification, health checks, and all other states.
    send=copy.deepcopy(s['VerifyProxyWordPress'])
    send['Parameters']['DocumentVersion']=version
    send['Parameters']['Parameters']['Mode']=['verify-alb']
    send['ResultPath']='$.VerifyAlbServiceCommand'
    send['Next']='VerifyAlbServiceInit'
    send['Catch']=[{'ErrorEquals':['States.ALL'],'ResultPath':'$.VerifyAlbServiceError','Next':'AlbServiceVerificationFailed'}]
    additions={
      'InitializeListenerRetry':{'Type':'Pass','Result':{'Attempts':0},'ResultPath':'$.ListenerRetry','Next':'WaitForListenerPropagation'},
      'WaitForListenerPropagation':{'Type':'Wait','Seconds':3,'Next':'ReadListenerAfterCutover'},
      'CountListenerRetry':{'Type':'Pass','Parameters':{'Attempts.$':'States.MathAdd($.ListenerRetry.Attempts, 1)'},'ResultPath':'$.ListenerRetry','Next':'VerifyListenerTarget'},
      'EvaluateListenerRetry':{'Type':'Choice','Choices':[{'Variable':'$.ListenerRetry.Attempts','NumericLessThan':20,'Next':'WaitForListenerPropagation'}],'Default':'CutoverVerificationFailed'},
      'VerifyAlbService':send,
      'VerifyAlbServiceInit':{'Type':'Pass','Result':{'Attempts':0},'ResultPath':'$.VerifyAlbServicePoll','Next':'VerifyAlbServiceWait'},
      'VerifyAlbServiceWait':{'Type':'Wait','Seconds':10,'Next':'VerifyAlbServiceRead'},
      'VerifyAlbServiceRead':{'Type':'Task','Resource':'arn:aws:states:::aws-sdk:ssm:getCommandInvocation',
        'Parameters':{'CommandId.$':'$.VerifyAlbServiceCommand.CommandId','InstanceId.$':'$.Configuration.InstanceId','PluginName':'ConfigureAndVerify'},
        'Retry':[{'ErrorEquals':['Ssm.InvocationDoesNotExist'],'IntervalSeconds':5,'MaxAttempts':6,'BackoffRate':1}],
        'Catch':[{'ErrorEquals':['States.ALL'],'ResultPath':'$.VerifyAlbServiceError','Next':'AlbServiceVerificationFailed'}],
        'ResultPath':'$.AlbServiceResult','Next':'VerifyAlbServiceCount'},
      'VerifyAlbServiceCount':{'Type':'Pass','Parameters':{'Attempts.$':'States.MathAdd($.VerifyAlbServicePoll.Attempts, 1)'},'ResultPath':'$.VerifyAlbServicePoll','Next':'VerifyAlbServiceEvaluate'},
      'VerifyAlbServiceEvaluate':{'Type':'Choice','Choices':[
        {'And':[{'Variable':'$.AlbServiceResult.Status','StringEquals':'Success'},{'Variable':'$.AlbServiceResult.ResponseCode','NumericEquals':0}],'Next':'TagRecoveredActive'},
        {'Variable':'$.VerifyAlbServicePoll.Attempts','NumericGreaterThanEquals':90,'Next':'AlbServiceVerificationFailed'},
        {'Or':[{'Variable':'$.AlbServiceResult.Status','StringEquals':v} for v in ('Pending','InProgress','Delayed','Cancelling')],'Next':'VerifyAlbServiceWait'}],'Default':'AlbServiceVerificationFailed'},
      'AlbServiceVerificationFailed':{'Type':'Fail','Error':'AlbServiceVerificationFailed','Cause':'ALB may already point to recovery. Inspect AlbServiceResult/SSM. Do not repeat isolation or automatically switch to an isolated original server.'},
    }
    for name,value in additions.items():
        if name in s:
            require(s[name]==value,'동일 이름의 다른 단계가 있습니다: '+name)
        s[name]=value
    s['SwitchDefaultTarget']['Next']='InitializeListenerRetry'
    s['ReadListenerAfterCutover']['Next']='CountListenerRetry'
    s['VerifyListenerTarget']['Choices'][0]['Next']='VerifyAlbService'
    s['VerifyListenerTarget']['Default']='EvaluateListenerRetry'
    return d

def local_changes(home):
    bastion=home/'team5-bastion-integration-package'/'team5-bastion-integration'
    proxy=home/'team5-proxy-recovery-package'/'team5-proxy-recovery'
    launcher=bastion/'run-response.py';helper=proxy/'proxy-recovery.py'
    require(launcher.is_file() and helper.is_file(),'기존 배포 패키지 파일이 없습니다.')
    a=launcher.read_text();b=helper.read_text()
    marker='    data = {"source": "project.security.detection"'
    addition='    from one_command_preflight import run_preflight\n    run_preflight(session)\n'
    if addition not in a:
        require(a.count(marker)==1,'실행 도구 구조 불일치')
        a=a.replace(marker,addition+marker)
    old="DocumentVersion='1', InstanceIds=[instance]"
    new="DocumentVersion=doc.get('version', '1'), InstanceIds=[instance]"
    if new not in b:
        require(b.count(old)==1,'복귀 도구 SSM 버전 구조 불일치')
        b=b.replace(old,new)
    anchor="    docs = json.loads((deployment / 'documents.json').read_text())\n"
    addition2='''    # Read the live pinned verification document instead of assuming version 1.
    live = json.loads(sf.describe_state_machine(stateMachineArn=WORKFLOW)['definition'])
    web = live['States']['RunProxyRecovery']['Branches'][0]['States']['VerifyProxyWordPress']['Parameters']
    docs['web'] = {'name': web['DocumentName'], 'version': web['DocumentVersion']}
'''
    if addition2 not in b:
        require(b.count(anchor)==1,'복귀 도구 문서 로드 구조 불일치')
        b=b.replace(anchor,anchor+addition2)
    import ast
    ast.parse(a);ast.parse(b)
    preflight=(HERE/'one_command_preflight.py').read_text(encoding='utf-8-sig');ast.parse(preflight)
    return {launcher:a,helper:b,bastion/'one_command_preflight.py':preflight}

def main():
    import boto3
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','apply']);mode=p.parse_args().mode
    session=boto3.Session(region_name=REGION)
    require(session.client('sts').get_caller_identity()['Account']==ACCOUNT,'계정 불일치')
    sf,ssm=session.client('stepfunctions'),session.client('ssm')
    def idle():
        for n in ('ec2-isolation-workflow','team5-web-response-core','team5-recovery-workflow'):
            require(not sf.list_executions(stateMachineArn=f'arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:{n}',statusFilter='RUNNING',maxResults=1)['executions'],'자동화 실행 중: '+n)
    idle()
    files=local_changes(Path.home())
    worker=(HERE/'web_worker.py').read_text(encoding='utf-8-sig')
    old=json.loads(ssm.get_document(Name=DOC,DocumentVersion='1',DocumentFormat='JSON')['Content'])
    require(old['mainSteps'][0]['name']=='ConfigureAndVerify','문서 플러그인 불일치')
    doc=make_document(old,worker)
    # VersionName makes deployment idempotent; default version is intentionally unchanged.
    existing=None
    for page in ssm.get_paginator('list_document_versions').paginate(Name=DOC):
        for v in page['DocumentVersions']:
            if v.get('VersionName')==VERSION_NAME:existing=v['DocumentVersion']
    if existing:
        require(json.loads(ssm.get_document(Name=DOC,DocumentVersion=existing,DocumentFormat='JSON')['Content'])==doc,'동일 문서 버전 이름의 내용이 다릅니다.')
    latest=ssm.describe_document(Name=DOC)['Document']['LatestVersion']
    version=existing or str(int(latest)+1)
    current=sf.describe_state_machine(stateMachineArn=WF)
    before=json.loads(current['definition'])
    live_task=before['States']['RunProxyRecovery']['Branches'][0]['States']['VerifyProxyWordPress']['Parameters']
    require(live_task['DocumentName']==DOC,'현재 문서 이름 불일치')
    live_doc=json.loads(ssm.get_document(Name=DOC,DocumentVersion=live_task['DocumentVersion'],DocumentFormat='JSON')['Content'])
    # Only known v2 (proxy SQL) or this exact new worker may be replaced.
    payload=re.search(r"base64\.b64decode\('([A-Za-z0-9+/=]+)'\)",live_doc['mainSteps'][0]['inputs']['runCommand'][0])
    require(payload is not None,'실행 문서 구조 불일치')
    source=base64.b64decode(payload.group(1)).decode().replace('\r\n','\n')
    reference=(HERE/'expected-v2-worker.py').read_text(encoding='utf-8-sig').replace('\r\n','\n')
    require(source in (reference,worker.replace('\r\n','\n')),'웹 검사 코드가 수집 기준과 다릅니다.')
    after=patch_definition(before,version)
    result=sf.validate_state_machine_definition(definition=json.dumps(after),type='STANDARD')
    require(result['result']=='OK',json.dumps(result.get('diagnostics'),ensure_ascii=False))
    folder=Path.home()/'team5-recovery'/('one-command-fix-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f'));folder.mkdir(parents=True)
    save(folder/'workflow-before.json',before);save(folder/'workflow-after.json',after);save(folder/'document.json',doc)
    local_before={}
    for index,(path,content) in enumerate(files.items()):
        local_before[path]=path.read_text() if path.exists() else None
        if local_before[path] is not None:(folder/f'local-{index}-before.py').write_text(local_before[path])
        (folder/f'local-{index}-after.py').write_text(content)
    save(folder/'local-paths.json',[str(x) for x in files])
    print('AWS 정의 검증 완료. 백업/검토 폴더:',folder)
    if mode=='prepare':
        print('AWS 설정 변경 및 실제 검사는 실행하지 않았습니다. 배포: python3 setup.py apply')
        return
    idle()
    live=sf.describe_state_machine(stateMachineArn=WF)
    require(json.loads(live['definition'])==before and live['roleArn']==current['roleArn'],'다른 작업자의 변경 발견')
    for path,content in local_before.items():require((path.read_text() if path.exists() else None)==content,'로컬 파일 동시 변경 발견')
    if not existing:
        require(ssm.describe_document(Name=DOC)['Document']['LatestVersion']==latest,'SSM 문서 버전 동시 변경 발견')
        version=ssm.update_document(Name=DOC,DocumentVersion=latest,VersionName=VERSION_NAME,DocumentFormat='JSON',Content=json.dumps(doc))['DocumentDescription']['DocumentVersion']
        after=patch_definition(before,version)
        result=sf.validate_state_machine_definition(definition=json.dumps(after),type='STANDARD')
        require(result['result']=='OK',json.dumps(result.get('diagnostics')))
    idle()
    live=sf.describe_state_machine(stateMachineArn=WF)
    require(json.loads(live['definition'])==before and live['roleArn']==current['roleArn'],'문서 생성 중 워크플로우 변경 발견')
    if before!=after:sf.update_state_machine(stateMachineArn=WF,definition=json.dumps(after))
    for attempt in range(20):
        if json.loads(sf.describe_state_machine(stateMachineArn=WF)['definition'])==after:break
        time.sleep(1)
    else:raise RuntimeError('배포 재조회 불일치')
    # Install dependency before its caller. Keep snapshots for partial deployment recovery.
    for path in sorted(files,key=lambda x:x.name!='one_command_preflight.py'):
        require((path.read_text() if path.exists() else None)==local_before[path],'로컬 파일 변경 발견; AWS는 이미 업데이트됐으므로 백업 폴더 확인')
        path.write_text(files[path],encoding='utf-8')
    save(folder/'applied.json',{'document':DOC,'version':version,'workflow':WF})
    print('배포 완료: 프록시 경유 검증 → ALB 전환 → 기본 웹 응답 3회 연속 확인 → active 태그')
    print('한 줄 실행 도구에 초기 상태/백업/프록시 사전 점검 추가 완료')
    print('복귀 도구의 웹 검사 문서 버전 자동 조회 수정 완료')
    print('현재 서비스, 보안 그룹, EC2/RDS는 변경하지 않았습니다. 격리/복원 실행 없음.')
    print('실행 명령은 동일합니다: python3 ~/team5-bastion-integration-package/team5-bastion-integration/run-response.py execute')
    print('현재는 복구 서비스 운영 중이므로 실행 사전 점검에서 차단되는 것이 정상입니다. 재테스트 전 초기 상태 복귀가 필요합니다.')
    print('실제 전체 자동화 성공은 아직 미검증입니다.')

if __name__=='__main__':main()
