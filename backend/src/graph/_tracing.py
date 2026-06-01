"""LangGraph 노드용 OpenTelemetry tracing + Prometheus latency 후크.

- `traced_node(name)` 데코레이터: 노드 진입 시 `node.<name>` span 시작, state에서
  intent/thread_id/user_id 속성 부착, 결과의 `response_blocks` 길이를 attribute로 기록.
  노드 반환 타입이 dict(`{"response_blocks": [...]}`)이거나 list 어느 쪽이든 처리.
  또한 `langgraph_node_latency_seconds{node, intent, status}` Prometheus 히스토그램에
  실행 시간을 observe (status는 ok|error).
- `traced_call(name, **attrs)` async 컨텍스트: 노드 내부 외부 호출(LLM/DB/HTTP)에
  sub-span 부착.

예외 처리는 OTel SDK 기본 동작(`record_exception=True`, `set_status_on_exception=True`)에
일임 — 명시적 `record_exception`/`set_status` 호출 시 중복 이벤트/상태 갱신이 발생하므로
수동 호출을 두지 않는다. 단, metric label용 `status` 추적을 위해 `traced_node`는
try/except로 status 변수만 갱신하고 예외는 즉시 재발생한다.

`telemetry.setup_tracing()`이 호출되지 않은 환경(`JAEGER_HOST` 미설정 로컬·테스트)에서는
OTel가 NoOp tracer를 반환 — span 생성·attribute 부착이 모두 no-op이라 성능 영향 0.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any, Optional

from opentelemetry import trace  # pyright: ignore[reportMissingImports]
from opentelemetry.trace import Span  # pyright: ignore[reportMissingImports]

from src.observability.metrics import langgraph_node_latency_seconds

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


def _count_response_blocks(result: Any) -> Optional[int]:
    """노드 반환값에서 response_blocks 길이를 추출. 측정 불가하면 None.

    LangGraph 노드는 두 가지 형태로 반환한다:
      - dict (`{"response_blocks": [...], ...}`) — analysis/calendar/cost_estimate 등 대부분
      - list (`[block, block, ...]`) — Annotated[list, operator.add]로 reduce되는 케이스

    어느 쪽도 아니면 None 반환 — attribute 부착 생략.
    """
    if isinstance(result, dict):
        blocks = result.get("response_blocks")
        if isinstance(blocks, list):
            return len(blocks)
        return None
    if isinstance(result, list):
        return len(result)
    return None


def traced_node(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """LangGraph 노드 진입에 `node.<name>` span + latency histogram을 부착하는 데코레이터.

    노드 함수 시그니처(`async def f(state, ...)`)를 보존하며, state에서
    intent/thread_id/user_id를 추출해 span 속성으로 부착한다. 노드가 추가한
    response_blocks 길이를 `response_blocks.added` 속성으로 기록(어느 노드가 출력을
    생성·스킵했는지 Jaeger UI에서 한눈에 확인).

    실행 시간은 `langgraph_node_latency_seconds{node, intent, status}` Prometheus
    히스토그램에 observe. 예외 발생 시 OTel SDK가 자동으로 `record_exception` +
    status=ERROR span을 기록 — try/except는 metric label `status="error"` 추적만 담당.

    Args:
        name: span 이름 suffix. 최종 span 이름은 `node.<name>`.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        async def wrapper(state: Any, *args: Any, **kwargs: Any) -> Any:
            intent_label = "unknown"
            if isinstance(state, dict):
                raw_intent = state.get("intent")
                if raw_intent:
                    intent_label = str(raw_intent)
            start = time.monotonic()
            status = "ok"
            with _tracer.start_as_current_span(f"node.{name}") as span:
                _attach_state_attrs(span, state)
                try:
                    result = await fn(state, *args, **kwargs)
                    block_count = _count_response_blocks(result)
                    if block_count is not None:
                        span.set_attribute("response_blocks.added", block_count)
                    return result
                except Exception:
                    status = "error"
                    raise  # OTel SDK가 record_exception + status=ERROR 자동 처리.
                finally:
                    elapsed = time.monotonic() - start
                    langgraph_node_latency_seconds.labels(node=name, intent=intent_label, status=status).observe(
                        elapsed
                    )

        return wrapper

    return decorator


@asynccontextmanager
async def traced_call(name: str, **attrs: Any) -> AsyncIterator[Span]:
    """노드 내부 외부 호출(LLM/DB/HTTP)에 sub-span을 부착하는 async 컨텍스트.

    예외 처리는 OTel SDK 기본값(`record_exception`+`set_status_on_exception`)에 일임.
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
        yield span
