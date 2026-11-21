> 개발 당시의 하위 패키지 문서입니다. 이후 보완과 최종 검증 범위는 저장소 최상위 README 및 docs/VERIFICATION.md를 먼저 확인하세요.

# 운영 배스천 격리와 Terraform 재생성 연결

## 적용 범위

- 계정: `123456789012`, 리전: `ap-northeast-2`
- 격리할 운영 배스천: `team5-bastion` / `i-0000000000000c871`
- 함께 대응하는 웹 서버: `i-000000000000071f8`
- 기존 테스트 배스천 `i-0000000000000216c`는 수정·삭제·import하지 않습니다.
- 새 배스천: `team5-bastion-auto-recovery`, 별도 Terraform 상태에서 신규 EC2 한 대만 관리합니다.
- 기존 배스천 SG, 메트릭 SG, 키 페어, 인스턴스 프로파일, CloudWatch IAM 정책은 재사용하며 소유권을 가져오지 않습니다.
- 운영 배스천의 계정/프로그램을 백업에서 복원하는 것이 아니라, 기존에 작성한 AMI+bootstrap 기반 환경을 재생성합니다. 사용자 파일·추가 설치 프로그램·SSH 호스트 키·IP가 동일해지는 것은 아닙니다.

## 실행 구조

```text
ec2-isolation-workflow (기존 진입점 유지)
  입력/실행 옵션/관리 계획/기존 EC2·RDS 백업 확인
  CodeBuild PREPARE
    실행 잠금 → 운영 배스천/웹 상태 확인 → AMI·공유 자원 확인
    Terraform init/validate/plan → 신규 EC2 한 대 생성인지 검사
    EC2 DryRun 권한 검사 → 계획 저장
  병렬 분기
    ├─ team5-web-response-core
    │    기존 웹 격리 정의를 그대로 실행
    │    → team5-recovery-workflow → 웹 EC2·RDS 복원/기존 검증/ALB 전환
    └─ 운영 배스천 SG를 전용 격리 SG 하나로 교체
         → CodeBuild APPLY → 저장된 Terraform 계획 적용
         → 새 배스천 SSM/초기 설정/서비스/node_exporter/CloudWatch 검사
  두 분기 종료 → 실행 잠금 해제 → 모두 성공한 경우만 전체 성공
```

병렬 실행은 정확히 같은 밀리초에 수행되거나 원자적으로 실행된다는 뜻이 아닙니다.
한 분기가 실패해도 다른 분기의 복구는 끝까지 진행하도록 일반 작업 오류를 수집합니다.
따라서 웹 전환은 성공했는데 배스천만 실패하거나 반대 상황이 가능합니다. 전체 `FAILED`만 보고 두 분기가 모두 실패했다고 판단하면 안 됩니다.
사용자의 실행 중단, 전체 시간 초과, 일부 비포착 런타임 오류에는 잠금이 남을 수 있습니다.

`execute_isolation=false`는 기존 미리보기입니다.
`execute_recovery_test=false`는 기존 웹 격리 전용 경로이며 배스천에는 손대지 않습니다.
두 플래그와 `confirm_management_plan`이 모두 true인 전체 대응 경로에서 배스천 연동이 동작합니다.
기존 EventBridge가 이 부모를 호출한다면 배포 후 해당 입력에도 적용됩니다. 이 패키지는 탐지 규칙이나 EventBridge 연결을 새로 만들지 않습니다.

## 격리 방식

전용 SG의 인바운드·아웃바운드 허용 규칙을 모두 제거합니다.
기존 운영 배스천은 삭제·중지하지 않으며 ENI/IP/디스크를 보존합니다.
운영 배스천 SSH 및 SSM 관리 접속도 제한됩니다. 새 서버 생성은 CloudShell 또는 기존 배스천 내부가 아니라 CodeBuild에서 수행합니다.
SG 변경만으로 이미 추적 중인 TCP/SSH 연결의 즉시 종료는 보장되지 않으며 AWS DNS 등 SG로 차단할 수 없는 트래픽도 있습니다. 완전한 통신 단절 또는 메모리 증거 보존이 필요한 경우 별도 사고 대응 정책이 필요합니다.
실패했다고 자동으로 격리를 풀거나 서버를 삭제하지 않습니다.

