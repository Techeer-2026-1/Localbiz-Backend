"""Google Places Photo API — 장소 카드 썸네일 URL 조회.

흐름: google_place_id → Place Details(fields=photos) → 첫 사진 name
→ media(skipHttpRedirect=true) → photoUri(googleusercontent.com, API 키 불포함) 반환.

Phase 1: 런타임 조회 (캐시는 Phase 2). 실패·사진없음·키없음은 조용히 None/제외 —
카드는 이미지 없이 정상 표시되어야 하므로 절대 예외를 호출부로 던지지 않는다.

불변식: 외부 HTTP는 src.utils.resilience.request_json 사용 (timeout+retry, host-only 로깅 #19).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_PLACES_API_BASE = "https://places.googleapis.com/v1"
_DEFAULT_MAX_WIDTH = 400
_PHOTO_TIMEOUT = 8.0


async def _fetch_photo_url(google_place_id: str, api_key: str, max_width: int) -> Optional[str]:
    """단일 google_place_id → 사진 photoUri. 사진 없거나 실패 시 None."""
    from src.utils.resilience import request_json  # pyright: ignore[reportMissingImports]

    try:
        details = await request_json(
            "GET",
            f"{_PLACES_API_BASE}/places/{google_place_id}?fields=photos&key={api_key}",
            timeout=_PHOTO_TIMEOUT,
        )
    except Exception:
        logger.info("place_photo: Place Details 조회 실패")
        return None

    photos = details.get("photos") if isinstance(details, dict) else None
    if not photos:
        return None
    name = photos[0].get("name") if isinstance(photos[0], dict) else None
    if not name:
        return None

    try:
        media = await request_json(
            "GET",
            f"{_PLACES_API_BASE}/{name}/media?maxWidthPx={max_width}&skipHttpRedirect=true&key={api_key}",
            timeout=_PHOTO_TIMEOUT,
        )
    except Exception:
        logger.info("place_photo: media 조회 실패")
        return None

    if isinstance(media, dict):
        return media.get("photoUri")
    return None


async def fetch_image_urls(
    pool: Any,
    place_ids: list[str],
    api_key: str,
    max_width: int = _DEFAULT_MAX_WIDTH,
) -> dict[str, str]:
    """내부 place_id 리스트 → {place_id: photoUri}.

    1. places에서 google_place_id 일괄 조회 (NULL/빈값 제외)
    2. Photos API 병렬 호출 (장소별 Place Details + media)
    3. 사진이 있는 항목만 반환 — 키가 없는 place_id는 카드에 이미지 미표시

    키 미설정·입력 없음·조회 실패·전부 사진없음이면 빈 dict.
    절대 예외를 호출부로 전파하지 않는다 (이미지는 보조 정보).
    """
    if not place_ids or not api_key:
        return {}

    try:
        rows = await pool.fetch(
            "SELECT place_id, google_place_id FROM places "
            "WHERE place_id::text = ANY($1::text[]) "
            "AND google_place_id IS NOT NULL AND google_place_id <> ''",
            [str(pid) for pid in place_ids],
        )
    except Exception:
        logger.exception("place_photo: google_place_id 조회 실패")
        return {}

    gpid_by_pid: dict[str, str] = {str(r["place_id"]): r["google_place_id"] for r in rows}
    if not gpid_by_pid:
        return {}

    async def _one(pid: str, gpid: str) -> tuple[str, Optional[str]]:
        return pid, await _fetch_photo_url(gpid, api_key, max_width)

    gathered = await asyncio.gather(
        *(_one(pid, gpid) for pid, gpid in gpid_by_pid.items()),
        return_exceptions=True,
    )

    out: dict[str, str] = {}
    for item in gathered:
        if isinstance(item, BaseException):
            continue
        pid, url = item
        if url:
            out[pid] = url
    return out
