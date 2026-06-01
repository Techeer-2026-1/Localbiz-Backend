# Observability Overhaul — Tracing + Logging + Metrics 통합 plan

**Date:** 2026-06-01
**Owner:** 이정 (BE/PM)
**Status:** DRAFT (대기 → APPROVED 시 구현 착수)
**Related:** `~/.claude/plans/polymorphic-wishing-peach.md`(노드 트레이싱 plan, 본 plan에 흡수), 메모 `project_monitoring_tracing.md`

---

## 1. Context

LocalBiz 백엔드는 Jaeger·Prometheus·Loki 인프라가 모두 떠 있고 dev에 배포까지 완료됐지만(PR #173/#174), **앱 측 계측이 절반만 들어가 있어 운영 가시성이 깨져 있다.** 현 상태:

- **Tracing**: `FastAPIInstrumentor`로 HTTP 진입 span은 자동 발급. 그래프 18개 노드 중 `event_search_node` 1개만 수동 sub-span 6개. 나머지 17개 노드는 Jaeger에 ASGI span만 보이고 어디서 시간이 새는지 모름.
- **Metrics**: `prometheus_fastapi_instrumentator`로 HTTP RPS/latency/status는 자동. **커스텀 메트릭 0건** — intent 분포, 노드 latency, LLM 호출/토큰, asyncpg 풀, OpenSearch 쿼리, fallback 카운터, SSE 세션 모두 없음.
- **Logging**: `logging.basicConfig(INFO, "%(levelname)s %(name)s: %(message)s")` 평문. Loki까지 적재는 되지만 `trace_id`/`thread_id`/`user_id`/`request_id` 미주입 → Loki ↔ Jaeger 점프 불가능, 사용자 단위 디버깅 불가.

목표는 *프로덕션 사고가 났을 때 Grafana 한 화면에서 "어느 intent의 어느 노드에서 어느 외부 호출이 느려졌고 그 사용자가 누구인지"까지 추적 가능* 한 수준.

## 2. Goals / Non-Goals

### Goals
- 모든 LangGraph 노드(18개)에 진입 span. 무거운 노드는 외부 호출(LLM/asyncpg/OS/Naver/OSRM/Google) sub-span까지.
- 로그를 JSON 구조화 + `trace_id`/`span_id`/`thread_id`/`user_id`/`request_id` 자동 주입 → Grafana Loki 패널에서 한 클릭으로 Jaeger trace 점프.
- 비즈니스 메트릭 8종 추가: intent 카운트, 노드 latency 히스토그램, LLM 호출 latency + 토큰 카운트, asyncpg 풀 게이지, OS 쿼리 latency, Naver fallback 카운터, OS k-NN empty 카운터, SSE 활성 세션 게이지.
- Grafana 대시보드 1개 추가: "LangGraph Overview" (노드별 latency p50/p95/p99, intent 분포, LLM 토큰 사용량, fallback rate).

### Non-Goals (별건 처리)
- `allow-fastapi` 방화벽 VPC 내부 제한 — 별도 보안 PR.
- 알람·SLO 룰 — Phase 4에서 별도 plan.
- 비용 예산 알람(Gemini 토큰 비용) — 메트릭 적재까지만, 알람은 후속.
- Phase 6 KAIROS 통합 — 본 plan 범위 외.

## 3. Architecture

### 3.1 Tracing 계층

```
SSE handler (auto span "POST /sse")
  └─ intent_router  ┬─ traced_node("intent.classify")
                    └─ LLM span ("gemini.classify")
  └─ query_preprocessor
                    ├─ traced_node("query.preprocess")
                    └─ LLM span ("gemini.json_parse")
  └─ {각 intent 노드}
                    ├─ traced_node("place_recommend") 등
                    ├─ DB span ("pg.places.search")
                    ├─ OS span ("os.places_vector.knn")
                    ├─ LLM span ("gemini.rerank")
                    └─ external span ("naver.fallback")
  └─ response_builder traced_node("response.build")
```

신규 모듈 `src/graph/_tracing.py`:

```python
# 골자
from functools import wraps
from opentelemetry import trace

_tracer = trace.get_tracer("localbiz.graph")

def traced_node(name: str):
    def deco(fn):
        @wraps(fn)
        async def wrapper(state, *args, **kwargs):
            with _tracer.start_as_current_span(f"node.{name}") as span:
                span.set_attribute("intent", state.get("intent", "unknown"))
                span.set_attribute("thread_id", state.get("thread_id", ""))
                span.set_attribute("user_id", state.get("user_id", 0))
                try:
                    result = await fn(state, *args, **kwargs)
                    span.set_attribute("response_blocks.added", len(result) if isinstance(result, list) else 0)
                    return result
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(trace.StatusCode.ERROR, str(exc))
                    raise
        return wrapper
    return deco
```

- `JAEGER_HOST` 미설정 시에도 OTel SDK가 NoOp tracer 반환 → 로컬·테스트 영향 0.
- 외부 호출 span 헬퍼는 같은 모듈에 `traced_call(name, **attrs)` 컨텍스트 매니저로 제공해 `event_search_node` 기존 패턴과 통합.

### 3.2 Logging 계층

신규 모듈 `src/observability/logging.py`:

```python
# 골자 — python-json-logger 의존성 추가
from pythonjsonlogger import jsonlogger
from opentelemetry import trace

class TraceContextFilter(logging.Filter):
    def filter(self, record):
        ctx = trace.get_current_span().get_span_context()
        record.trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else None
        record.span_id = format(ctx.span_id, "016x") if ctx.is_valid else None
        # FastAPI ContextVar에서 request_id/user_id/thread_id 주입
        return True

def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
    ))
    handler.addFilter(TraceContextFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    # 소음 모듈 quiet-down
    for noisy in ("httpx", "httpcore", "asyncpg", "opentelemetry.exporter"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
```

- `main.py`의 `logging.basicConfig` 호출은 `configure_logging()`으로 교체.
- ContextVar 기반 request-scoped 컨텍스트(`request_id`, `user_id`, `thread_id`)는 SSE 핸들러 진입에서 `set` → 자식 태스크/노드에서 자동 inherit (anyio TaskGroup 호환).
- promtail은 이미 stdout 수집 중 — 포맷만 JSON으로 바뀌면 Loki LogQL에서 `{app="localbiz-api"} | json | trace_id="..."` 가능.

### 3.3 Metrics 계층

신규 모듈 `src/observability/metrics.py` (Prometheus client 직접 사용):

| 메트릭 | 타입 | 라벨 | 측정 위치 |
|---|---|---|---|
| `langgraph_intent_total` | Counter | `intent` | intent_router 종료 |
| `langgraph_node_latency_seconds` | Histogram | `node`, `intent`, `status` | `traced_node` 데코레이터 내부 (span end 시점) |
| `langgraph_llm_calls_total` | Counter | `model`, `purpose`, `status` | 공통 LLM 래퍼 (`utils/llm_parsing.py`에 카운터 hook) |
| `langgraph_llm_tokens_total` | Counter | `model`, `purpose`, `direction` | 동상, `response.usage_metadata`에서 추출 |
| `langgraph_llm_latency_seconds` | Histogram | `model`, `purpose` | 동상 |
| `langgraph_os_query_latency_seconds` | Histogram | `index`, `op` | `db/opensearch.py` 진입/종료 |
| `langgraph_fallback_total` | Counter | `path` (e.g., `event.naver`, `places.os_empty`) | 각 fallback 분기 |
| `langgraph_sse_sessions` | Gauge | — | SSE handler enter/exit |
| `langgraph_pg_pool_in_use` | Gauge | — | 주기적 task로 `pool.get_size() - pool.get_idle_size()` |

- 기존 `prometheus_fastapi_instrumentator` 노출(`/metrics`)에 자동 합류. 별도 endpoint 분리 안 함.

## 4. Components & Files

| 파일 | 변경 유형 | 비고 |
|---|---|---|
| `backend/src/observability/__init__.py` | NEW | 패키지 진입 |
| `backend/src/observability/logging.py` | NEW | JSON 포맷 + trace 컨텍스트 필터 |
| `backend/src/observability/metrics.py` | NEW | Prometheus client metrics 정의·헬퍼 |
| `backend/src/observability/context.py` | NEW | ContextVar (`request_id`, `user_id`, `thread_id`) |
| `backend/src/graph/_tracing.py` | NEW | `traced_node` 데코레이터 + `traced_call` 컨텍스트 |
| `backend/src/telemetry.py` | EDIT | `setup_tracing`은 유지. `configure_logging` 호출은 `main.py`로 이동. |
| `backend/src/main.py` | EDIT | `basicConfig` → `configure_logging()`. metrics 초기화 추가. SSE 라우터 미들웨어에서 ContextVar 세팅. |
| `backend/src/api/sse.py` | EDIT | request 진입 시 `request_id` 생성, `thread_id`·`user_id` 컨텍스트 주입, `langgraph_sse_sessions` enter/exit |
| `backend/src/graph/*_node.py` (17개) | EDIT | `@traced_node("name")` 데코레이터 부착. 무거운 노드(course_plan/place_search/place_recommend/analysis/booking/calendar/cost_estimate/review_compare)는 외부 호출 sub-span 추가 |
| `backend/src/graph/event_search_node.py` | EDIT | 기존 span 호출을 `traced_call` 헬퍼로 리팩토링 (의미·계층 동일, DRY 통일) |
| `backend/src/utils/llm_parsing.py` | EDIT | Gemini 호출 래퍼에서 metrics counter/histogram·sub-span 발급 |
| `backend/src/db/postgres.py` | EDIT | pool 게이지 백그라운드 갱신 task (lifespan) |
| `backend/src/db/opensearch.py` | EDIT | 쿼리 헬퍼에 metrics + span |
| `backend/requirements.txt` | EDIT | `python-json-logger==2.0.7` 추가 (필요 시) |
| `backend/monitoring/grafana/dashboards/langgraph-overview.json` | NEW | 노드 latency p50/p95/p99, intent 분포, LLM 토큰, fallback rate 패널 |
| `backend/monitoring/grafana/provisioning/dashboards/*.yaml` | EDIT | 위 대시보드 등록 |
| `backend/tests/test_observability_logging.py` | NEW | JSON 포맷 + trace_id 주입 검증 |
| `backend/tests/test_observability_metrics.py` | NEW | counter/histogram 라벨 회귀 |
| `backend/tests/test_graph_tracing.py` | NEW | `traced_node` no-op·span 발급·에러 status 검증 |

## 5. Phased Rollout (3 PRs)

각 페이즈는 별도 PR. 검증·롤백 독립.

### Phase 1 — Tracing 전 노드 확장 (`feat/observability-tracing`)
1. `src/graph/_tracing.py` + 테스트.
2. 17개 노드에 `@traced_node` 부착.
3. 무거운 노드(8개)에 외부 호출 `traced_call` sub-span 추가.
4. `event_search_node` 기존 span을 `traced_call`로 리팩토링.
5. **Verify**: `JAEGER_HOST` 세팅한 staging에서 각 intent 호출 → Jaeger에 `node.*` span 트리 + 외부 호출 sub-span 보이는지 확인. `validate.sh` 통과.

### Phase 2 — 구조화 로깅 + 트레이스 상관 (`feat/observability-logging`)
1. `python-json-logger` 의존성 추가, `requirements.txt`·`validate.sh` 통과.
2. `src/observability/{__init__,logging,context}.py` 작성·테스트.
3. `main.py`에서 `configure_logging()` 적용, `sse.py`에서 ContextVar 세팅.
4. **Verify**: 로컬 backend → `curl /sse?... | head`. stdout이 JSON 1줄당 1로그, `trace_id`/`thread_id` 필드 존재. dev 배포 후 Loki에서 `{app="localbiz-api"} | json | trace_id != ""` 매칭. Grafana 로그 패널에서 Jaeger 점프 링크(`derived field`) 동작.

### Phase 3 — 비즈니스 메트릭 + Grafana 대시보드 (`feat/observability-metrics`)
1. `src/observability/metrics.py` 작성·테스트.
2. `_tracing.py`의 데코레이터 안에 latency 히스토그램 hook.
3. `llm_parsing.py`/`opensearch.py`/`postgres.py`/`sse.py`에 메트릭 hook.
4. fallback 분기 카운터 부착(event Naver, places OS empty 등 — 코드 grep 후 확정).
5. Grafana 대시보드 JSON·provisioning 추가.
6. **Verify**: `/metrics` curl → `langgraph_*` 메트릭 노출. Prometheus Status→Targets UP. Grafana "LangGraph Overview" 대시보드 패널 데이터 흐름. 5분 부하 후 p95 latency 그래프 채워짐.

## 6. Data Flow (한 요청 기준)

```
브라우저 EventSource POST /sse
  ↓
sse.py 진입:
  - ContextVar 세팅: request_id(uuid4), user_id(jwt), thread_id(body)
  - SSE 세션 게이지 +1
  ↓
intent_router_node:
  - @traced_node("intent.classify") span 시작 (trace_id 발급 → log 자동 포함)
  - LLM 래퍼: traced_call("gemini.classify") + llm_calls_total/+tokens_total/+latency
  - intent_total{intent="PLACE_RECOMMEND"} +1
  ↓
query_preprocessor_node: 동일 패턴
  ↓
place_recommend_node:
  - @traced_node("place_recommend") + node_latency_seconds histogram
  - traced_call("os.places_vector.knn") + os_query_latency_seconds
  - traced_call("gemini.rerank") + llm_*
  - 결과 없으면 fallback_total{path="places.os_empty"} +1
  ↓
response_builder_node: traced_node + 마지막 SSE write
  ↓
sse.py 종료: 세션 게이지 -1, request_id 컨텍스트 해제
```

로그 한 줄 예시:
```json
{"ts":"2026-06-01T12:00:00Z","level":"INFO","logger":"src.graph.place_recommend_node","message":"OS knn hit","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","span_id":"00f067aa0ba902b7","request_id":"...","thread_id":"42","user_id":7,"hits":12}
```

## 7. Error Handling

- `traced_node` 내부 예외는 `span.record_exception` + `StatusCode.ERROR` → 로그에 stacktrace 자동(JSON formatter `exc_info`) → 메트릭은 `status="error"` 라벨로 카운트.
- OTel/JSON 로거 초기화 실패는 기존 `telemetry.py` 패턴대로 ImportError·RuntimeError 분리 warning, **앱 기동은 계속.** 관측 인프라 부재가 서비스 장애로 이어지지 않아야 함.
- ContextVar는 진입 시 `set` 후 finally에서 `reset` — async-leak 회피.

## 8. Testing

| 테스트 | 위치 | 핵심 케이스 |
|---|---|---|
| `test_observability_logging.py` | tests/ | JSON 1줄 1레코드, `trace_id` 주입, 없을 때 `null`, `exc_info` 직렬화 |
| `test_observability_metrics.py` | tests/ | counter inc, histogram observe, label cardinality 회귀 |
| `test_graph_tracing.py` | tests/ | `traced_node` no-op(tracer 미설정), span attribute 세팅, 예외 시 status=ERROR |
| `test_sse_context.py` | tests/ | SSE 진입 시 ContextVar 세팅·해제, async task 간 inherit |
| `validate.sh` | 루트 | ruff/format/pyright/pytest/기획·plan 무결성 |
| 수동(staging) | — | Jaeger 트리 / Loki json 매칭 / Grafana 대시보드 패널 |

## 9. Risks & Rollback

| 리스크 | 완화 |
|---|---|
| JSON 로그가 로컬 가독성 저해 | `LOG_FORMAT=plain` env로 fallback 옵션 제공 (개발용) |
| 메트릭 라벨 카디널리티 폭증 | 라벨에 `user_id`/`thread_id` **포함 금지** (오직 trace/log에). intent enum 13종, node 18종, model 2~3종으로 상한 고정 |
| 데코레이터 부착으로 노드 시그니처 변경 | `@wraps` + `async def` 유지 — LangGraph는 함수 객체만 보므로 호환 |
| OTel SDK 버전 충돌 | requirements.txt 현재 핀과 비교 후 별도 commit으로 분리 |
| 롤백 | 각 PR이 독립 → 문제 PR만 revert. metrics만 끄려면 `/metrics` 라우트 차단으로 즉시 격리 가능. |

## 10. Verification (PR 머지 전 공통)

- [ ] `./validate.sh` 통과
- [ ] 신규 모듈 단위 테스트 추가 (logging/metrics/tracing)
- [ ] dev 배포 후 Jaeger에 `node.*` span 트리 확인 (Phase 1)
- [ ] Loki LogQL `{app="localbiz-api"} | json | trace_id != ""` 매칭 (Phase 2)
- [ ] Prometheus targets UP + `langgraph_*` 메트릭 노출 + 대시보드 패널 (Phase 3)
- [ ] 19 데이터 모델 불변식 무관 (관측성은 데이터 모델 변경 없음)

## 11. Open Questions / Follow-ups

- LLM 토큰 카운트 추출 경로: `google.genai` 응답의 `usage_metadata` 정확한 키 확인 필요(Phase 3 착수 시 1회 spike).
- promtail이 stdout만 보는데, multiprocess uvicorn 워커일 때 JSON 1라인 보장되는지 — Phase 2 staging에서 검증.
- Grafana 대시보드는 JSON으로 git에 두는 게 맞는지, provisioning 디렉터리 권한 — `backend/monitoring/` 기존 패턴 따라감.

---

## APPROVED

APPROVED — 2026-06-01 이정

**정정사항 반영:** 본 plan의 일부 가정(특히 §3.3 `utils/llm_parsing.py` LLM 후크 위치)은 코드 실측에서 오류로 확인됨. 정정·실행 순서는 `~/.claude/plans/polymorphic-questing-biscuit.md`(execution plan) 참조. 충돌 시 execution plan 우선.
