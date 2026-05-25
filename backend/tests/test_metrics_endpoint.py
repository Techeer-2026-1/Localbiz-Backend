"""`/metrics` 엔드포인트 단위 테스트 (#155).

Instrumentator가 등록한 Prometheus exposition 응답 형식·기본 메트릭 키 검증.

TestClient를 **컨텍스트 매니저 없이** 사용해 lifespan(startup/shutdown)을 우회한다.
다른 테스트가 src/db/opensearch.py·postgres.py의 모듈 전역(`_client`/`_pool`)에
MagicMock을 남겨놓는 경우가 있어, lifespan shutdown이 `await _client.close()`에서
`MagicMock can't be used in 'await'` TypeError를 던지기 때문(테스트 격리 결함).
/metrics 자체는 lifespan 무관 — Instrumentator 미들웨어만으로 동작.
"""

from __future__ import annotations

from fastapi.testclient import TestClient  # pyright: ignore[reportMissingImports]


def test_metrics_endpoint_returns_prometheus_format() -> None:
    """/metrics가 200 + Prometheus 텍스트 포맷 + 기본 메트릭 키 포함."""
    from src.main import app  # pyright: ignore[reportMissingImports]

    client = TestClient(app)

    # /metrics 호출 전에 비-제외 엔드포인트 1회 호출해 메트릭 한 줄이라도 생성
    client.get("/openapi.json")

    resp = client.get("/metrics")

    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")

    body = resp.text
    # Instrumentator 기본 메트릭 키 존재 (default 함수 — metrics.py L673-724)
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body


def test_metrics_excludes_health_and_metrics_handlers() -> None:
    """/health·/metrics 자체 호출은 통계에서 제외 (excluded_handlers)."""
    from src.main import app  # pyright: ignore[reportMissingImports]

    client = TestClient(app)

    # /health를 여러 번 때려도 메트릭에 잡히면 안 됨
    for _ in range(3):
        client.get("/health")

    resp = client.get("/metrics")

    body = resp.text
    # excluded_handlers=["/health", "/metrics"] — handler 라벨에 이 경로가 안 나와야 함
    assert 'handler="/health"' not in body
    assert 'handler="/metrics"' not in body
