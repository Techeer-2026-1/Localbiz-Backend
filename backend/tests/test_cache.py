"""P2-1: cache 모듈 graceful degradation 단위 테스트."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_cache_get_returns_none_without_redis_host() -> None:
    """REDIS_HOST 미설정 시 cache_get은 None."""
    import importlib

    import src.utils.cache as cache_mod  # pyright: ignore[reportMissingImports]

    cache_mod._REDIS_HOST = ""  # type: ignore[attr-defined]
    cache_mod._client = None  # type: ignore[attr-defined]
    cache_mod._init_attempted = False  # type: ignore[attr-defined]

    result = await cache_mod.cache_get("any_key")
    assert result is None

    importlib.reload(cache_mod)


@pytest.mark.asyncio
async def test_cache_set_returns_false_without_redis_host() -> None:
    """REDIS_HOST 미설정 시 cache_set은 False."""
    import importlib

    import src.utils.cache as cache_mod  # pyright: ignore[reportMissingImports]

    cache_mod._REDIS_HOST = ""  # type: ignore[attr-defined]
    cache_mod._client = None  # type: ignore[attr-defined]
    cache_mod._init_attempted = False  # type: ignore[attr-defined]

    result = await cache_mod.cache_set("any_key", {"data": 1}, ttl=60)
    assert result is False

    importlib.reload(cache_mod)


@pytest.mark.asyncio
async def test_cache_close_safe_when_disabled() -> None:
    """cache_close는 미가용 시 예외 없이 종료."""
    import importlib

    import src.utils.cache as cache_mod  # pyright: ignore[reportMissingImports]

    cache_mod._client = None  # type: ignore[attr-defined]
    await cache_mod.cache_close()

    importlib.reload(cache_mod)
