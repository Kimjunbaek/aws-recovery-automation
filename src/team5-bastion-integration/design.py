"""AWS API 호출 없이 CloudFormation 및 상태 머신 정의를 생성합니다."""
import copy

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
PREFIX = "team5-bastion-integration"
PARENT = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:ec2-isolation-workflow"
CORE_NAME = "team5-web-response-core"
CORE = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:{CORE_NAME}"
PROJECT = PREFIX
BUCKET = f"{PREFIX}-{ACCOUNT}-{REGION}"
TABLE = PREFIX
OLD = "i-0000000000000c871"


def statement(actions, resources, condition=None):
    value = {"Effect": "Allow", "Action": actions, "Resource": resources}
    if condition:
        value["Condition"] = condition
    return value


def policy(statements):
    return {"Version": "2012-10-17", "Statement": statements}


def template(original, role_arn, bastion_role_arn=None):
    role_name = role_arn.rsplit("/", 1)[-1]
    project_arn = f"arn:aws:codebuild:{REGION}:{ACCOUNT}:project/{PROJECT}"
    bucket_arn = f"arn:aws:s3:::{BUCKET}"
    table_arn = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{TABLE}"
    build_statements = [
        statement(["ec2:Describe*"], "*", {"StringEquals": {"aws:RequestedRegion": REGION}}),
        statement(["ec2:RunInstances"], [
            f"arn:aws:ec2:{REGION}::image/ami-00000000000000cac",
            f"arn:aws:ec2:{REGION}:{ACCOUNT}:subnet/subnet-0000000000000960f",
            f"arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/sg-000000000000039bc",
            f"arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/sg-0000000000000bd52",
            f"arn:aws:ec2:{REGION}:{ACCOUNT}:key-pair/team5-key",
            *[f"arn:aws:ec2:{REGION}:{ACCOUNT}:{kind}/*" for kind in ("instance", "volume", "network-interface")]
        ], {"StringEquals": {"aws:RequestedRegion": REGION}, "StringEqualsIfExists": {"ec2:InstanceType": "t3.micro"}}),
        statement(["ec2:CreateTags"], [f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/*", f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/*"],
                  {"StringEquals": {"ec2:CreateAction": "RunInstances"}}),
        statement(["iam:PassRole"], bastion_role_arn or f"arn:aws:iam::{ACCOUNT}:role/team5-bastion-role",
                  {"StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}}),
        statement(["iam:GetRole"], bastion_role_arn or f"arn:aws:iam::{ACCOUNT}:role/team5-bastion-role"),
        statement(["iam:GetInstanceProfile"], f"arn:aws:iam::{ACCOUNT}:instance-profile/team5-bastion-role"),
        statement(["s3:ListBucket", "s3:GetBucketLocation", "s3:GetBucketVersioning", "s3:GetBucketAcl"], bucket_arn),
        statement(["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], bucket_arn + "/*"),
        statement(["s3:DeleteObject"], bucket_arn + "/terraform/*.tflock"),
        statement(["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"], table_arn),
        statement(["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                  f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/codebuild/{PROJECT}:*"),
        statement(["logs:FilterLogEvents"], f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/project/security/bastion-auth:*"),
        statement(["ssm:DescribeInstanceInformation", "ssm:GetCommandInvocation"], "*"),
        statement(["ssm:SendCommand"], f"arn:aws:ssm:{REGION}::document/AWS-RunShellScript"),
        statement(["ssm:SendCommand"], f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/*",
                  {"StringEquals": {"ssm:resourceTag/ManagedBy": PREFIX}}),
        {"Effect": "Deny", "Action": ["ec2:TerminateInstances", "ec2:StopInstances"], "Resource": "*"}
    ]
    parent_statements = [
        statement(["codebuild:StartBuild", "codebuild:BatchGetBuilds", "codebuild:StopBuild"], project_arn),
        statement(["states:StartExecution"], CORE),
        statement(["states:DescribeExecution", "states:StopExecution"], f"arn:aws:states:{REGION}:{ACCOUNT}:execution:{CORE_NAME}:*"),
        statement(["events:PutTargets", "events:PutRule", "events:DescribeRule"], [
            f"arn:aws:events:{REGION}:{ACCOUNT}:rule/StepFunctionsGetEventForCodeBuildStartBuildRule",
            f"arn:aws:events:{REGION}:{ACCOUNT}:rule/StepFunctionsGetEventsForStepFunctionsExecutionRule"
        ]),
        statement(["ec2:ModifyInstanceAttribute"], [
            f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{OLD}",
            {"Fn::Sub": f"arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/${{Quarantine}}"}
        ]),
        statement(["dynamodb:DeleteItem"], table_arn)
    ]
    resources = {
        "Storage": {
            "Type": "AWS::S3::Bucket", "DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain",
            "Properties": {
                "BucketName": BUCKET, "VersioningConfiguration": {"Status": "Enabled"},
                "BucketEncryption": {"ServerSideEncryptionConfiguration": [{"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]},
                "PublicAccessBlockConfiguration": {k: True for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")}
            }
        },
        "BucketPolicy": {"Type": "AWS::S3::BucketPolicy", "Properties": {
            "Bucket": {"Ref": "Storage"}, "PolicyDocument": policy([{
                "Effect": "Deny", "Principal": "*", "Action": "s3:*",
                "Resource": [bucket_arn, bucket_arn + "/*"],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}}
            }])
        }},
        "Lock": {"Type": "AWS::DynamoDB::Table", "DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain", "Properties": {
            "TableName": TABLE, "BillingMode": "PAY_PER_REQUEST",
            "AttributeDefinitions": [{"AttributeName": "LockId", "AttributeType": "S"}],
            "KeySchema": [{"AttributeName": "LockId", "KeyType": "HASH"}]
        }},
        "Quarantine": {"Type": "AWS::EC2::SecurityGroup", "Properties": {
            "GroupDescription": "Team5 operating bastion quarantine; no allowed ingress or egress",
            "VpcId": "vpc-000000000000021c2", "SecurityGroupIngress": [], "SecurityGroupEgress": [],
            "Tags": [{"Key": "Name", "Value": "team5-operating-bastion-quarantine"}]
        }},
        "BuildRole": {"Type": "AWS::IAM::Role", "Properties": {
            "RoleName": PREFIX + "-build-role",
            "AssumeRolePolicyDocument": policy([{
                "Effect": "Allow", "Principal": {"Service": "codebuild.amazonaws.com"},
                "Action": "sts:AssumeRole", "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT}, "ArnEquals": {"aws:SourceArn": project_arn}}
            }]),
            "Policies": [{"PolicyName": PREFIX, "PolicyDocument": policy(build_statements)}]
        }},
        "Build": {"Type": "AWS::CodeBuild::Project", "Properties": {
            "Name": PROJECT, "ServiceRole": {"Fn::GetAtt": ["BuildRole", "Arn"]},
            "ConcurrentBuildLimit": 1, "TimeoutInMinutes": 55, "QueuedTimeoutInMinutes": 10,
            "Artifacts": {"Type": "NO_ARTIFACTS"},
            "Source": {"Type": "S3", "Location": BUCKET + "/source/runtime.zip", "BuildSpec": "buildspec.yml"},
            "Environment": {"Type": "LINUX_CONTAINER", "ComputeType": "BUILD_GENERAL1_SMALL",
                "Image": "aws/codebuild/standard:7.0", "PrivilegedMode": False,
                "EnvironmentVariables": [
                    {"Name": "BUCKET", "Value": BUCKET}, {"Name": "LOCK_TABLE", "Value": TABLE},
                    {"Name": "QUARANTINE_SG", "Value": {"Ref": "Quarantine"}},
                    {"Name": "MODE", "Value": "UNSET"}, {"Name": "EXECUTION_ID", "Value": "UNSET"}
                ]}
        }},
        "WebCore": {"Type": "AWS::StepFunctions::StateMachine", "Properties": {
            "StateMachineName": CORE_NAME, "StateMachineType": "STANDARD", "RoleArn": role_arn,
            "Definition": original
        }},
        "ParentPermission": {"Type": "AWS::IAM::Policy", "Properties": {
            "PolicyName": PREFIX + "-orchestration", "Roles": [role_name],
            "PolicyDocument": policy(parent_statements)
        }}
    }
    return {"AWSTemplateFormatVersion": "2010-09-09", "Description": "Team5 bastion automation infrastructure; no instance creation or isolation at deployment",
            "Resources": resources, "Outputs": {"QuarantineSG": {"Value": {"Ref": "Quarantine"}}}}


def build_task(mode, next_state=None):
    task = {
        "Type": "Task", "Resource": "arn:aws:states:::codebuild:startBuild.sync", "TimeoutSeconds": 4000,
        "Parameters": {"ProjectName": PROJECT, "EnvironmentVariablesOverride": [
            {"Name": "MODE", "Value": mode, "Type": "PLAINTEXT"},
            {"Name": "EXECUTION_ID", "Value.$": "$$.Execution.Id", "Type": "PLAINTEXT"}
        ]}, "ResultSelector": {"BuildId.$": "$.Build.Id"}, "ResultPath": "$.bastion_build"
    }
    task.update({"Next": next_state} if next_state else {"End": True})
    return task


def wrapper(original, quarantine):
    if "PrepareBastion" in original["States"]:
        raise ValueError("이미 배스천 통합이 적용되어 있습니다.")
    # 입력/승인/백업 사전검사는 현재 정의를 그대로 재사용합니다.
    keep = {"ValidateTarget", "ChooseExecutionMode", "CheckManagementPlan", "PreviewOnly",
            "InvalidInput", "ManagementPlanRequired", "BackupPreflightNeeded", "BackupPreflightFailed"}
    keep.update(k for k in original["States"] if k.startswith("Preflight"))
    states = {k: copy.deepcopy(original["States"][k]) for k in keep}
    if states["PreflightRdsSnapshotReady"]["Choices"][0]["Next"] != "CheckExistingIsolation":
        raise ValueError("기존 백업 사전검사 구조가 변경되었습니다.")
    states["PreflightRdsSnapshotReady"]["Choices"][0]["Next"] = "PrepareBastion"
    if states["BackupPreflightNeeded"]["Default"] != "CheckExistingIsolation":
        raise ValueError("격리 전용 분기 구조가 변경되었습니다.")
    states["BackupPreflightNeeded"]["Default"] = "RunWebOnly"

    def webtask():
        return {"Type": "Task", "Resource": "arn:aws:states:::states:startExecution.sync:2", "TimeoutSeconds": 9500,
                "Parameters": {"StateMachineArn": CORE,
                    "Name.$": "States.Format('web-{}', States.UUID())", "Input.$": "$"},
                "ResultSelector": {"ExecutionArn.$": "$.ExecutionArn", "Status.$": "$.Status"},
                "ResultPath": "$.web_result", "End": True}

    def guarded(branch, prefix):
        return {"StartAt": prefix + "Run", "States": {
            prefix + "Run": {"Type": "Parallel", "Branches": [branch], "ResultPath": "$.result",
                "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.failure", "Next": prefix + "Failed"}],
                "Next": prefix + "Succeeded"},
            prefix + "Succeeded": {"Type": "Pass", "Parameters": {"Status": "SUCCEEDED", "Result.$": "$.result[0]"}, "End": True},
            prefix + "Failed": {"Type": "Pass", "Parameters": {"Status": "FAILED", "Failure.$": "$.failure"}, "End": True}
        }}
    bastion = {"StartAt": "IsolateOperatingBastion", "States": {
        "IsolateOperatingBastion": {"Type": "Task", "Resource": "arn:aws:states:::aws-sdk:ec2:modifyInstanceAttribute",
            "Parameters": {"InstanceId": OLD, "Groups": [quarantine]}, "ResultPath": None,
            "TimeoutSeconds": 60, "Next": "CreateAndVerifyBastion"},
        "CreateAndVerifyBastion": build_task("APPLY")
    }}
    states["RunWebOnly"] = webtask()
    states["PrepareBastion"] = build_task("PREPARE", "RespondInParallel")
    states["RespondInParallel"] = {"Type": "Parallel", "Branches": [
        guarded({"StartAt": "RunWeb", "States": {"RunWeb": webtask()}}, "WebBranch"), guarded(bastion, "BastionBranch")
    ], "ResultPath": "$.response_results", "Next": "ReleaseResponseLock"}
    states["ReleaseResponseLock"] = {
        "Type": "Task", "Resource": "arn:aws:states:::aws-sdk:dynamodb:deleteItem", "TimeoutSeconds": 60,
        "Parameters": {"TableName": TABLE, "Key": {"LockId": {"S": "response"}},
            "ConditionExpression": "#owner = :owner", "ExpressionAttributeNames": {"#owner": "Owner"},
            "ExpressionAttributeValues": {":owner": {"S.$": "$$.Execution.Id"}}},
        "ResultPath": None, "Next": "EvaluateResponses"
    }
    states["EvaluateResponses"] = {"Type": "Choice", "Choices": [{"And": [
        {"Variable": "$.response_results[0].Status", "StringEquals": "SUCCEEDED"},
        {"Variable": "$.response_results[1].Status", "StringEquals": "SUCCEEDED"}
    ], "Next": "ResponseCompleted"}], "Default": "ResponseFailed"}
    states["ResponseCompleted"] = {"Type": "Succeed"}
    states["ResponseFailed"] = {"Type": "Fail", "Error": "CombinedResponseFailed",
        "Cause": "Inspect response_results, child execution and CodeBuild logs. No automatic unquarantine or cleanup was performed."}
    return {"Comment": "Web response and operating bastion quarantine/recreation; fixed targets; no automatic rollback",
            "QueryLanguage": "JSONPath", "StartAt": original["StartAt"], "TimeoutSeconds": 14400, "States": states}
