"""Smoke tests for `src.observability.metrics` — 메트릭 정의 존재 + 라벨 인터페이스."""

from __future__ import annotations

from prometheus_client import generate_latest  # pyright: ignore[reportMissingImports]

from src.observability.metrics import (
    langgraph_fallback_total,
    langgraph_intent_total,
    langgraph_llm_calls_total,
    langgraph_llm_latency_seconds,
    langgraph_llm_tokens_total,
    langgraph_node_latency_seconds,
    langgraph_os_query_latency_seconds,
    langgraph_pg_pool_in_use,
    langgraph_pg_query_latency_seconds,
    langgraph_sse_sessions,
)


def test_all_metrics_are_exportable() -> None:
    """10종 메트릭이 모두 정의되고 default registry에 등록되어 /metrics에 노출되는지."""
    langgraph_intent_total.labels(intent="PLACE_SEARCH").inc()
    langgraph_node_latency_seconds.labels(node="place_search", intent="PLACE_SEARCH", status="ok").observe(0.5)
    langgraph_llm_calls_total.labels(model="gemini-2.5-flash", purpose="rerank", status="ok").inc()
    langgraph_llm_tokens_total.labels(model="gemini-2.5-flash", purpose="rerank", direction="input").inc(100)
    langgraph_llm_latency_seconds.labels(model="gemini-2.5-flash", purpose="rerank").observe(1.5)
    langgraph_os_query_latency_seconds.labels(index="places_vector", op="knn").observe(0.1)
    langgraph_pg_query_latency_seconds.labels(op="places_search").observe(0.05)
    langgraph_pg_pool_in_use.set(3)
    langgraph_fallback_total.labels(path="event.naver_sparse").inc()
    langgraph_sse_sessions.inc()

    payload = generate_latest().decode()
    for name in (
        "langgraph_intent_total",
        "langgraph_node_latency_seconds",
        "langgraph_llm_calls_total",
        "langgraph_llm_tokens_total",
        "langgraph_llm_latency_seconds",
        "langgraph_os_query_latency_seconds",
        "langgraph_pg_query_latency_seconds",
        "langgraph_pg_pool_in_use",
        "langgraph_fallback_total",
        "langgraph_sse_sessions",
    ):
        assert name in payload, f"{name} 메트릭이 /metrics에 노출되지 않음"


def test_fallback_path_labels_known_enum() -> None:
    """fallback path 라벨 enum 4종 — 폭증 회피 확인용 회귀 테스트."""
    known_paths = (
        "event.naver_sparse",
        "event_recommend.naver_sparse",
        "intent.no_llm_key",
        "image.knn_scene_fallback",
    )
    for path in known_paths:
        langgraph_fallback_total.labels(path=path).inc()
