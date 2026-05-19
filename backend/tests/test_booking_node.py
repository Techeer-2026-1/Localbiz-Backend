"""booking_node 단위 테스트.

asyncpg pool은 unittest.mock으로, httpx는 respx로 mock.
실제 DB / Google Places API 호출 없이 로직만 검증.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
from httpx import Response

from src.graph.booking_node import _places_cache, booking_node


# ---------------------------------------------------------------------------
# 공통 픽스처
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def clear_cache() -> None:
    """각 테스트 전 캐시 초기화 — 테스트 간 캐시 오염 방지."""
    _places_cache.clear()


def _make_pool_mock(category: str = "음식점", phone: str = "02-1234-5678") -> Any:
    """asyncpg pool mock 생성 헬퍼.

    pool.acquire() → conn → conn.fetchrow() 체인을 모두 mock.
    """
    row = {"category": category, "phone": phone}
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row)

    # async with pool.acquire() as conn: 패턴을 mock
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


# ---------------------------------------------------------------------------
# 테스트
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_place_name_only_returns_text_stream() -> None:
    """place_id 없이 place_name만 있어도 text_stream 블록 반환 (fallback 동작)."""
    state = {"processed_query": {"place_name": "스타벅스 강남"}}
    result = await booking_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text_stream"


@pytest.mark.asyncio
async def test_missing_place_name_returns_error() -> None:
    """place_name 없으면 text_stream으로 안내 반환."""
    state = {"processed_query": {"place_id": "uuid-001", "place_name": ""}}
    result = await booking_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"


@pytest.mark.asyncio
@respx.mock
async def test_restaurant_with_google_places_returns_text_stream() -> None:
    """음식점 + Google Places mock → text_stream 블록 반환."""
    # Google Places API 응답 mock
    respx.post("https://places.googleapis.com/v1/places:searchText").mock(
        return_value=Response(
            200,
            json={
                "places": [
                    {
                        "websiteUri": "https://example.com",
                        "nationalPhoneNumber": "02-9999-0000",
                    }
                ]
            },
        )
    )

    pool_mock = _make_pool_mock(category="음식점")

    with (
        patch("src.graph.booking_node.get_pool", return_value=pool_mock),
        patch("src.graph.booking_node.get_settings") as mock_settings,
    ):
        mock_settings.return_value.google_places_api_key = "fake-key"

        state = {"processed_query": {"place_id": "uuid-001", "place_name": "스타벅스 강남"}}
        result = await booking_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert "example.com" in blocks[0]["prompt"]
    assert "booking.naver.com" in blocks[0]["prompt"]


@pytest.mark.asyncio
async def test_accommodation_with_dates_returns_yanolja_url() -> None:
    """숙박 + 날짜 있음 → 야놀자/여기어때 URL 포함한 text_stream."""
    pool_mock = _make_pool_mock(category="숙박")

    with (
        patch("src.graph.booking_node.get_pool", return_value=pool_mock),
        patch("src.graph.booking_node.get_settings") as mock_settings,
    ):
        mock_settings.return_value.google_places_api_key = ""

        state = {
            "processed_query": {
                "place_id": "uuid-002",
                "place_name": "롯데호텔 서울",
                "check_in": "2026-05-10",
                "check_out": "2026-05-12",
            }
        }
        result = await booking_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert "yanolja.com" in blocks[0]["prompt"]
    assert "2026-05-10" in blocks[0]["prompt"]


@pytest.mark.asyncio
async def test_accommodation_missing_dates_returns_error() -> None:
    """숙박 + 날짜 없음 → error 블록 반환."""
    pool_mock = _make_pool_mock(category="호텔")

    with (
        patch("src.graph.booking_node.get_pool", return_value=pool_mock),
        patch("src.graph.booking_node.get_settings") as mock_settings,
    ):
        mock_settings.return_value.google_places_api_key = ""

        state = {
            "processed_query": {
                "place_id": "uuid-003",
                "place_name": "롯데호텔 서울",
                # check_in / check_out 없음
            }
        }
        result = await booking_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert "체크인" in blocks[0]["prompt"]


@pytest.mark.asyncio
async def test_tourist_category_returns_tourist_links() -> None:
    """관광지 카테고리 → KOPIS 아닌 네이버/카카오/구글 링크."""
    pool_mock = _make_pool_mock(category="관광지")

    with (
        patch("src.graph.booking_node.get_pool", return_value=pool_mock),
        patch("src.graph.booking_node.get_settings") as mock_settings,
    ):
        mock_settings.return_value.google_places_api_key = ""

        state = {"processed_query": {"place_id": "uuid-006", "place_name": "신라스테이 마포"}}
        result = await booking_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert "booking.naver.com" in blocks[0]["prompt"]
    assert "kopis" not in blocks[0]["prompt"].lower()


@pytest.mark.asyncio
async def test_unavailable_category_returns_text_stream() -> None:
    """예약 불가 카테고리(의료) → text_stream 안내 메시지."""
    pool_mock = _make_pool_mock(category="의료")

    with (
        patch("src.graph.booking_node.get_pool", return_value=pool_mock),
        patch("src.graph.booking_node.get_settings") as mock_settings,
    ):
        mock_settings.return_value.google_places_api_key = ""

        state = {"processed_query": {"place_id": "uuid-007", "place_name": "강남 내과"}}
        result = await booking_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert "의료" in blocks[0]["prompt"]


@pytest.mark.asyncio
async def test_accommodation_different_dates_different_cache() -> None:
    """숙박 날짜 다르면 캐시 별도 저장 — 날짜 오염 없음."""
    pool_mock = _make_pool_mock(category="숙박")

    with (
        patch("src.graph.booking_node.get_pool", return_value=pool_mock),
        patch("src.graph.booking_node.get_settings") as mock_settings,
    ):
        mock_settings.return_value.google_places_api_key = ""

        state_a = {
            "processed_query": {
                "place_id": "uuid-008",
                "place_name": "롯데호텔",
                "check_in": "2026-05-22",
                "check_out": "2026-05-23",
            }
        }
        state_b = {
            "processed_query": {
                "place_id": "uuid-008",
                "place_name": "롯데호텔",
                "check_in": "2026-06-10",
                "check_out": "2026-06-12",
            }
        }
        result_a = await booking_node(state_a)  # type: ignore[arg-type]
        result_b = await booking_node(state_b)  # type: ignore[arg-type]

    prompt_a = result_a["response_blocks"][0]["prompt"]
    prompt_b = result_b["response_blocks"][0]["prompt"]
    assert "2026-05-22" in prompt_a
    assert "2026-06-10" in prompt_b
    assert prompt_a != prompt_b


@pytest.mark.asyncio
async def test_accommodation_missing_checkin_returns_ask() -> None:
    """check_in/check_out 둘 다 없으면 text_stream으로 날짜 요청."""
    state = {
        "processed_query": {
            "place_name": "신라스테이 마포",
            "category": "호텔",
        }
    }
    result = await booking_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert "체크인" in blocks[0]["prompt"]


@pytest.mark.asyncio
@respx.mock
async def test_cache_hit_skips_google_places_api() -> None:
    """캐시 히트 → Google Places API 2회 호출 안 됨."""
    respx.post("https://places.googleapis.com/v1/places:searchText").mock(
        return_value=Response(200, json={"places": []})
    )

    pool_mock = _make_pool_mock(category="카페")
    state = {"processed_query": {"place_id": "uuid-004", "place_name": "블루보틀 성수"}}

    with (
        patch("src.graph.booking_node.get_pool", return_value=pool_mock),
        patch("src.graph.booking_node.get_settings") as mock_settings,
    ):
        mock_settings.return_value.google_places_api_key = "fake-key"

        # 첫 번째 호출 — DB 조회 + API 호출
        await booking_node(state)  # type: ignore[arg-type]
        # 두 번째 호출 — 캐시 히트, API 재호출 없어야 함
        await booking_node(state)  # type: ignore[arg-type]

    # API는 첫 번째 호출 때 1번만 호출됨
    assert respx.calls.call_count == 1


@pytest.mark.asyncio
async def test_tourist_category_with_dates_returns_accommodation_links() -> None:
    """관광지 카테고리 + 날짜 있음 → 숙박 예약 링크 (yanolja/goodchoice)."""
    pool_mock = _make_pool_mock(category="관광지")

    with (
        patch("src.graph.booking_node.get_pool", return_value=pool_mock),
        patch("src.graph.booking_node.get_settings") as mock_settings,
    ):
        mock_settings.return_value.google_places_api_key = ""

        state = {
            "processed_query": {
                "place_id": "uuid-009",
                "place_name": "신라스테이 마포",
                "check_in": "2026-05-22",
                "check_out": "2026-05-23",
            }
        }
        result = await booking_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert "yanolja.com" in blocks[0]["prompt"]
    assert "goodchoice.kr" in blocks[0]["prompt"]


@pytest.mark.asyncio
async def test_unknown_category_returns_naver_fallback() -> None:
    """unknown 카테고리 → 네이버/카카오 fallback URL 포함."""
    pool_mock = _make_pool_mock(category="unknown")

    with (
        patch("src.graph.booking_node.get_pool", return_value=pool_mock),
        patch("src.graph.booking_node.get_settings") as mock_settings,
    ):
        mock_settings.return_value.google_places_api_key = ""

        state = {"processed_query": {"place_id": "uuid-005", "place_name": "어딘가"}}
        result = await booking_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert "search.naver.com" in blocks[0]["prompt"]
