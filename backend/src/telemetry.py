"""OpenTelemetry Jaeger tracing 설정.

JAEGER_HOST 환경변수가 없으면 no-op — 로컬 개발 영향 없음.
GCE 배포 시 .env에 JAEGER_HOST=10.178.0.4 추가.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def setup_tracing(app: FastAPI) -> None:
    jaeger_host = os.getenv("JAEGER_HOST", "").strip()
    if not jaeger_host:
        return

    jaeger_port = int(os.getenv("JAEGER_PORT", "6831"))

    # 1) 의존성 누락은 ImportError로 명확히 구분 — silent fail 회피, 무엇이 빠졌는지 로그에 노출.
    try:
        from opentelemetry import trace  # pyright: ignore[reportMissingImports]
        from opentelemetry.exporter.jaeger.thrift import JaegerExporter  # pyright: ignore[reportMissingImports]
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # pyright: ignore[reportMissingImports]
        from opentelemetry.sdk.trace import TracerProvider  # pyright: ignore[reportMissingImports]
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        logger.warning(
            "Jaeger tracing 의존성 누락 (%s) — `pip install opentelemetry-api opentelemetry-sdk "
            "opentelemetry-exporter-jaeger opentelemetry-instrumentation-fastapi` 필요. tracing 비활성.",
            exc,
        )
        return

    # 2) 런타임 초기화 실패는 별도 처리 — host/port를 로그에 포함해 진단 가능.
    try:
        exporter = JaegerExporter(agent_host_name=jaeger_host, agent_port=jaeger_port)
        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app)
        logger.info("Jaeger tracing 활성화: %s:%d", jaeger_host, jaeger_port)
    except Exception:
        logger.warning(
            "Jaeger tracing 초기화 실패 (host=%s, port=%d) — 계속 진행",
            jaeger_host,
            jaeger_port,
            exc_info=True,
        )
