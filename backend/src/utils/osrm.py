"""OSRM polyline 유틸 (P3-B).

``OSRM_URL`` 환경변수 미설정 시 모든 함수 None 반환 → 호출부 직선 polyline fallback.
인프라 추가 없이 도입 가능 — 추후 OSRM 인스턴스 운영 시 환경변수만 설정.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

_OSRM_URL = os.environ.get("OSRM_URL", "").strip().rstrip("/")
_OSRM_TIMEOUT = float(os.environ.get("OSRM_TIMEOUT", "2.0"))
_OSRM_PROFILE = os.environ.get("OSRM_PROFILE", "foot")


def is_enabled() -> bool:
    """OSRM 활성화 여부."""
    return bool(_OSRM_URL)


async def fetch_segment(
    from_lng: float,
    from_lat: float,
    to_lng: float,
    to_lat: float,
) -> Optional[list[list[float]]]:
    """두 좌표 사이 OSRM polyline. None 반환 시 호출부에서 직선 fallback."""
    if not _OSRM_URL:
        return None

    url = (
        f"{_OSRM_URL}/route/v1/{_OSRM_PROFILE}/"
        f"{from_lng},{from_lat};{to_lng},{to_lat}"
        "?geometries=geojson&overview=full&steps=false"
    )

    try:
        async with httpx.AsyncClient(timeout=_OSRM_TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        logger.warning("osrm fetch_segment 실패 — 직선 fallback")
        return None

    routes = data.get("routes") or []
    if not routes:
        return None
    geometry = routes[0].get("geometry") or {}
    coords = geometry.get("coordinates")
    if not isinstance(coords, list) or not coords:
        return None
    return coords
