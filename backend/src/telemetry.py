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
    # service.name 미설정 시 Jaeger UI에 'unknown_service'로 표시됨. OTEL_SERVICE_NAME 환경변수 우선, default 'localbiz-api'.
    # 빈/공백 값은 default로 대체 — env에 빈 문자열만 들어있으면 unknown_service로 다시 떨어지는 함정 회피.
    service_name = os.getenv("OTEL_SERVICE_NAME", "").strip() or "localbiz-api"
    # /health, /metrics는 GCE 헬스체크·Prometheus scrape로 초마다 호출돼 trace를 도배함 — 분석 노이즈 회피.
    # 각 항목 trim — env에 `"/health, /metrics"` 형태로 공백 섞여 들어와도 정확히 매칭되도록.
    excluded_urls = ",".join(
        u.strip() for u in os.getenv("OTEL_EXCLUDED_URLS", "/health,/metrics").split(",") if u.strip()
    )

    # 1) 의존성 누락은 ImportError로 명확히 구분 — silent fail 회피, 무엇이 빠졌는지 로그에 노출.
    try:
        from opentelemetry import trace  # pyright: ignore[reportMissingImports]
        from opentelemetry.exporter.jaeger.thrift import JaegerExporter  # pyright: ignore[reportMissingImports]
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # pyright: ignore[reportMissingImports]
        from opentelemetry.sdk.resources import Resource  # pyright: ignore[reportMissingImports]
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
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app, excluded_urls=excluded_urls)
        logger.info(
            "Jaeger tracing 활성화: service=%s host=%s port=%d excluded=%s",
            service_name,
            jaeger_host,
            jaeger_port,
            excluded_urls,
        )
    except Exception:
        logger.warning(
            "Jaeger tracing 초기화 실패 (service=%s host=%s port=%d) — 계속 진행",
            service_name,
            jaeger_host,
            jaeger_port,
            exc_info=True,
        )