## 설치 (CloudShell)

ZIP을 프로젝트 계정 CloudShell에 업로드합니다. 아래는 bash용입니다.

```bash
unzip -n ~/team5-bastion-integration.zip -d ~/team5-bastion-integration-package
cd ~/team5-bastion-integration-package/team5-bastion-integration
python3 setup.py prepare
```

`prepare`는 최신 부모 정의/AMI/공유 자원을 조회하고 AWS의 정의 검증을 실행합니다.
`deployment/before.json`, `preview.json`, `infrastructure.json`, `manifest.json`을 저장합니다.
인프라 생성, IAM 수정, 격리, 서버 생성은 없습니다.

준비 결과를 확인한 뒤 배포합니다.

```bash
python3 setup.py apply
```

배포 단계에서 생성/변경되는 항목:

1. 전용 S3 버킷(소스, 계획, 결과, 신규 Terraform 상태 저장), 버킷 정책
2. DynamoDB 실행 잠금 테이블
3. 운영 배스천 전용 격리 SG (생성만 하며 아직 연결하지 않음)
4. CodeBuild 프로젝트와 실행 IAM 역할
5. `team5-web-response-core` 상태 머신: 배포 직전 웹 격리 정의의 복사본
6. 기존 부모 실행 역할에 전용 추가 정책
7. 기존 `ec2-isolation-workflow` 정의를 병렬 대응 진입점으로 변경

이 단계는 EC2를 생성하거나 격리하지 않습니다. CodeBuild/S3 등 자동 실행 기반이 추가됩니다.
기존 `team5-recovery-workflow`, Lambda 코드, WordPress SSM 문서는 변경하지 않습니다.
다른 팀원이 부모 정의를 변경하면 revision 재확인으로 중단합니다. AWS UpdateStateMachine에는 원자적 revision 조건 갱신 인자가 없으므로, 배포 중 공동 편집을 피해야 합니다.

## 실제 실행

기존 서비스가 이미 복원 서버에서 동작하거나 원본 웹/배스천이 격리 상태라면 재실행하지 않습니다.
RDS 추가 생성 여유, 원본 웹/배스천 상태, 웹 복구의 프록시 설정을 확인한 뒤 실행합니다.

```bash
python3 run-response.py execute
```

위 명령은 실제 운영 웹과 배스천을 격리하고 EC2/RDS/배스천 생성 및 기존 ALB 전환 절차를 시작합니다.
한 번 배포한 뒤 전체 실행을 시작하는 명령은 이 한 줄입니다.

```bash
python3 run-response.py status
```

웹 결과는 부모의 `response_results[0]` 및 연결된 자식 실행에서 확인합니다.
배스천 결과는 `response_results[1]`, CodeBuild 로그, S3 `runs/<실행 ARN SHA256>/result.json`에서 확인합니다.
생성 직후 검증 실패 시에도 `created.json`과 Terraform 상태에 새 서버 ID가 남습니다.

## 새 배스천 검증 범위

- SSM Online, cloud-init 완료 및 초기 설정 성공 표시
- SSM/rsyslog/CloudWatch Agent/node_exporter 실행 및 부팅 자동 실행 상태
- SSH 설정 문법
- 로컬 `9100/metrics` 응답
- 고유 인증 로그를 생성하고 CloudWatch `/project/security/bastion-auth`의 `<새 인스턴스 ID>/secure`에서 실제 수신 확인

Prometheus 서버의 `up=1`은 자동 확인하지 않습니다. 기존 태그 기반 EC2 탐색 설정이 모니터링 서버에 적용돼 있어야 새 서버를 수집합니다.
새 배스천의 실제 SSH 로그인과 내부 웹 서버로의 접속까지 검증하는 것은 별도 확인입니다.
`Monitoring=team5-bastion` 태그와 기존 9100 접근 SG는 유지합니다.

## 중복 실행·재시도

