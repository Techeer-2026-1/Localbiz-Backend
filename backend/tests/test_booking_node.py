"""booking_node 단위 테스트.

Gemini grounding(_search_booking_info)을 mock으로 대체.
실제 API 호출 없이 라우팅·재질문·캐시 로직만 검증.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.graph.booking_node import _booking_cache, booking_node


# ---------------------------------------------------------------------------
# 공통 픽스처
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def clear_cache() -> None:
    """각 테스트 전 캐시 초기화."""
    _booking_cache.clear()


_FAKE_BOOKING_INFO = "롯데호텔 서울 공식 예약: https://www.lottehotel.com  전화: 02-771-1000"


# ---------------------------------------------------------------------------
# 재질문 로직
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_processed_query_returns_ask() -> None:
    """processed_query 없으면 장소 입력 요청."""
    state = {}
    result = await booking_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert "장소" in blocks[0]["prompt"]


@pytest.mark.asyncio
async def test_missing_place_name_returns_ask() -> None:
    """place_name 없으면 장소명 입력 요청."""
    state = {"processed_query": {"place_name": ""}}
    result = await booking_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert "장소" in blocks[0]["prompt"]


@pytest.mark.asyncio
async def test_accommodation_missing_dates_returns_ask() -> None:
    """숙박 카테고리인데 날짜 없으면 체크인/아웃 요청."""
    state = {
        "processed_query": {
            "place_name": "롯데호텔 서울",
            "category": "호텔",
        }
    }
    result = await booking_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert "체크인" in blocks[0]["prompt"]


@pytest.mark.asyncio
async def test_accommodation_with_dates_proceeds_to_grounding() -> None:
    """숙박 + 날짜 있으면 grounding 호출 후 text_stream 반환."""
    state = {
        "processed_query": {
            "place_name": "롯데호텔 서울",
            "category": "호텔",
            "check_in": "2026-06-10",
            "check_out": "2026-06-12",
        }
    }
    with patch(
        "src.graph.booking_node._search_booking_info",
        new=AsyncMock(return_value=_FAKE_BOOKING_INFO),
    ):
        result = await booking_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert blocks[0]["prompt"] == _FAKE_BOOKING_INFO


# ---------------------------------------------------------------------------
# grounding 정상 동작
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_place_name_only_calls_grounding() -> None:
    """place_name만 있어도 grounding 호출 후 text_stream 반환."""
    state = {"processed_query": {"place_name": "스타벅스 강남"}}

    with patch(
        "src.graph.booking_node._search_booking_info",
        new=AsyncMock(return_value=_FAKE_BOOKING_INFO),
    ) as mock_search:
        result = await booking_node(state)  # type: ignore[arg-type]

    mock_search.assert_awaited_once()
    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert blocks[0]["prompt"] == _FAKE_BOOKING_INFO


@pytest.mark.asyncio
async def test_grounding_error_returns_fallback_message() -> None:
    """grounding 실패해도 fallback 메시지로 text_stream 반환."""
    state = {"processed_query": {"place_name": "테스트 장소"}}

    with patch(
        "src.graph.booking_node._search_booking_info",
        new=AsyncMock(return_value="'테스트 장소' 예약 정보 조회 중 오류가 발생했어요."),
    ):
        result = await booking_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert "오류" in blocks[0]["prompt"]


# ---------------------------------------------------------------------------
# 캐시 동작
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cache_hit_skips_grounding() -> None:
    """동일 장소 + 날짜 두 번 요청 → grounding 1회만 호출."""
    state = {
        "processed_query": {
            "place_name": "블루보틀 성수",
            "check_in": "",
            "check_out": "",
        }
    }

    with patch(
        "src.graph.booking_node._search_booking_info",
        new=AsyncMock(return_value=_FAKE_BOOKING_INFO),
    ) as mock_search:
        await booking_node(state)  # type: ignore[arg-type]
        await booking_node(state)  # type: ignore[arg-type]

    mock_search.assert_awaited_once()


@pytest.mark.asyncio
async def test_different_dates_different_cache_entries() -> None:
    """날짜 다르면 캐시 별도 저장 — 각각 grounding 호출."""
    base = {"place_name": "롯데호텔", "category": "숙박"}
    state_a = {"processed_query": {**base, "check_in": "2026-05-22", "check_out": "2026-05-23"}}
    state_b = {"processed_query": {**base, "check_in": "2026-06-10", "check_out": "2026-06-12"}}

    with patch(
        "src.graph.booking_node._search_booking_info",
        new=AsyncMock(return_value=_FAKE_BOOKING_INFO),
    ) as mock_search:
        await booking_node(state_a)  # type: ignore[arg-type]
        await booking_node(state_b)  # type: ignore[arg-type]

    assert mock_search.await_count == 2
