> 개발 당시의 하위 패키지 문서입니다. 이후 보완과 최종 검증 범위는 저장소 최상위 README 및 docs/VERIFICATION.md를 먼저 확인하세요.

# 로컬 검증 기록

2026-10-23

- Python 문법 검사 통과.
- 상태 전이/원본 보존/기존 자원 삭제 및 교체 차단 등 12개 테스트 통과.
- 최초 배포 준비에서 AWS가 병렬 분기의 Run/Succeeded/Failed 이름 중복을 거부했습니다. 분기별 WebBranch/BastionBranch 접두사를 추가하고 전체 중첩 상태 이름 중복 검사와 회귀 테스트를 보강했습니다. 수정 후 AWS 서버 측 재검증은 사용자의 prepare 실행으로 확인해야 합니다.
- Terraform v1.16.5 / AWS provider v6.69.1에서 `terraform validate` 통과. 로컬 Windows provider를 사용했고 AWS 인증/API 호출은 하지 않았습니다.
- CodeBuild에서는 별도 Linux Terraform 1.13.5를 공식 체크섬 검증 후 사용합니다. Linux 실제 plan/apply는 미실행입니다.
- CloudFormation `cfn-lint` 서울 리전 정적 검사 통과.
- botocore 모델로 주요 SDK 요청 8개 형식 검사 통과.
- buildspec YAML 파싱과 API 정의 크기 검사 통과.
- AWS Step Functions 서버 측 정의 검증 및 CloudFormation ValidateTemplate는 CloudShell `setup.py prepare`에서 수행합니다.
- AWS 인프라 배포, 원본 배스천 격리, 신규 배스천 생성 및 실제 전체 통합 테스트는 수행하지 않았습니다.
- 실제 Prometheus 수집(up=1), SSH 로그인, 새 배스천에서 내부 서버로 접속하는 검증은 포함되지 않습니다.

사전 검사와 정적 검증은 계정의 IAM/SCP, 서비스 할당량, 용량, AMI 수명, 외부 패키지 다운로드 및 네트워크 연결 성공을 보장하지 않습니다.
