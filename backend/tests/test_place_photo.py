"""place_photo 서비스 단위 테스트 — Google Places Photo 조회.

외부 HTTP(request_json) + DB(pool) 모두 mock. 실연결 없음.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.asyncio


def _pool_with_rows(rows: list[dict[str, Any]]) -> AsyncMock:
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=rows)
    return pool


# ---------------------------------------------------------------------------
# fetch_image_urls — 가드
# ---------------------------------------------------------------------------


async def test_fetch_image_urls_empty_key() -> None:
    """키 없으면 빈 dict (외부 호출/DB 조회도 안 함)."""
    from src.services.place_photo import fetch_image_urls  # pyright: ignore[reportMissingImports]

    pool = _pool_with_rows([])
    result = await fetch_image_urls(pool, ["p1"], api_key="")
    assert result == {}
    pool.fetch.assert_not_called()


async def test_fetch_image_urls_empty_place_ids() -> None:
    """place_ids 없으면 빈 dict."""
    from src.services.place_photo import fetch_image_urls  # pyright: ignore[reportMissingImports]

    result = await fetch_image_urls(_pool_with_rows([]), [], api_key="k")
    assert result == {}


async def test_fetch_image_urls_no_google_place_id() -> None:
    """google_place_id 가진 행이 없으면 빈 dict."""
    from src.services.place_photo import fetch_image_urls  # pyright: ignore[reportMissingImports]

    result = await fetch_image_urls(_pool_with_rows([]), ["p1"], api_key="k")
    assert result == {}


# ---------------------------------------------------------------------------
# fetch_image_urls — 성공
# ---------------------------------------------------------------------------


async def test_fetch_image_urls_success() -> None:
    """google_place_id → Place Details → media → photoUri 매핑."""
    from src.services.place_photo import fetch_image_urls  # pyright: ignore[reportMissingImports]

    pool = _pool_with_rows([{"place_id": "p1", "google_place_id": "gp1"}])

    # request_json: 1) Place Details(photos) 2) media(photoUri)
    mock_req = AsyncMock(
        side_effect=[
            {"photos": [{"name": "places/gp1/photos/ref1"}]},
            {"photoUri": "https://lh3.googleusercontent.com/abc=s4800-w400"},
        ]
    )
    with patch("src.utils.resilience.request_json", mock_req):
        result = await fetch_image_urls(pool, ["p1"], api_key="k")

    assert result == {"p1": "https://lh3.googleusercontent.com/abc=s4800-w400"}


async def test_fetch_image_urls_no_photos_excluded() -> None:
    """사진 없는 장소는 결과에서 제외 (에러 아님)."""
    from src.services.place_photo import fetch_image_urls  # pyright: ignore[reportMissingImports]

    pool = _pool_with_rows([{"place_id": "p1", "google_place_id": "gp1"}])
    mock_req = AsyncMock(return_value={"photos": []})  # Place Details에 photos 없음
    with patch("src.utils.resilience.request_json", mock_req):
        result = await fetch_image_urls(pool, ["p1"], api_key="k")

    assert result == {}


async def test_fetch_image_urls_http_error_swallowed() -> None:
    """외부 호출 예외는 삼켜지고 해당 장소만 제외."""
    from src.services.place_photo import fetch_image_urls  # pyright: ignore[reportMissingImports]

    pool = _pool_with_rows([{"place_id": "p1", "google_place_id": "gp1"}])
    mock_req = AsyncMock(side_effect=Exception("502"))
    with patch("src.utils.resilience.request_json", mock_req):
        result = await fetch_image_urls(pool, ["p1"], api_key="k")

    assert result == {}


async def test_fetch_image_urls_db_error_swallowed() -> None:
    """DB 조회 실패 시 빈 dict (예외 전파 안 함)."""
    from src.services.place_photo import fetch_image_urls  # pyright: ignore[reportMissingImports]

    pool = AsyncMock()
    pool.fetch = AsyncMock(side_effect=Exception("db down"))
    result = await fetch_image_urls(pool, ["p1"], api_key="k")
    assert result == {}
