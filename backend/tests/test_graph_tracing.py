"""Tests for `src.graph._tracing` — `traced_node` 데코레이터 + `traced_call` 컨텍스트.

InMemorySpanExporter로 span 생성·attribute·status를 직접 검증.

NOTE: fixture 패턴(autouse/module-scope)을 사용하면 pytest-asyncio 0.x의
hookwrapper teardown에서 `OSError: could not get source code`가 발생하는 케이스가 있다
— 그래서 모듈-레벨 export·테스트당 `_clear()` 호출의 단순 패턴을 사용한다.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
from opentelemetry import trace  # pyright: ignore[reportMissingImports]
from opentelemetry.sdk.trace import TracerProvider  # pyright: ignore[reportMissingImports]
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # pyright: ignore[reportMissingImports]
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # pyright: ignore[reportMissingImports]
    InMemorySpanExporter,
)

from src.graph._tracing import traced_call, traced_node

# 모듈-import 시점에 TracerProvider + InMemorySpanExporter 1회 세팅.
# `set_tracer_provider`는 첫 호출만 적용되므로, 이미 세팅된 경우 우리 exporter를 추가만 한다.
_exporter = InMemorySpanExporter()
_current_provider = trace.get_tracer_provider()
if isinstance(_current_provider, TracerProvider):
    _current_provider.add_span_processor(SimpleSpanProcessor(_exporter))
else:
    _provider = TracerProvider()
    _provider.add_span_processor(SimpleSpanProcessor(_exporter))
    trace.set_tracer_provider(_provider)


def _fresh_exporter() -> InMemorySpanExporter:
    """누적된 span을 비우고 exporter를 반환."""
    _exporter.clear()
    return _exporter


# ---------------------------------------------------------------------------
# traced_node
# ---------------------------------------------------------------------------
class TestTracedNode:
    async def test_passes_through_when_state_not_dict(self) -> None:
        """state가 dict가 아니어도 노드는 정상 통과·span은 생성."""
        exporter = _fresh_exporter()

        @traced_node("noop")
        async def fn(state: Any) -> list[dict[str, str]]:
            return [{"type": "text"}]

        result = await fn("not-a-dict")
        assert result == [{"type": "text"}]
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "node.noop"

    async def test_attaches_state_attributes(self) -> None:
        """state에서 intent/thread_id/user_id + response_blocks.added 부착."""
        exporter = _fresh_exporter()

        @traced_node("place_search")
        async def fn(state: dict[str, Any]) -> list[dict[str, Any]]:
            return [{"type": "places"}, {"type": "map_markers"}]

        await fn({"intent": "PLACE_SEARCH", "thread_id": "t-1", "user_id": 7})

        spans = exporter.get_finished_spans()
        assert spans[0].name == "node.place_search"
        attrs = spans[0].attributes or {}
        assert attrs.get("intent") == "PLACE_SEARCH"
        assert attrs.get("thread_id") == "t-1"
        assert attrs.get("user_id") == "7"
        assert attrs.get("response_blocks.added") == 2

    async def test_missing_state_keys_silent(self) -> None:
        """state dict에 키가 없거나 빈 값이면 attribute 생략 — KeyError·NoneError 금지."""
        exporter = _fresh_exporter()

        @traced_node("general")
        async def fn(state: dict[str, Any]) -> None:
            return None

        await fn({"intent": ""})  # 빈 intent는 부착하지 않음
        spans = exporter.get_finished_spans()
        attrs = spans[0].attributes or {}
        assert "intent" not in attrs
        assert "thread_id" not in attrs
        assert "user_id" not in attrs

    async def test_exception_sets_error_status(self) -> None:
        """노드 예외 → status=ERROR + exception event 기록 + 예외 재발생."""
        exporter = _fresh_exporter()

        @traced_node("boom")
        async def fn(state: dict[str, Any]) -> None:
            raise RuntimeError("kaboom")

        with pytest.raises(RuntimeError, match="kaboom"):
            await fn({"intent": "X"})

        spans = exporter.get_finished_spans()
        assert spans[0].status.status_code.name == "ERROR"
        assert any(ev.name == "exception" for ev in spans[0].events)


# ---------------------------------------------------------------------------
# traced_call
# ---------------------------------------------------------------------------
class TestTracedCall:
    async def test_basic_attributes(self) -> None:
        exporter = _fresh_exporter()
        async with traced_call("os.places_vector.knn", index="places_vector", op="knn") as span:
            assert span is not None

        spans = exporter.get_finished_spans()
        assert spans[0].name == "os.places_vector.knn"
        attrs = spans[0].attributes or {}
        assert attrs.get("index") == "places_vector"
        assert attrs.get("op") == "knn"

    async def test_none_attrs_skipped(self) -> None:
        """attribute 값이 None이면 부착 skip — 호출부에서 미상 값 그대로 넘겨도 안전."""
        exporter = _fresh_exporter()
        none_attr: Optional[str] = None
        async with traced_call("pg.places.search", op="search", note=none_attr):
            pass
        spans = exporter.get_finished_spans()
        attrs = spans[0].attributes or {}
        assert attrs.get("op") == "search"
        assert "note" not in attrs

    async def test_exception_recorded(self) -> None:
        exporter = _fresh_exporter()
        with pytest.raises(ValueError, match="bad"):
            async with traced_call("gemini.classify"):
                raise ValueError("bad")

        spans = exporter.get_finished_spans()
        assert spans[0].name == "gemini.classify"
        assert spans[0].status.status_code.name == "ERROR"
        assert any(ev.name == "exception" for ev in spans[0].events)