전체 대응용 DynamoDB 잠금과 Terraform S3 상태 잠금을 별도로 사용합니다.
S3 상태 경로는 새 자동화 전용 `terraform/bastion.tfstate`이고, 기존 CloudShell 테스트 상태를 복사하지 않습니다.
신규 생성 1대만 허용하며 기존 관리 자원의 변경/교체/삭제/no-op 계획은 PREPARE에서 차단합니다.
따라서 성공 후 다시 명령을 입력한다고 배스천이 무한히 생성되지 않습니다. 이미 생성된 자동화 배스천이 있으면 다음 사고의 대상 승계/정리 정책을 먼저 결정해야 합니다.
현재 격리 대상은 원래 운영 배스천 ID로 고정됩니다. 새 배스천으로 대상 ID가 자동 승계되는 기능은 포함하지 않습니다.

실행 중단/시간 초과로 잠금이 남았다면 부모·자식 실행 및 CodeBuild가 모두 종료됐는지 먼저 확인합니다.
자동 잠금 만료나 강제 잠금 해제는 제공하지 않습니다. 실패한 계획을 임의로 재적용하거나 state를 삭제하지 않습니다.
APPLY 중 일부 생성에 성공했다면 생성 자원과 state를 보존하고 확인합니다.

## 연결만 되돌리기

```bash
python3 setup.py disconnect
```

실행 중인 작업이 없고 배포 후 정의가 바뀌지 않았을 때, 부모 정의를 `before.json`으로 되돌립니다.
이 명령은 격리 SG를 풀거나 ALB를 복귀시키거나 생성 자원을 삭제하지 않습니다.
CloudFormation 스택을 삭제해도 S3/잠금 테이블은 Retain이며, 실제 격리/새 EC2의 원복이 되지 않습니다.

## 현재 웹 복구와의 관계

첨부된 웹 복구는 여전히 `team5-configure-recovery-wordpress` 버전 2를 호출합니다.
이 배스천 패키지는 DB 엔드포인트 덮어쓰기/프록시 전환을 수정하지 않습니다.
프록시 담당자의 변경과 SSM 문서 버전 연결을 별도로 정리한 뒤 전체 사고 대응을 테스트해야 합니다.
고정된 EC2/RDS 복구 시점과 기존 웹 ALB 동작도 유지됩니다. 백업 자동 최신 선택은 포함하지 않습니다.
격리 전 검사가 성공해도 이후 AWS 용량, 할당량, 패키지 다운로드, SSM 통신까지 성공한다고 보장하지 않습니다.

## 파일 역할

| 파일 | 역할 |
|---|---|
| `setup.py` | 최신 AWS 구성 조회, 배포, 부모 연결 원복 |
| `design.py` | CloudFormation/병렬 상태 머신 정의 생성 |
| `run-response.py` | 전체 실행 시작/결과 조회 |
| `runtime/worker.py` | CodeBuild 사전 계획 및 배스천 생성·검증 |
| `runtime/terraform/main.tf` | 신규 배스천 EC2 한 대의 Terraform 정의 |
| `runtime/terraform/bootstrap*.sh` | 제공받은 계정/SSH/SSM/로그/node_exporter 초기 설정 |
| `reference-parent.json` | 첨부한 원본 정의, 로컬 테스트용. 실제 배포는 최신 AWS 정의를 조회 |
| `test_design.py` | 네트워크 호출 없는 구조/생성 계획 차단 테스트 |

## 검증 및 공식 근거

로컬에서 Python 문법, 상태 전이 연결, 기존 정의 보존, 잘못된 생성 계획 차단을 검사합니다.
AWS 정의 검증과 실제 IAM/서비스 실행은 CloudShell `prepare` 이후 사용자 계정에서 확인해야 합니다.
실제 격리/신규 생성/통합 테스트는 아직 수행하지 않았습니다.

- [Step Functions와 CodeBuild 통합](https://docs.aws.amazon.com/step-functions/latest/dg/connect-codebuild.html)
- [Terraform S3 상태 및 use_lockfile](https://developer.hashicorp.com/terraform/language/backend/s3)
- [보안 그룹 연결 추적](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/security-group-connection-tracking.html)
- [CodeBuild S3 소스 버전 지정](https://docs.aws.amazon.com/codebuild/latest/APIReference/API_UpdateProject.html)
