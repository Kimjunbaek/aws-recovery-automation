import base64
import copy
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ACCOUNT = '123456789012'
REGION = 'ap-northeast-2'
WORKFLOW = f'arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:team5-recovery-workflow'
PROXY = 'i-0000000000000a47b'
PROXY_IP = '192.0.2.242'
WEB_INSTANCE = 'i-000000000000071f8'
ELB_PREFIX = f'arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:'
LISTENER = ELB_PREFIX + 'listener/app/project-alb/10037eb6c994f7f5/14f140be8ffa4d1b'
WEB_TG = ELB_PREFIX + 'targetgroup/project-web-tg/0cc0a8e0ec60caec'
RECOVERY_TG = ELB_PREFIX + 'targetgroup/project-recovery-tg/097ceff4e9b210a0'
RDS_PATTERN = '^team5-rds-(mysql|restore-[a-z0-9-]+)\\.[a-z0-9]+\\.ap-northeast-2\\.rds\\.amazonaws\\.com$'


def parameter(pattern=None, default=None, values=None):
    result = {'type': 'String', 'interpolationType': 'ENV_VAR'}
    if pattern is not None:
        result['allowedPattern'] = pattern
    if default is not None:
        result['default'] = default
    if values is not None:
        result['allowedValues'] = values
    return result


def document(file, parameters, timeout):
    content = (HERE / file).read_bytes()
    payload = base64.b64encode(content).decode()
    # python -c leaves standard input available and avoids shell interpolation.
    script = "set -eu\numask 077\npython3 -c \"import base64; exec(compile(base64.b64decode('" + payload + "'), '<team5-worker>', 'exec'))\""
    return {'schemaVersion': '2.2', 'description': 'Team5 proxy-aware recovery; versioned worker',
            'parameters': parameters, 'mainSteps': [{
                'action': 'aws:runShellScript', 'name': 'ConfigureAndVerify',
                'precondition': {'StringEquals': ['platformType', 'Linux']},
                'inputs': {'timeoutSeconds': str(timeout), 'runCommand': [script]}
            }]}


def documents():
    proxy = document('proxy_worker.py', {
        'Action': parameter(values=['inspect', 'switch', 'restore', 'resume', 'release']),
        'Owner': parameter('^arn:aws:states:ap-northeast-2:123456789012:execution:team5-recovery-workflow:[A-Za-z0-9_-]+$'),
        'TargetHost': parameter('^$|' + RDS_PATTERN, default='')
    }, 180)
    web = document('web_worker.py', {
        'Mode': parameter(values=['prepare', 'verify']),
        'RestoreDbHost': parameter(RDS_PATTERN)
    }, 600)
    result = {}
    for kind, doc in [('proxy', proxy), ('web', web)]:
        suffix = hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:12]
        result[kind] = {'name': 'team5-' + kind + '-recovery-' + suffix, 'content': doc, 'version': '1'}
    return result


def command(states, prefix, doc, instance, params, next_state, fail_state, result_path=None):
    dynamic = instance.startswith('$')
    states[prefix] = {
        'Type': 'Task', 'Resource': 'arn:aws:states:::aws-sdk:ssm:sendCommand',
        'Parameters': {
            'DocumentName': doc['name'], 'DocumentVersion': '1',
            ('InstanceIds.$' if dynamic else 'InstanceIds'): ('States.Array(' + instance + ')' if dynamic else [instance]),
            'Parameters': params, 'TimeoutSeconds': 120,
        }, 'ResultSelector': {'CommandId.$': '$.Command.CommandId'},
        'ResultPath': '$.' + prefix + 'Command', 'Next': prefix + 'Init'
    }
    states[prefix + 'Init'] = {'Type': 'Pass', 'Result': {'Attempts': 0}, 'ResultPath': '$.' + prefix + 'Poll', 'Next': prefix + 'Wait'}
    states[prefix + 'Wait'] = {'Type': 'Wait', 'Seconds': 10, 'Next': prefix + 'Read'}
    path = result_path or '$.' + prefix + 'Result'
    states[prefix + 'Read'] = {
        'Type': 'Task', 'Resource': 'arn:aws:states:::aws-sdk:ssm:getCommandInvocation',
        'Parameters': {'CommandId.$': '$.' + prefix + 'Command.CommandId',
                       ('InstanceId.$' if dynamic else 'InstanceId'): instance,
                       'PluginName': 'ConfigureAndVerify'},
        'Retry': [{'ErrorEquals': ['Ssm.InvocationDoesNotExist'], 'IntervalSeconds': 5, 'MaxAttempts': 6, 'BackoffRate': 1}],
        'ResultPath': path, 'Next': prefix + 'Count'
    }
    states[prefix + 'Count'] = {'Type': 'Pass', 'Parameters': {'Attempts.$': 'States.MathAdd($.' + prefix + 'Poll.Attempts, 1)'},
                               'ResultPath': '$.' + prefix + 'Poll', 'Next': prefix + 'Evaluate'}
    states[prefix + 'Evaluate'] = {
        'Type': 'Choice', 'Choices': [
            {'And': [{'Variable': path + '.Status', 'StringEquals': 'Success'},
                     {'Variable': path + '.ResponseCode', 'NumericEquals': 0}], 'Next': next_state},
            # Do not launch rollback while a possibly delayed remote command can still run.
            # A nonterminal command past the bound fails into manual intervention.
            {'Variable': '$.' + prefix + 'Poll.Attempts', 'NumericGreaterThanEquals': 90, 'Next': fail_state},
            {'Or': [{'Variable': path + '.Status', 'StringEquals': s} for s in ['Pending', 'InProgress', 'Delayed', 'Cancelling']], 'Next': prefix + 'Wait'}
        ], 'Default': fail_state
    }


