"""Tests for `src.observability.logging` — JSON 포맷 + trace 컨텍스트 주입."""

from __future__ import annotations

import json
import logging
from typing import Any

from opentelemetry import trace  # pyright: ignore[reportMissingImports]
from opentelemetry.sdk.trace import TracerProvider  # pyright: ignore[reportMissingImports]

from src.observability.context import request_id_var, thread_id_var, user_id_var
from src.observability.logging import TraceContextFilter, configure_logging


def _last_log_line(capsys: Any) -> dict[str, Any]:
    """capsys로 캡처한 stderr/stdout의 마지막 비어있지 않은 JSON 라인을 dict로 반환."""
    captured = capsys.readouterr()
    out = captured.out + captured.err
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return json.loads(lines[-1])


def test_configure_logging_json_format(capsys: Any) -> None:
    """JSON 포맷 시 한 줄 1 JSON, ts/level/logger/message 필드 보장."""
    configure_logging(format_="json")
    logging.getLogger("test_json").info("hello world")
    record = _last_log_line(capsys)
    assert record["level"] == "INFO"
    assert record["logger"] == "test_json"
    assert record["message"] == "hello world"
    assert "ts" in record


def test_configure_logging_plain_format(capsys: Any) -> None:
    """plain 포맷 시 JSON이 아닌 평문 출력."""
    configure_logging(format_="plain")
    logging.getLogger("test_plain").info("plain hello")
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "plain hello" in out
    last_line = [ln for ln in out.splitlines() if "plain hello" in ln][-1]
    try:
        json.loads(last_line)
        json_parsed = True
    except json.JSONDecodeError:
        json_parsed = False
    assert not json_parsed


def test_trace_context_filter_injects_request_user_thread() -> None:
    """ContextVar에 set된 request_id/user_id/thread_id가 record에 부착."""
    rid_token = request_id_var.set("req-abc")
    uid_token = user_id_var.set(42)
    tid_token = thread_id_var.set("thread-1")
    try:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="msg",
            args=(),
            exc_info=None,
        )
        TraceContextFilter().filter(record)
        assert record.request_id == "req-abc"
        assert record.user_id == 42
        assert record.thread_id == "thread-1"
        assert record.trace_id is None
        assert record.span_id is None
    finally:
        request_id_var.reset(rid_token)
        user_id_var.reset(uid_token)
        thread_id_var.reset(tid_token)


def test_trace_context_filter_attaches_trace_id_when_span_active() -> None:
    """활성 OTel span이 있으면 trace_id/span_id가 16/32-hex로 부착."""
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("test-span"):
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="msg",
            args=(),
            exc_info=None,
        )
        TraceContextFilter().filter(record)
        trace_id = record.trace_id
        span_id = record.span_id
        assert trace_id is not None
        assert span_id is not None
        assert len(trace_id) == 32
        assert len(span_id) == 16
