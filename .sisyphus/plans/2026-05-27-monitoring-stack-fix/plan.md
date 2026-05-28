# Plan: 모니터링 스택 메트릭·로깅 복구 (별도 VM 토폴로지)

- **작성일:** 2026-05-27
- **상태:** DRAFT (승인 대기)
- **Phase:** P2 (분석/관측)
- **브랜치:** `fix/monitoring-metrics-logging` (dev 기준 분기)

## 배경 — 실측 결과 (2026-05-27)

모니터링은 **별도 VM**(`localbiz-monitoring` 10.178.0.4)에서 동작. 같은 호스트 가정으로 짜인 설정이 VM 분리로 끊김.

| 신호 | 실측 상태 | 근거 |
|---|---|---|
| 메트릭 | ❌ 끊김 | Prometheus 타깃 `host.docker.internal:8000` → 모니터링 VM의 `172.17.0.1:8000`, connection refused (target down). 백엔드 `/metrics`는 정상(200, 메트릭 5종) |
| 로깅 | ❌ 끊김 | API VM에 promtail 미기동(`promtail=NONE`), Loki labels 비어있음 |
| 트레이싱 | ✅ 정상 | `JAEGER_HOST=SET`, Jaeger services에 `localbiz-api` 존재 — **수정 대상 아님** |
| 대시보드 | ⚠️ 메트릭 전용 | `fastapi.json`에 Loki/Jaeger 패널 0 |

VM IP: api `10.178.0.3` / monitoring `10.178.0.4` / opensearch `10.178.0.2` (모두 asia-northeast3-a, VPC 내부).

## 목표 & 성공 기준

1. **메트릭** → verify: 모니터링 VM에서 `curl -s localhost:9091/api/v1/targets`의 `localbiz-backend` health == `up`, Grafana FastAPI 대시보드 패널에 데이터 표시.
2. **로깅** → verify: 모니터링 VM에서 `curl localhost:3100/loki/api/v1/labels`에 `job`/`container` 라벨 존재, Grafana Explore에서 `{job="localbiz-api"}` 로그 조회됨.
3. **대시보드(선택)** → verify: Grafana에 로그 패널 1개 추가되어 `localbiz-api` 로그 표시.

비목표: 트레이싱(정상), Jaeger `unknown_service` 정리(경미, 별건).

## 작업 단계

### Step 1 — 메트릭: Prometheus 타깃 수정
- 파일: `backend/monitoring/prometheus.yml`
- `targets: ['host.docker.internal:8000']` → `['10.178.0.3:8000']`
- `host.docker.internal:8000` 의존이 사라지므로 `docker-compose.monitoring.yml`의 prometheus `extra_hosts: host.docker.internal:host-gateway` 제거.
- 주석 갱신(별도 VM 토폴로지 명시).
- verify: 변경 배포 후 타깃 health up.

### Step 2 — 메트릭: 방화벽 (모니터링 → API:8000)
- GCP 방화벽 규칙: source `10.178.0.4` (또는 VPC 서브넷) → target `localbiz-api` tag, tcp:8000 허용.
- 확인 명령(실측 후 결정):
  `gcloud compute firewall-rules list --project project-e9a9c8f2-70da-411d-a51 --format="table(name,sourceRanges.list(),allowed[].map().firewall_rule().list(),targetTags.list())"`
- 규칙 없으면 생성:
  `gcloud compute firewall-rules create allow-prometheus-scrape --network=<NET> --direction=INGRESS --action=ALLOW --rules=tcp:8000 --source-ranges=10.178.0.0/24 --target-tags=<api-tag>`
- ⚠️ 백엔드 컨테이너가 호스트 8000에 publish 되어 있는지 확인(이미 /metrics가 localhost:8000에서 200 → publish 됨 추정). 호스트 바인딩이 `127.0.0.1:8000`이면 `0.0.0.0:8000`으로 변경 필요(docker-compose api ports 확인).

### Step 3 — 로깅: promtail 기동 + LOKI_HOST 주입
- `docker-compose.yml`의 promtail 서비스는 이미 존재(`profiles: ["monitoring"]`, `LOKI_HOST`/`PROMTAIL_ENV` env).
- 배포 워크플로 `deploy-dev.yml` 수정:
  - 시크릿/env 블록(L55~)에 `LOKI_HOST=10.178.0.4`, `PROMTAIL_ENV=production`(또는 dev) 추가 → `.env` 병합.
  - 기동 단계(L108) 다음에 promtail 기동 추가:
    `docker compose --profile monitoring up -d --no-deps promtail`
- verify: API VM `docker ps`에 `localbiz-promtail` Up.

### Step 4 — 로깅: 방화벽 (API → Loki:3100)
- GCP 방화벽: source `10.178.0.3` → monitoring VM tcp:3100 허용(VPC 내부면 default-allow-internal로 이미 열렸을 수 있음 — Step 2 실측 시 같이 확인).

### Step 5 (선택) — 대시보드 로그 패널
- 파일: `backend/monitoring/grafana/dashboards/fastapi.json`
- Loki datasource(uid `loki`) 기반 `logs` 패널 1개 추가: `{job="localbiz-api"}`.
- verify: Grafana 대시보드에 로그 스트림 표시.

## 리스크 / 주의
- **불변식 영향 없음**(19개 무관 — 인프라/관측 설정만).
- `prometheus.yml` 하드코딩 IP(10.178.0.3): 내부 IP는 VM 재생성 시에도 보통 유지되나, 변경되면 다시 수정 필요. (대안: GCE SD 사용 — YAGNI, 현 단계 과함.)
- 방화벽 변경은 되돌리기 쉬움(rule 삭제). 단 잘못 열면 노출 — source-ranges를 VPC 내부(10.178.0.0/24)로 한정.
- 배포 워크플로 변경은 dev 배포로 검증.

## 검증 (구현 후)
- `validate.sh` 통과 (ruff/pyright/pytest — 단 이번 변경은 YAML/JSON/워크플로라 코드 테스트 영향 적음).
- 실 배포 후 Step 1~3 verify 명령 재실행 → up/labels 확인.
- 커밋 prefix `fix:`, 브랜치 `fix/`, PR base `dev`.