def build(before, docs):
    if before.get('StartAt') == 'RequireProxyCutover':
        raise ValueError('Already proxy-aware; use saved deployment instead of patching twice')
    branch = copy.deepcopy(before)
    states = branch['States']
    if states['ConfigureWordPress']['Parameters']['DocumentName'] != 'team5-configure-recovery-wordpress':
        raise ValueError('Unexpected current WordPress task')
    for name in ('ConfigureWordPress', 'WaitForCommand', 'CheckCommand', 'EvaluateCommand', 'ConfigurationFailed'):
        del states[name]
    states['EvaluateSsm']['Choices'][0]['Next'] = 'PrepareProxyWeb'
    states['ProxyStepFailed'] = {'Type': 'Fail', 'Error': 'ProxyRecoveryNeedsReview',
                                'Cause': 'Inspect SSM command results. Shared proxy may have changed; use proxy-recovery.py rollback after all commands are terminal.'}
    command(states, 'PrepareProxyWeb', docs['web'], '$.Configuration.InstanceId',
            {'Mode': ['prepare'], 'RestoreDbHost.$': 'States.Array($.Configuration.RestoreDbHost)'},
            'SwitchProxyBackend', 'ProxyStepFailed')
    command(states, 'SwitchProxyBackend', docs['proxy'], PROXY,
            {'Action': ['switch'], 'Owner.$': 'States.Array($$.Execution.Id)',
             'TargetHost.$': 'States.Array($.Configuration.RestoreDbHost)'},
            'VerifyProxyWordPress', 'ProxyStepFailed')
    command(states, 'VerifyProxyWordPress', docs['web'], '$.Configuration.InstanceId',
            {'Mode': ['verify'], 'RestoreDbHost.$': 'States.Array($.Configuration.RestoreDbHost)'},
            'RegisterRecoveryTarget', 'ProxyStepFailed', '$.CommandResult')
    # One managed recovery at a time; an old target must be explicitly removed by its owner.
    branch.pop('TimeoutSeconds', None)  # Parallel branches do not support a root timeout.
    branch.pop('Comment', None)
    branch.pop('QueryLanguage', None)
    root = {
        'Comment': 'Team5 shared ProxySQL recovery. Actual WordPress DB verification; explicit guarded rollback.',
        'QueryLanguage': 'JSONPath', 'StartAt': 'RequireProxyCutover', 'TimeoutSeconds': 10800,
        'States': {
            'RequireProxyCutover': {'Type': 'Choice', 'Choices': [{'And': [
                {'Variable': '$.Cutover', 'IsPresent': True},
                {'Variable': '$.Cutover', 'IsBoolean': True},
                {'Variable': '$.Cutover', 'BooleanEquals': True}
            ], 'Next': 'ProxyInspect'}], 'Default': 'SharedProxyRequiresCutover'},
            'SharedProxyRequiresCutover': {'Type': 'Fail', 'Error': 'SharedProxyRequiresCutover',
                                          'Cause': 'Cutover=false cannot safely test a new RDS through the shared production proxy. Use a separate proxy for isolated tests.'},
            'ProxyInspectionFailed': {'Type': 'Fail', 'Error': 'ProxyPreflightFailed', 'Cause': 'Inspect ProxyInspectResult; no recovery requested by this execution.'},
            'ReadInitialListener': {'Type': 'Task', 'Resource': 'arn:aws:states:::aws-sdk:elasticloadbalancingv2:describeListeners',
                                    'Parameters': {'ListenerArns': [LISTENER]}, 'ResultPath': '$.ProxyListenerBefore', 'Next': 'RunProxyRecovery'},
            'RunProxyRecovery': {'Type': 'Parallel', 'Branches': [branch], 'OutputPath': '$[0]', 'End': True,
                                 'Catch': [{'ErrorEquals': ['States.ALL'], 'ResultPath': '$.ProxyRecoveryFailure', 'Next': 'ProxyRecoveryFailed'}]},
            'ProxyRecoveryFailed': {'Type': 'Fail', 'Error': 'ProxyRecoveryFailed',
                                    'Cause': 'No automatic proxy/ALB rollback: shared DB changes affect all clients. Inspect child state/SSM and run guarded rollback for this execution. EC2/RDS preserved.'}
        }
    }
    command(root['States'], 'ProxyInspect', docs['proxy'], PROXY,
            {'Action': ['inspect'], 'Owner.$': 'States.Array($$.Execution.Id)'},
            'ReadInitialListener', 'ProxyInspectionFailed')
    return root


def policy(docs):
    return {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': 'ssm:SendCommand', 'Resource': [
            f'arn:aws:ssm:{REGION}:{ACCOUNT}:document/' + value['name'] for value in docs.values()
        ] + [f'arn:aws:ec2:{REGION}:{ACCOUNT}:instance/*']},
        {'Effect': 'Allow', 'Action': 'ssm:GetCommandInvocation', 'Resource': '*',
         'Condition': {'StringEquals': {'aws:RequestedRegion': REGION}}}
    ]}
