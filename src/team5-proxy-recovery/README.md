> 개발 당시의 하위 패키지 문서입니다. 이후 보완과 최종 검증 범위는 저장소 최상위 README 및 docs/VERIFICATION.md를 먼저 확인하세요.

# 공유 ProxySQL을 사용하는 복구 자동화 수정본

현재 배포되지 않은 로컬 수정 패키지입니다. AWS 설정 검증, 프록시 준비 확인, 실제 복구 검증은 CloudShell에서 순서대로 진행합니다.

## 변경 목적

기존 문서 2번은 웹 `.env`를 새 RDS로 변경하고 컨테이너 환경변수로 DB를 검사했습니다. 그러나 실제 WordPress는 `wp-config.php`에 고정된 프록시 주소를 사용해 검증 대상과 서비스 연결 대상이 달라질 수 있었습니다.

이번 수정은 다음과 같이 동작합니다.

1. 공유 프록시 사용이므로 `Cutover=true`인 실행만 허용합니다. false/누락이면 새 EC2/RDS를 만들기 전에 복구 자식 워크플로우가 실패합니다. 부모의 격리가 이미 실행됐을 수 있으므로 실험용 false 입력을 격리 부모에 보내지 마세요.
2. 기존 ProxySQL 인증, SSM 실행 환경, 단일 backend/hostgroup 1, memory/runtime/disk 일치, 지원하지 않는 라우팅/스케줄러/클러스터 여부를 검사합니다.
3. 기존 절차대로 새 웹 EC2 및 새 RDS를 복원합니다. 매번 생성된 EC2 ID와 RDS 주소를 사용합니다.
4. 복구 웹의 `.env`는 `DB_HOST=192.0.2.242`로, `wp-config.php`의 DB_HOST는 `WORDPRESS_DB_HOST` 환경변수를 읽도록 정리합니다. 컨테이너 값은 `192.0.2.242:3306`이어야 합니다.
5. 원래 ProxySQL backend를 프록시의 root 전용 경로에 저장하고, 단일 hostgroup 1의 hostname을 새 RDS로 변경합니다. LOAD TO RUNTIME과 SAVE TO DISK를 수행합니다. 다른 서버 옵션은 보존합니다.
6. 실제 WordPress 설정을 읽어 접속한 DB의 `@@server_uuid`와 복구 RDS에 직접 접속해 읽은 UUID를 비교합니다. 웹에서 복구 RDS로 직접 접속할 권한/네트워크도 검증에 필요합니다. 이것은 실제 WordPress 경로를 우회하는 성공 판정을 방지합니다.
7. 웹 HTTP 200, 복구 대상 그룹 healthy 및 기존 ALB 전환 조건을 통과하면 기존 절차대로 ALB 기본 대상을 전환합니다.

공유 프록시의 DB 대상을 변경하는 시점부터 기존 웹을 포함한 모든 프록시 이용자에게 영향이 있습니다. ALB 전환 이전에 DB 연결 전환이 먼저 일어납니다. 무중단/세션 유지/기존 DB와 신규 DB의 데이터 병합은 제공하지 않습니다.

## 설치

ZIP을 CloudShell 홈에 업로드한 뒤:

```bash
unzip -n ~/team5-proxy-recovery.zip -d ~/team5-proxy-recovery-package
cd ~/team5-proxy-recovery-package/team5-proxy-recovery
python3 setup.py prepare
```

prepare는 원격 정의와 리소스를 읽고 AWS 정의 검증을 수행하며, before/after/docs/policy/manifest를 deployment 폴더에 저장합니다. AWS 리소스는 변경하지 않습니다. prepare가 성공하면:

```bash
python3 setup.py apply
python3 proxy-recovery.py check
```

apply는 다음만 변경하며 격리/복원/DB 전환/ALB 전환은 시작하지 않습니다.

- 코드 해시가 붙은 새로운 SSM 문서 2개 생성, 각각 버전 1 고정
- `StepFunctions-team5-recovery-workflow-role-aiqdz3kk2`에 추가 인라인 정책 1개
- `team5-recovery-workflow` 정의 업데이트

원본 `team5-configure-recovery-wordpress` 버전 2와 격리 Lambda/부모 워크플로우는 변경하지 않습니다. 프록시 IAM 역할도 변경하지 않습니다. 현재 프록시 역할의 AmazonSSMManagedInstanceCore에는 GetParameter 권한이 이미 포함되어 있습니다. 추가 복구 역할 정책도 기존의 광범위한 권한을 축소하지는 않습니다.

check는 SSM으로 프록시 검사 코드를 실행합니다. 비밀번호 조회와 임시 root 전용 인증 파일/로컬 작업 디렉터리는 사용하지만, backend UPDATE/LOAD/SAVE나 웹 변경은 하지 않습니다. 관리자 쓰기 권한은 실제 전환 전까지 검증되지 않습니다.

코드 수정 후에는 prepare부터 다시 실행합니다. 적용이 중간에 실패하면 새 문서/정책만 남을 수 있습니다. 원격 정의가 기존 revision과 같을 때만 재시도할 수 있습니다. 배포 실패가 실행 실패를 뜻하지는 않습니다.

## 실행 및 검증

기존 `ec2-isolation-workflow`가 `team5-recovery-workflow`를 호출하는 연결은 유지됩니다. 적용 이후 새로운 호출은 이 수정 로직을 사용합니다. 이 패키지는 새로운 사고 감지나 Bastion 통합을 추가하지 않습니다.

