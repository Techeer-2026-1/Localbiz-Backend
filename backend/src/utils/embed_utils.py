"""Gemini 768d 임베딩 공통 진입점 (P2-2).

여러 노드에 흩어져 있던 인라인 `_embed_query_768d`를 단일 함수로 통합.
캐시(P2-1, REDIS_HOST 설정 시)는 본 함수가 hook 지점.

불변식 #7: 768d Gemini embedding만. OpenAI 임베딩 금지.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

_EMBED_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"
_EMBED_DIM = 768
_EMBED_TTL = 60 * 60 * 24  # 24h


async def embed_query(query: str, api_key: str) -> list[float]:
    """Gemini gemini-embedding-001 단건 768d 임베딩.

    P2-1 캐시 hook: REDIS_HOST 설정 시 sha256(query) → 결과 캐싱.

    Args:
        query: 사용자 쿼리. 2000자 초과는 절단.
        api_key: GEMINI_LLM_API_KEY.

    Returns:
        768차원 float 리스트. 실패 시 0.0 × 768.
    """
    text = (query or "")[:2000]
    if not text:
        return [0.0] * _EMBED_DIM

    cache_key = "embed:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    try:
        from src.utils.cache import cache_get  # pyright: ignore[reportMissingImports]

        cached = await cache_get(cache_key)
        if isinstance(cached, list) and len(cached) == _EMBED_DIM:
            return cached
    except Exception:
        logger.exception("embed_query: cache_get 실패 — 무시")

    from src.utils.resilience import request_json  # pyright: ignore[reportMissingImports]

    body: dict[str, Any] = {
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": text}]},
        "outputDimensionality": _EMBED_DIM,
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }

    try:
        data = await request_json("POST", _EMBED_URL, json=body, headers=headers, timeout=10)
        values = data.get("embedding", {}).get("values") or [0.0] * _EMBED_DIM
    except Exception:
        logger.exception("embed_query: Gemini 호출 실패 — 0-vector 반환")
        return [0.0] * _EMBED_DIM

    if len(values) != _EMBED_DIM:
        logger.warning("embed_query: dim 불일치(%d) — 0-vector", len(values))
        return [0.0] * _EMBED_DIM

    try:
        from src.utils.cache import cache_set  # pyright: ignore[reportMissingImports]

        await cache_set(cache_key, values, ttl=_EMBED_TTL)
    except Exception:
        logger.exception("embed_query: cache_set 실패 — 무시")

    return values
