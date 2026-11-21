> 개발 당시의 하위 패키지 문서입니다. 이후 보완과 최종 검증 범위는 저장소 최상위 README 및 docs/VERIFICATION.md를 먼저 확인하세요.

# 로컬 검증 결과

- 날짜: 2026-10-23
- Python 구문 검사 통과
- 단위/모의 테스트 17개 통과
- botocore SDK 요청 형식 검사 9개 통과
- 상태 이름 중복 없음, 분기 내 참조 대상 존재 확인
- 각 그래프의 StartAt에서 모든 상태에 도달 가능함을 검사
- 기존 EC2/RDS 복원 및 ALB 변경 Task 보존 확인
- SSM 문서 64KB 및 상태 머신 1MB 크기 제한 확인
- 비밀번호는 수집 ZIP/생성 코드에 포함되지 않음

주요 모의 테스트: 조회 시 backend 쓰기 없음, 전환/복귀 시 옵션 보존, 다른 실행의 복귀 차단, 외부 backend 변경 덮어쓰기 차단, LOAD 실패 시 원상 보상, 원상 복귀 전 잠금 해제 차단, 재시도/재적용, 설정 계층 불일치 차단, 잘못된 hostname 차단.

Windows에서 Linux fcntl, 파일 소유권/권한과 AWS CLI/mysql 호출은 모의 처리했습니다. 실제 ProxySQL SQL 엔진 및 PHP 구문/실행, 실제 SSM, AWS 배포, RDS 복구, WordPress 접속, ALB 전환은 이 로컬 검증에 포함되지 않습니다.

AWS 정의 검증은 사용자가 CloudShell에서 setup.py prepare를 실행할 때 수행됩니다. apply 후 proxy-recovery.py check로 프록시 인증/구조를 확인합니다. 실제 변경 권한과 전체 경로는 이후 통합 실행에서 확인해야 합니다.

완성된 환경에 배포/테스트까지 완료됐다는 의미가 아닙니다.

사용자 AWS prepare 검증에서 발견된 ConfigurationFailed 도달 불가 오류를 수정했습니다. 기존 웹 설정 흐름 제거 시 해당 실패 상태도 함께 제거합니다. 로컬 참조 검사만으로는 이 오류를 잡지 못했으므로 그래프 도달 가능성 회귀 검사를 추가했습니다. 수정 후 원격 검증은 prepare 재실행으로 확인해야 합니다.
