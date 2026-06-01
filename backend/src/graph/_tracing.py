"""LangGraph 노드용 OpenTelemetry tracing 헬퍼.

- `traced_node(name)` 데코레이터: 노드 진입 시 `node.<name>` span 시작, state에서
  intent/thread_id/user_id 속성 부착, 결과 list 길이를 `response_blocks.added`로 기록.
- `traced_call(name, **attrs)` async 컨텍스트: 노드 내부 외부 호출(LLM/DB/HTTP)에
  sub-span 부착.

`telemetry.setup_tracing()`이 호출되지 않은 환경(`JAEGER_HOST` 미설정 로컬·테스트)에서는
OTel가 NoOp tracer를 반환 — span 생성·attribute 부착이 모두 no-op이라 성능 영향 0.

Phase 3(메트릭) 진입 시 `traced_node`/`traced_call` 내부에서 `langgraph_node_latency_seconds`,
`langgraph_os_query_latency_seconds` 등 Prometheus 히스토그램 observe 후크를 추가한다.
현재 파일은 tracing 전용 — metric hook은 의도적으로 미포함.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any

from opentelemetry import trace  # pyright: ignore[reportMissingImports]
from opentelemetry.trace import Span, StatusCode  # pyright: ignore[reportMissingImports]

# 모든 그래프 노드 span을 단일 tracer name으로 통합 — Jaeger UI에서
# service.name(localbiz-api)·tracer name(localbiz.graph) 단위 필터를 일관되게 사용.
_tracer = trace.get_tracer("localbiz.graph")


def _attach_state_attrs(span: Span, state: Any) -> None:
    """state dict에서 intent/thread_id/user_id를 span 속성으로 부착.

    state가 dict가 아니거나 키가 누락이어도 silent하게 skip — tracing이 노드 실행을
    절대 깨트리지 않도록.
    """
    if not isinstance(state, dict):
        return
    intent = state.get("intent")
    if intent is not None and intent != "":
        span.set_attribute("intent", str(intent))
    thread_id = state.get("thread_id")
    if thread_id is not None:
        span.set_attribute("thread_id", str(thread_id))
    user_id = state.get("user_id")
    if user_id is not None:
        span.set_attribute("user_id", str(user_id))


def traced_node(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """LangGraph 노드 진입에 `node.<name>` span을 부착하는 데코레이터.

    노드 함수 시그니처(`async def f(state, ...)`)를 보존하며, state에서
    intent/thread_id/user_id를 추출해 span 속성으로 부착한다. 노드가 list를 반환하면
    그 길이를 `response_blocks.added` 속성으로 기록(어느 노드가 출력을 생성·스킵했는지
    Jaeger UI에서 한눈에 확인).

    예외 발생 시 `record_exception` + status=ERROR 부착 후 원래 예외를 재발생.

    Args:
        name: span 이름 suffix. 최종 span 이름은 `node.<name>`.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        async def wrapper(state: Any, *args: Any, **kwargs: Any) -> Any:
            with _tracer.start_as_current_span(f"node.{name}") as span:
                _attach_state_attrs(span, state)
                try:
                    result = await fn(state, *args, **kwargs)
                    if isinstance(result, list):
                        span.set_attribute("response_blocks.added", len(result))
                    return result
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(StatusCode.ERROR, str(exc))
                    raise

        return wrapper

    return decorator


@asynccontextmanager
async def traced_call(name: str, **attrs: Any) -> AsyncIterator[Span]:
    """노드 내부 외부 호출(LLM/DB/HTTP)에 sub-span을 부착하는 async 컨텍스트.

    예외 발생 시 `record_exception` + status=ERROR 후 원래 예외를 재발생.
    attribute 값이 None이면 부착 skip — 호출부에서 `index=None` 같은 미상 값을
    그대로 넘겨도 안전.

    Args:
        name: span 이름. 권장 명명: `<system>.<op>` — 예: `os.places_vector.knn`,
            `pg.places.search`, `gemini.booking_grounding`, `naver.event_search`.
        **attrs: span attribute로 즉시 부착할 키-값 쌍.
    """
    with _tracer.start_as_current_span(name) as span:
        for k, v in attrs.items():
            if v is not None:
                span.set_attribute(k, str(v))
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            raise
