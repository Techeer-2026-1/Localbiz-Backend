"""JSON 구조화 로깅 + trace 컨텍스트 자동 주입.

`configure_logging()`은 환경 변수 `LOG_FORMAT` (`json` | `plain`, default `json`) 으로
포맷을 분기한다.

JSON 모드:
  - python-json-logger의 `JsonFormatter`로 모든 레코드를 한 줄 JSON으로 직렬화
  - `TraceContextFilter`가 활성 OTel span의 `trace_id`/`span_id`와
    `src.observability.context`의 ContextVar(`request_id`/`user_id`/`thread_id`)를 주입
  - Loki promtail이 stdout을 수집 → LogQL `{app="localbiz-api"} | json | trace_id != ""`
  - Grafana derived field로 trace_id 클릭 → Jaeger 점프

Plain 모드:
  - 기존 `basicConfig` 호환 평문 포맷 — 로컬 가독성용

소음 모듈(`httpx`, `httpcore`, `asyncpg`, `opentelemetry.exporter`)은 WARNING으로
quiet-down 하여 비즈니스 로그가 묻히지 않도록.
"""

from __future__ import annotations

import logging
import os
from typing import Literal, Optional

from opentelemetry import trace  # pyright: ignore[reportMissingImports]
from pythonjsonlogger import jsonlogger  # pyright: ignore[reportMissingImports]

from src.observability.context import request_id_var, thread_id_var, user_id_var

LogFormat = Literal["json", "plain"]

# WARNING으로 낮출 noisy logger 이름 — JSON 로그 한 줄당 의미 있는 비즈니스 이벤트만 남기기.
_NOISY_LOGGERS: tuple[str, ...] = (
    "httpx",
    "httpcore",
    "asyncpg",
    "opentelemetry.exporter",
    "opentelemetry.exporter.jaeger",
    "urllib3",
)


class UvicornAccessFilter(logging.Filter):
    """uvicorn.access 로거에서 /health, /metrics 요청 로그를 drop.

    헬스체크/메트릭 스크랩이 초당 호출돼 비즈니스 로그가 묻히고 Loki 적재 비용도
    증가하는 문제 회피. uvicorn.access 표준 args 위치는
    (client_addr, method, full_path, http_version, status_code) — 3번째가 path.
    형식이 다르면 getMessage() fallback.
    """

    _EXCLUDED_PATHS: tuple[str, ...] = ("/health", "/metrics")

    def filter(self, record: logging.LogRecord) -> bool:
        path = ""
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            third = record.args[2]
            if isinstance(third, str):
                path = third
        if not path:
            path = record.getMessage()
        for excluded in self._EXCLUDED_PATHS:
            if excluded in path:
                return False
        return True


class TraceContextFilter(logging.Filter):
    """모든 로그 레코드에 trace/request 컨텍스트 필드를 주입.

    JsonFormatter는 record의 `__dict__`에 있는 모든 키를 출력에 포함시키므로
    여기서 setattr만 하면 자동으로 JSON 필드로 노출된다.

    필드 시멘틱스:
      - `trace_id`/`span_id`: 활성 OTel span. span이 없으면 None.
      - `request_id`/`user_id`/`thread_id`: SSE 핸들러 등이 ContextVar로 주입한 값.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            record.trace_id = format(span_context.trace_id, "032x")
            record.span_id = format(span_context.span_id, "016x")
        else:
            record.trace_id = None
            record.span_id = None
        record.request_id = request_id_var.get()
        record.user_id = user_id_var.get()
        record.thread_id = thread_id_var.get()
        return True


def _build_handler(format_: LogFormat) -> logging.Handler:
    handler = logging.StreamHandler()
    formatter: logging.Formatter
    if format_ == "json":
        formatter = jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
        )
    else:
        formatter = logging.Formatter("%(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)
    handler.addFilter(TraceContextFilter())
    return handler


def _resolve_format_from_env() -> LogFormat:
    raw = os.getenv("LOG_FORMAT", "json").strip().lower()
    if raw == "plain":
        return "plain"
    return "json"


def configure_logging(format_: Optional[LogFormat] = None, level: int = logging.INFO) -> None:
    """루트 로거를 재설정. 기존 핸들러는 모두 제거 후 단일 stdout 핸들러로 교체.

    Args:
        format_: "json" 또는 "plain". None이면 환경변수 `LOG_FORMAT` 사용, 그것도 없으면 "json".
        level: 루트 로거 레벨. 기본 INFO.
    """
    fmt: LogFormat = format_ or _resolve_format_from_env()
    handler = _build_handler(fmt)
    root = logging.getLogger()
    # 기존 핸들러 제거 — uvicorn 등이 미리 부착했을 수 있음.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)
    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # uvicorn.access는 INFO로 두되 /health·/metrics만 drop — 비즈니스 API 호출 로그는 유지.
    logging.getLogger("uvicorn.access").addFilter(UvicornAccessFilter())
    root.info("logging configured: format=%s level=%s", fmt, logging.getLevelName(level))
