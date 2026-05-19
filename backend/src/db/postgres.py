"""asyncpg 커넥션 풀 관리.

lifespan 진입 시 init_pool(), 종료 시 close_pool() 호출.
쿼리 시 get_pool()로 풀 획득 후 pool.fetch / pool.execute 사용.
모든 SQL은 파라미터 바인딩($1, $2) 필수 — f-string SQL 금지 (불변식 #8).
"""

from __future__ import annotations

from typing import Optional

import asyncpg

from src.config import get_settings

# 모듈 레벨 풀 참조. init_pool()에서 초기화.
_pool: Optional[asyncpg.Pool] = None  # type: ignore[type-arg]


async def init_pool() -> asyncpg.Pool:  # type: ignore[type-arg]
    """asyncpg 커넥션 풀 생성. lifespan startup에서 호출."""
    global _pool  # noqa: PLW0603
    if _pool is not None:
        return _pool
    settings = get_settings()
    _pool = await asyncpg.create_pool(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        min_size=2,
        max_size=10,
        # 쿼리당 타임아웃(초) — DB 장애 시 무한 대기 방지 (#124 안정성 고도화)
        command_timeout=10,
    )
    return _pool


def get_pool() -> asyncpg.Pool:  # type: ignore[type-arg]
    """현재 초기화된 풀 반환. init_pool() 전 호출 시 RuntimeError."""
    if _pool is None:
        raise RuntimeError("DB pool이 초기화되지 않았습니다. lifespan에서 init_pool()을 호출하세요.")
    return _pool


async def close_pool() -> None:
    """커넥션 풀 종료. lifespan shutdown에서 호출."""
    global _pool  # noqa: PLW0603
    if _pool is not None:
        await _pool.close()
        _pool = None
