"""src/utils/resilience.py 단위 테스트 (#124).

retry_call / with_retry / request_json / is_retryable_http 검증.
backoff 대기는 monkeypatch로 0으로 만들어 테스트를 빠르게 유지.
"""

from __future__ import annotations

from typing import Any

import httpx  # pyright: ignore[reportMissingImports]
import pytest

from src.utils import resilience  # pyright: ignore[reportMissingImports]

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: Any) -> None:
    """재시도 대기를 0으로 — 테스트 속도."""
    monkeypatch.setattr(resilience, "RETRY_BASE_WAIT", 0.0)
    monkeypatch.setattr(resilience, "RETRY_MAX_WAIT", 0.0)


# ---------------------------------------------------------------------------
# is_retryable_http
# ---------------------------------------------------------------------------
async def test_is_retryable_http_transport_error() -> None:
    """httpx 일시 오류(TransportError 계열)는 재시도 대상."""
    assert resilience.is_retryable_http(httpx.ConnectError("boom")) is True
    assert resilience.is_retryable_http(httpx.ReadTimeout("slow")) is True


async def test_is_retryable_http_status() -> None:
    """5xx/429는 재시도, 4xx는 비재시도."""
    req = httpx.Request("GET", "http://x")

    def _status_err(code: int) -> httpx.HTTPStatusError:
        return httpx.HTTPStatusError("e", request=req, response=httpx.Response(code, request=req))

    assert resilience.is_retryable_http(_status_err(503)) is True
    assert resilience.is_retryable_http(_status_err(429)) is True
    assert resilience.is_retryable_http(_status_err(404)) is False
    assert resilience.is_retryable_http(ValueError("not http")) is False


# ---------------------------------------------------------------------------
# retry_call
# ---------------------------------------------------------------------------
async def test_retry_call_succeeds_after_transient_failures() -> None:
    """일시 실패 N회 후 성공 — 결과 반환."""
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("transient")
        return "ok"

    result = await resilience.retry_call(flaky, attempts=3, retry_on=(ConnectionError,))
    assert result == "ok"
    assert calls["n"] == 3


async def test_retry_call_exhausts_and_reraises() -> None:
    """모든 시도 실패 시 마지막 예외를 그대로 raise (reraise)."""
    calls = {"n": 0}

    async def always_fail() -> str:
        calls["n"] += 1
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        await resilience.retry_call(always_fail, attempts=3, retry_on=(ConnectionError,))
    assert calls["n"] == 3


async def test_retry_call_does_not_retry_unmatched_exception() -> None:
    """retry_on에 없는 예외는 즉시 전파 — 재시도 안 함."""
    calls = {"n": 0}

    async def fail_value() -> str:
        calls["n"] += 1
        raise ValueError("not retryable")

    with pytest.raises(ValueError):
        await resilience.retry_call(fail_value, attempts=3, retry_on=(ConnectionError,))
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# with_retry 데코레이터
# ---------------------------------------------------------------------------
async def test_with_retry_decorator() -> None:
    """데코레이터 형태도 동일하게 재시도."""
    calls = {"n": 0}

    @resilience.with_retry(attempts=3, retry_on=(ConnectionError,))
    async def flaky(x: int) -> int:
        calls["n"] += 1
        if calls["n"] < 2:
            raise ConnectionError("transient")
        return x * 2

    assert await flaky(21) == 42
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# request_json — httpx.AsyncClient 대역으로 검증
# ---------------------------------------------------------------------------
class _FakeClient:
    """httpx.AsyncClient 대역. request() 호출마다 outcomes를 순서대로 소비."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = outcomes
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> _FakeClient:
        return self  # httpx.AsyncClient(timeout=...) 호출 → 자기 자신 반환

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return httpx.Response(outcome, json={"ok": True}, request=httpx.Request(method, url))


async def test_request_json_success(monkeypatch: Any) -> None:
    """정상 200 응답 → JSON 반환."""
    fake = _FakeClient([200])
    monkeypatch.setattr(httpx, "AsyncClient", fake)

    result = await resilience.request_json("GET", "https://api.example.com/v1/search?q=x")
    assert result == {"ok": True}
    assert fake.calls == 1


async def test_request_json_retries_on_503(monkeypatch: Any) -> None:
    """503 → 재시도 후 200 성공."""
    fake = _FakeClient([503, 200])
    monkeypatch.setattr(httpx, "AsyncClient", fake)

    result = await resilience.request_json("GET", "https://api.example.com/x", attempts=3)
    assert result == {"ok": True}
    assert fake.calls == 2


async def test_request_json_no_retry_on_404(monkeypatch: Any) -> None:
    """404(4xx)는 재시도 없이 즉시 raise."""
    fake = _FakeClient([404])
    monkeypatch.setattr(httpx, "AsyncClient", fake)

    with pytest.raises(httpx.HTTPStatusError):
        await resilience.request_json("GET", "https://api.example.com/x", attempts=3)
    assert fake.calls == 1