`team5-web-response-core`가 없다는 수집 오류는 실제 미배포가 확인됐습니다. 기존 Bastion 통합 패키지의 오래된 prepare 산출물을 적용하지 말고, 필요 시 그 패키지에서 prepare를 다시 실행해 변경 충돌 여부를 확인해야 합니다.

실제 실행 전에 확인할 사항:

- 실제 사용할 백업이 여전히 유효한지: 이 패키지는 백업 자동 최신 선택을 추가하지 않습니다.
- 새 RDS를 만들 수 있는 상태/한도인지.
- 복구 대상 그룹에 다른 테스트 서버가 남아 있지 않은지. 기존 로직은 전환 시 선택된 healthy 서버 한 대만 허용합니다.
- 원래 웹을 격리하면서 발생하는 서비스 중단과 공유 프록시 전환 영향에 대한 팀 합의.

결과 조회는 복구 **자식** 실행 ARN으로 합니다.

```bash
python3 proxy-recovery.py status '복구_자식_실행_ARN'
```

기존 `team5-recovery.py status`의 주요 출력 형식은 유지했습니다. 하지만 기존 스크립트의 rollback은 ALB만 되돌리므로 이번 수정 이후 실행에는 사용하지 마세요. 과거 launcher는 오래된 백업을 캐시할 수 있으므로 그 run 명령을 이 패키지의 신규 실행 명령으로 안내하지 않습니다.

## 실패 및 복귀

ProxySQL SQL 적용 자체가 실패하면 가능한 경우 원래 backend로 즉시 보상하고 잠금을 유지합니다. 그 이후 웹/ALB 검증 실패, Step Functions 수동 중단, 타임아웃에는 자동 전체 복귀하지 않습니다. 공유 DB 변경 후의 상태를 확인하지 않고 원래 서비스를 복원했다고 판단하지 않습니다.

프록시에는 실행 ARN 소유권과 변경 전 상태를 저장합니다:

```text
/var/lib/team5-proxy-recovery/active.json
/var/lib/team5-proxy-recovery/<실행 ARN SHA256>.json
```

성공 후에도 테스트 복귀 전까지 새 전환을 차단합니다. 잠금 파일을 임의로 지우거나 예전 실행을 redrive하지 마세요. 새 환경을 영구 운영 환경으로 승격하거나 연속 사고를 처리하는 기능은 이번 범위에 포함하지 않습니다.

시험 후 복귀:

```bash
python3 proxy-recovery.py rollback '복구_자식_실행_ARN'
```

이 명령은 다음을 확인합니다.

- 해당 실행/다른 복구 실행/관측된 SSM 명령이 진행 중이지 않음
- 실행 당시 ALB 기본 대상이 기존 웹 그룹이었다는 기록
- 기존 웹 `i-000000000000071f8`이 running, 정상 웹 SG만 연결, 기존 웹 TG에 등록
- 프록시 변경 기록의 소유권 및 외부 변경 여부

이후 임시 무작위 헤더 규칙으로 기존 TG를 사용 상태로 만들어 검사하고, 기존 RDS로 프록시 복귀 → 기존 웹의 실제 DB UUID/HTTP 검사 → healthy → ALB 복귀 → 프록시 잠금 해제 순으로 수행합니다. 기존 웹 검증 실패 시 ALB 변경 전이라면 프록시를 복구 RDS로 다시 돌립니다. ALB 변경 요청 이후 오류는 자동 추측 복귀하지 않고 잠금을 유지합니다. 임시 규칙은 종료 시 정리합니다. 실행 프로세스를 강제 종료했다면 rollback 폴더의 temporary-rule.txt로 잔존 규칙을 확인하세요.

원래 웹의 격리 해제, SG 변경, 대상 그룹 재등록은 자동 수행하지 않습니다. 실제 침해 여부 판단 후 담당자가 처리해야 합니다. EC2/RDS/백업도 삭제하지 않습니다.

원래 프록시 디스크/스냅샷과 CloudShell deployment/rollback 폴더는 복귀가 끝날 때까지 보존하세요. DB의 실제 쓰기 데이터 차이는 이 절차가 되돌리지 않습니다.

## 비밀 정보 및 전제 조건

- 관리자 비밀번호는 `/team5/recovery/proxysql/admin` SecureString에 유지합니다. 코드/SSM 파라미터/로그/명령 인자에 비밀번호를 넣지 않습니다.
- 프록시 EC2에서 AWS CLI, mysql CLI, Python3, 최신 SSM Agent의 ENV_VAR 지원과 Parameter Store 접근이 필요합니다.
- 지원 구조는 현재 제공된 단일 ProxySQL backend hostgroup 1입니다. 클러스터/스케줄러/다른 hostgroup 라우팅/쿼리 캐시는 확인 없이 변경하지 않습니다.
- 웹 구성은 `/home/ec2-user/wp/docker-compose.yml`, `.env`, `wp-wordpress-1`, 지속성 볼륨의 `/var/www/html/wp-config.php`입니다. 구성 차이가 있으면 실패로 중단합니다.
- 호스트와 컨테이너 백업 파일은 공개 웹 경로 밖에 저장합니다.

## 검증 범위

로컬 테스트와 SDK 검사는 AWS에 연결하지 않습니다. ProxySQL 동작은 모의 테스트이며 실제 SQL 엔진, PHP 실행, 실제 RDS/네트워크/ALB 성공을 대체하지 않습니다. 원격 AWS 정의 검증은 prepare에서, 실제 프록시 조회는 check에서 확인합니다. 실제 복구 성공은 실행 후에만 판단합니다.

참고: https://proxysql.com/documentation/backend-server-configuration/
참고: https://docs.aws.amazon.com/systems-manager/latest/userguide/documents-syntax-data-elements-parameters.html
