# 검증 기록

## 당시 개발 기록

| 시점 | 기록 | 해석 범위 |
|---|---|---|
| 2026-10-23 | Bastion 구조·계획 검사 12개, SDK 요청 모델 검사 8개, Terraform validate·정적 검사 | 해당 시점의 정적/로컬 검증. 당시 문서의 미검증 항목을 이후 성공으로 일괄 변경하지 않음 |
| 2026-10-26 | 단일 명령으로 시작한 병렬 통합 대응 전체 성공, 26분 17초 | 한 차례 실환경 워크플로우 실행 시간. RTO·무중단·반복 성공률을 증명하지 않음 |
| 2026-10-28 | 개인 복구 자동화 작업 보고서 작성 | 역할·실행 기록을 정리한 근거 문서. 개인정보와 실환경 식별자가 포함된 원문은 공개 제외 |

## 공개본 재검증 — 2026-11-21

| 명령 | 관찰 결과 |
|---|---|
| `python -X utf8 -m unittest discover -s src/team5-bastion-integration -p test_design.py` | 12개 통과 |
| `python -X utf8 -m unittest discover -s src/team5-proxy-recovery -p test_package.py` | 17개 통과 |
| `python -X utf8 -m unittest discover -s src/team5-one-command-fix -p test_fix.py` | 8개 통과 |

Windows Python에서 UTF-8 옵션 없이 실행한 최초 보완 테스트는 fixture를 CP949로 읽으며 UnicodeDecodeError가 발생했다. `-X utf8`로 원본 Linux UTF-8 환경과 파일 해석을 맞춘 뒤 8개 통과했다. 제품 동작 코드는 이 문제로 변경하지 않았다.

클라우드 호출은 모의 객체를 사용하거나 구조 검사만 수행했다. 공개 정리 과정에서 격리·EC2/RDS 복원·ALB 전환을 재실행하지 않았다. 최초 소스와 별도 최종 보완 패치를 보존했으므로, 이 테스트가 최종 AWS 배포본과의 동일성을 보장하지는 않는다.
