"""외부 API 호출 안정성 유틸 — 재시도·타임아웃·HTTP 래퍼 (#124).

제공:
  - `with_retry`   : 독립 async 함수용 재시도 데코레이터.
  - `retry_call`   : 인라인 await(예: `llm.ainvoke`)를 함수 추출 없이 감싸는 호출형 래퍼.
  - `request_json` : httpx 요청 + timeout + retry + raise_for_status → JSON.

원칙:
  - 재시도는 **멱등 읽기 호출에만**. 비멱등 호출(이벤트 생성·INSERT·스트리밍)에는 쓰지 말 것.
  - 재시도 소진 시 원본 예외를 그대로 전파(`reraise=True`) — 호출부의 기존 except가 동작.
  - 불변식 #19: 재시도/실패 로그에 사용자 query·API 키·full URL(쿼리스트링) 미포함 — 호스트만.

Phase: Infra (공통 인프라 하드닝).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional, TypeVar
from urllib.parse import urlparse

import httpx  # pyright: ignore[reportMissingImports]
from tenacity import (  # pyright: ignore[reportMissingImports]
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

logger = logging.getLogger(__name__)

# 기본값 — 기존 노드 timeout 분포(5~15초)의 중앙값. p99 측정 후 사후 조정 여지.
DEFAULT_TIMEOUT = 10.0
DEFAULT_RETRY_ATTEMPTS = 3
RETRY_BASE_WAIT = 0.5  # 초 — exponential backoff 배수
RETRY_MAX_WAIT = 4.0  # 초 — 단일 대기 상한

# httpx 호출에서 재시도할 5xx/429 상태 코드
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

T = TypeVar("T")


# ---------------------------------------------------------------------------
# 재시도 판정
# ---------------------------------------------------------------------------
def is_retryable_http(exc: BaseException) -> bool:
    """httpx 일시 오류 + 5xx/429 → 재시도 대상. 4xx 등은 즉시 실패."""
    if isinstance(exc, httpx.TransportError):
        # TransportError가 TimeoutException·NetworkError·ConnectError를 포함
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return False


def _log_before_sleep(retry_state: RetryCallState) -> None:
    """재시도 직전 로깅 — 시도횟수·예외 타입명만 (불변식 #19, query/키/URL 미노출)."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(
        "외부 호출 재시도 %d회차 — %s",
        retry_state.attempt_number,
        type(exc).__name__ if exc else "unknown",
    )


def _build_retrying(
    *,
    attempts: int,
    predicate: Callable[[BaseException], bool],
) -> AsyncRetrying:
    """tenacity AsyncRetrying 구성. exponential backoff + jitter, 원본 예외 reraise."""
    return AsyncRetrying(
        stop=stop_after_attempt(attempts),
        wait=wait_random_exponential(multiplier=RETRY_BASE_WAIT, max=RETRY_MAX_WAIT),
        retry=retry_if_exception(predicate),
        before_sleep=_log_before_sleep,
        reraise=True,
    )


# ---------------------------------------------------------------------------
# 호출형 래퍼 / 데코레이터
# ---------------------------------------------------------------------------
async def retry_call(
    func: Callable[[], Awaitable[T]],
    *,
    attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    predicate: Optional[Callable[[BaseException], bool]] = None,
) -> T:
    """인라인 await를 재시도로 감싸는 호출형 래퍼.

    Args:
        func: 호출 시마다 **새 awaitable**을 반환하는 무인자 팩토리.
              예) `lambda: llm.ainvoke(messages)`.
        attempts: 최대 시도 횟수(최초 호출 포함).
        retry_on: 재시도할 예외 타입 튜플. `predicate` 미지정 시 사용.
        predicate: 재시도 여부 판정 함수. 지정 시 `retry_on`보다 우선.

    Returns:
        func()의 결과. 모든 시도 실패 시 마지막 예외를 그대로 raise.
    """
    pred = predicate or (lambda e: isinstance(e, retry_on))
    retrying = _build_retrying(attempts=attempts, predicate=pred)

    # tenacity AsyncRetrying은 coroutine 함수만 await한다. func가 coroutine을
    # 반환하는 일반 callable(팩토리)일 수 있으므로 coroutine 함수로 한 번 감싼다.
    async def _runner() -> T:
        return await func()

    return await retrying(_runner)


def with_retry(
    *,
    attempts: int = DEFAULT_RETRY_ATTEMPTS,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    predicate: Optional[Callable[[BaseException], bool]] = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """독립 async 함수용 재시도 데코레이터. 인자는 `retry_call`과 동일."""

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return await retry_call(
                lambda: fn(*args, **kwargs),
                attempts=attempts,
                retry_on=retry_on,
                predicate=predicate,
            )

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# httpx JSON 요청 래퍼
# ---------------------------------------------------------------------------
async def request_json(
    method: str,
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_RETRY_ATTEMPTS,
    **kwargs: Any,
) -> Any:
    """httpx 요청 + timeout + retry + raise_for_status → JSON 파싱 결과.

    재시도 대상: httpx 일시 오류 + 5xx/429 (`is_retryable_http`). 4xx 등은 즉시 raise.
    모든 시도 실패 시 마지막 예외를 그대로 전파 — 호출부가 기존 except로 graceful 처리.

    `httpx.AsyncClient`는 호출당 생성(기존 노드 동작과 동일 — 성능 회귀 없음).
    """
    host = urlparse(url).hostname or "unknown"

    async def _do() -> Any:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp.json()

    retrying = _build_retrying(attempts=attempts, predicate=is_retryable_http)
    try:
        return await retrying(_do)
    except Exception:
        # 최종 실패 — 호스트만 기록 (#19: full URL/쿼리스트링 미노출)
        logger.warning("외부 HTTP 호출 실패: host=%s method=%s attempts=%d", host, method, attempts)
        raise
