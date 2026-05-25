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

    try:
        from opentelemetry import trace  # pyright: ignore[reportMissingImports]
        from opentelemetry.exporter.jaeger.thrift import JaegerExporter  # pyright: ignore[reportMissingImports]
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # pyright: ignore[reportMissingImports]
        from opentelemetry.sdk.trace import TracerProvider  # pyright: ignore[reportMissingImports]
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # pyright: ignore[reportMissingImports]

        exporter = JaegerExporter(
            agent_host_name=jaeger_host,
            agent_port=int(os.getenv("JAEGER_PORT", "6831")),
        )
        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app)
        logger.info("Jaeger tracing 활성화: %s:6831", jaeger_host)
    except Exception:
        logger.warning("Jaeger tracing 설정 실패 — 계속 진행", exc_info=True)
