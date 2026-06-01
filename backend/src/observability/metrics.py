"""LangGraph 비즈니스 메트릭 — Prometheus client 직접 사용.

이미 `prometheus_fastapi_instrumentator`가 HTTP RPS/latency/status는 자동 export 중.
본 모듈은 그 위에 LangGraph 도메인 메트릭 10종을 추가한다 — 같은 `/metrics` 엔드포인트에
자동 합류(prometheus_client의 default registry 사용).

라벨 카디널리티 상한 (불변 — 폭증 회피):
  - intent: 13종 (PLACE_SEARCH … GENERAL)
  - node: 18종 (그래프 노드)
  - model: 2~3종 (gemini-2.5-flash, gemini-embedding-001 등)
  - purpose: 약 10종 (intent_classify / query_preprocess / rerank / …)
  - path (fallback): 약 5종

라벨에 **`user_id`/`thread_id` 절대 포함 금지** — 그건 trace_id/log로 식별. 메트릭은 집계만.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram  # pyright: ignore[reportMissingImports]

# ---------------------------------------------------------------------------
# LangGraph 노드
# ---------------------------------------------------------------------------
langgraph_intent_total = Counter(
    "langgraph_intent_total",
    "분류된 intent 누적 카운트 (intent_router 종료 시점).",
    ["intent"],
)

langgraph_node_latency_seconds = Histogram(
    "langgraph_node_latency_seconds",
    "LangGraph 노드 실행 시간 (초). `traced_node` 데코레이터에서 observe.",
    ["node", "intent", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# ---------------------------------------------------------------------------
# LLM (Gemini)
# ---------------------------------------------------------------------------
langgraph_llm_calls_total = Counter(
    "langgraph_llm_calls_total",
    "LLM 호출 누적 카운트. status는 ok|error.",
    ["model", "purpose", "status"],
)

langgraph_llm_tokens_total = Counter(
    "langgraph_llm_tokens_total",
    "LLM 토큰 누적. direction은 input|output.",
    ["model", "purpose", "direction"],
)

langgraph_llm_latency_seconds = Histogram(
    "langgraph_llm_latency_seconds",
    "LLM 호출 시간 (초).",
    ["model", "purpose"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)

# P3-D: Gemini 토큰 → USD 환산 누적 비용 메트릭
# 토큰 단가는 환경변수로 분리 — 가격 변동 시 재배포 없이 조정.
langgraph_llm_cost_usd_total = Counter(
    "langgraph_llm_cost_usd_total",
    "LLM 호출 누적 비용 (USD). 토큰 단가 × 토큰 수.",
    ["model", "purpose", "direction"],
)

# ---------------------------------------------------------------------------
# OpenSearch / PostgreSQL
# ---------------------------------------------------------------------------
langgraph_os_query_latency_seconds = Histogram(
    "langgraph_os_query_latency_seconds",
    "OpenSearch 쿼리 시간 (초). index/op 라벨.",
    ["index", "op"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

langgraph_pg_query_latency_seconds = Histogram(
    "langgraph_pg_query_latency_seconds",
    "PostgreSQL 쿼리 시간 (초). op 라벨.",
    ["op"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

langgraph_pg_pool_in_use = Gauge(
    "langgraph_pg_pool_in_use",
    "현재 사용 중인 asyncpg 커넥션 수 (pool.get_size - pool.get_idle_size).",
)

# ---------------------------------------------------------------------------
# Fallback / SSE
# ---------------------------------------------------------------------------
langgraph_fallback_total = Counter(
    "langgraph_fallback_total",
    "Fallback 분기 진입 카운트. path 라벨 enum 고정.",
    ["path"],
)

langgraph_sse_sessions = Gauge(
    "langgraph_sse_sessions",
    "현재 활성 SSE 세션 수 (enter 시 inc, exit 시 dec).",
)
