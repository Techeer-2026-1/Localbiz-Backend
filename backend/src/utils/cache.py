"""Graceful-degradation Redis 캐시 (P2-1).

redis-py 미설치 또는 ``REDIS_HOST`` 미설정 시 모든 캐시 함수가 no-op으로 동작.
인프라 추가 없이 도입 가능 — 추후 Memorystore 운영 시 환경변수만 설정.

사용 패턴 (호출부):

    from src.utils.cache import cache_get, cache_set

    cached = await cache_get(key)
    if cached is not None:
        return cached
    result = await expensive_computation()
    await cache_set(key, result, ttl=3600)

캐시 키 규칙 (호출부 책임):
    - sha256(text) → embedding 결과
    - sha256(query + history_summary) → processed_query (TTL 30분, 시간 의존 쿼리 제외)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_REDIS_HOST = os.environ.get("REDIS_HOST", "").strip()
_REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
_REDIS_DB = int(os.environ.get("REDIS_DB", "0"))

_client: Optional[Any] = None
_init_attempted = False


def _get_client() -> Optional[Any]:
    """Lazy init. redis-py 미설치 또는 REDIS_HOST 미설정이면 None."""
    global _client, _init_attempted  # noqa: PLW0603

    if _init_attempted:
        return _client

    _init_attempted = True

    if not _REDIS_HOST:
        logger.info("cache: REDIS_HOST 미설정 — 캐시 비활성화 (no-op)")
        return None

    try:
        import redis.asyncio as redis_async  # pyright: ignore[reportMissingImports]
    except ImportError:
        logger.warning("cache: redis-py 미설치 — 캐시 비활성화. pip install redis로 활성화")
        return None

    try:
        _client = redis_async.Redis(
            host=_REDIS_HOST,
            port=_REDIS_PORT,
            db=_REDIS_DB,
            decode_responses=True,
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
        )
        logger.info("cache: Redis 클라이언트 초기화 host=%s db=%d", _REDIS_HOST, _REDIS_DB)
        return _client
    except Exception:
        logger.exception("cache: Redis 클라이언트 초기화 실패 — 캐시 비활성화")
        _client = None
        return None


async def cache_get(key: str) -> Optional[Any]:
    """캐시 조회. Redis 미가용·미스 시 None."""
    client = _get_client()
    if client is None:
        return None
    try:
        raw = await client.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception:
        logger.exception("cache_get 실패 — None 반환")
        return None


async def cache_set(key: str, value: Any, ttl: int = 3600) -> bool:
    """캐시 저장. Redis 미가용 시 False."""
    client = _get_client()
    if client is None:
        return False
    try:
        await client.set(key, json.dumps(value, ensure_ascii=False), ex=ttl)
        return True
    except Exception:
        logger.exception("cache_set 실패 — skip")
        return False


async def cache_close() -> None:
    """lifespan shutdown에서 호출. 미가용 시 no-op."""
    global _client  # noqa: PLW0603
    if _client is None:
        return
    try:
        await _client.aclose()
    except Exception:
        logger.exception("cache_close 실패 — 무시")
    finally:
        _client = None
