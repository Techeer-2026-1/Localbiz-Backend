"""calendar_node 단위 테스트.

asyncpg pool은 unittest.mock으로, httpx는 respx로, Gemini는 AsyncMock으로 대체.
실제 DB / Google Calendar API / Gemini API 호출 없이 로직만 검증.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
from httpx import Response

from src.graph.calendar_node import _token_cache, calendar_node

_START = "2026-05-02T14:00:00+09:00"
_END = "2026-05-02T16:00:00+09:00"
_LINK = "https://calendar.google.com/calendar/event?eid=test123"

# _extract_calendar_fields mock 경로
_EXTRACT_PATH = "src.graph.calendar_node._extract_calendar_fields"


@pytest.fixture(autouse=True)
def clear_cache() -> None:
    """각 테스트 전 접근 토큰 캐시 초기화."""
    _token_cache.clear()


def _make_pool_mock(has_token: bool = True) -> Any:
    """asyncpg pool mock 생성 헬퍼."""
    row = {"refresh_token": "test-refresh-token"} if has_token else None
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=conn)
    return pool


# ---------------------------------------------------------------------------
# 입력 검증
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_user_id_returns_error() -> None:
    """user_id 미입력 시 로그인 안내 text_stream 블록 반환."""
    state: dict[str, Any] = {
        "processed_query": {"keywords": ["경복궁"]},
        "conversation_history": [],
    }
    result = await calendar_node(state)  # type: ignore[arg-type]
    blocks = result["response_blocks"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text_stream"
    assert "로그인" in blocks[0]["prompt"]


@pytest.mark.asyncio
async def test_missing_event_title_returns_error() -> None:
    """event_title 추출 실패 시 재질문 error 블록 반환."""
    with patch(_EXTRACT_PATH, new=AsyncMock(return_value={"start_time": _START})):
        state: dict[str, Any] = {
            "user_id": 1,
            "processed_query": {},
            "conversation_history": [],
        }
        result = await calendar_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert "제목" in blocks[0]["prompt"]


@pytest.mark.asyncio
async def test_missing_start_time_returns_error() -> None:
    """start_time 추출 실패 시 재질문 text_stream 블록 반환."""
    with patch(_EXTRACT_PATH, new=AsyncMock(return_value={"event_title": "경복궁 방문"})):
        state: dict[str, Any] = {
            "user_id": 1,
            "processed_query": {},
            "conversation_history": [],
        }
        result = await calendar_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert "시작" in blocks[0]["prompt"]


@pytest.mark.asyncio
async def test_extract_failure_returns_re_ask_error() -> None:
    """_extract_calendar_fields 빈 dict 반환 시 재질문 text_stream 블록 반환."""
    with patch(_EXTRACT_PATH, new=AsyncMock(return_value={})):
        state: dict[str, Any] = {
            "user_id": 1,
            "processed_query": {},
            "conversation_history": [],
        }
        result = await calendar_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert "제목" in blocks[0]["prompt"]


# ---------------------------------------------------------------------------
# OAuth 토큰 없음
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_oauth_token_returns_error() -> None:
    """DB에 refresh_token 없으면 Google 연동 안내 error 블록 반환."""
    pool = _make_pool_mock(has_token=False)
    with (
        patch(_EXTRACT_PATH, new=AsyncMock(return_value={"event_title": "경복궁 방문", "start_time": _START})),
        patch("src.graph.calendar_node.get_pool", return_value=pool),
    ):
        state: dict[str, Any] = {
            "user_id": 1,
            "processed_query": {},
            "conversation_history": [],
        }
        result = await calendar_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert blocks[0]["type"] == "text_stream"
    assert "Google" in blocks[0]["prompt"]


# ---------------------------------------------------------------------------
# 성공 케이스
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_successful_creation_returns_calendar_block() -> None:
    """이벤트 생성 성공 시 text_stream + calendar 블록 반환."""
    pool = _make_pool_mock()
    extracted = {
        "event_title": "경복궁 방문",
        "start_time": _START,
        "end_time": _END,
        "location": "경복궁",
    }
    with (
        patch(_EXTRACT_PATH, new=AsyncMock(return_value=extracted)),
        patch("src.graph.calendar_node.get_pool", return_value=pool),
        patch("src.graph.calendar_node.get_settings") as mock_settings,
        respx.mock,
    ):
        mock_settings.return_value.google_calendar_client_id = "cid"
        mock_settings.return_value.google_calendar_client_secret = "csec"
        mock_settings.return_value.gemini_llm_api_key = "gkey"

        respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=Response(200, json={"access_token": "test-access-token"})
        )
        respx.post("https://www.googleapis.com/calendar/v3/calendars/primary/events").mock(
            return_value=Response(200, json={"htmlLink": _LINK})
        )

        state: dict[str, Any] = {
            "user_id": 1,
            "processed_query": {},
            "conversation_history": [],
        }
        result = await calendar_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert len(blocks) == 2

    text_block = blocks[0]
    assert text_block["type"] == "text_stream"

    cal_block = blocks[1]
    assert cal_block["type"] == "calendar"
    assert cal_block["event_title"] == "경복궁 방문"
    assert cal_block["status"] == "created"
    assert cal_block["calendar_link"] == _LINK
    assert cal_block["location"] == "경복궁"


# ---------------------------------------------------------------------------
# 실패 케이스 — API 오류 시 status="failed"
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_calendar_api_failure_returns_failed_status() -> None:
    """Calendar API 500 응답 시 status="failed" + calendar_link=None."""
    pool = _make_pool_mock()
    extracted = {"event_title": "경복궁 방문", "start_time": _START, "end_time": _END}
    with (
        patch(_EXTRACT_PATH, new=AsyncMock(return_value=extracted)),
        patch("src.graph.calendar_node.get_pool", return_value=pool),
        patch("src.graph.calendar_node.get_settings") as mock_settings,
        respx.mock,
    ):
        mock_settings.return_value.google_calendar_client_id = "cid"
        mock_settings.return_value.google_calendar_client_secret = "csec"
        mock_settings.return_value.gemini_llm_api_key = "gkey"

        respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=Response(200, json={"access_token": "test-access-token"})
        )
        respx.post("https://www.googleapis.com/calendar/v3/calendars/primary/events").mock(
            return_value=Response(500, json={"error": "internal"})
        )

        state: dict[str, Any] = {
            "user_id": 1,
            "processed_query": {},
            "conversation_history": [],
        }
        result = await calendar_node(state)  # type: ignore[arg-type]

    blocks = result["response_blocks"]
    assert len(blocks) == 2
    cal_block = blocks[1]
    assert cal_block["status"] == "failed"
    assert cal_block.get("calendar_link") is None


# ---------------------------------------------------------------------------
# end_time 자동 계산
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_end_time_defaults_to_one_hour_later() -> None:
    """end_time 미추출 시 start_time + 1시간으로 자동 계산."""
    pool = _make_pool_mock()
    extracted = {"event_title": "경복궁 방문", "start_time": _START}
    with (
        patch(_EXTRACT_PATH, new=AsyncMock(return_value=extracted)),
        patch("src.graph.calendar_node.get_pool", return_value=pool),
        patch("src.graph.calendar_node.get_settings") as mock_settings,
        respx.mock,
    ):
        mock_settings.return_value.google_calendar_client_id = "cid"
        mock_settings.return_value.google_calendar_client_secret = "csec"
        mock_settings.return_value.gemini_llm_api_key = "gkey"

        respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=Response(200, json={"access_token": "test-access-token"})
        )

        captured: dict[str, Any] = {}

        def capture_request(request: Any) -> Response:
            captured["body"] = json.loads(request.content)
            return Response(200, json={"htmlLink": _LINK})

        respx.post("https://www.googleapis.com/calendar/v3/calendars/primary/events").mock(side_effect=capture_request)

        state: dict[str, Any] = {
            "user_id": 1,
            "processed_query": {},
            "conversation_history": [],
        }
        await calendar_node(state)  # type: ignore[arg-type]

    assert captured["body"]["end"]["dateTime"] == "2026-05-02T15:00:00+09:00"


# ---------------------------------------------------------------------------
# conversation_history 전달 검증
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_conversation_history_forwarded_to_extractor() -> None:
    """conversation_history가 _extract_calendar_fields에 올바르게 전달됨."""
    captured: dict[str, Any] = {}

    async def mock_extract(
        pq: Any,
        history: Any,
    ) -> dict[str, Any]:
        captured["pq"] = pq
        captured["history"] = history
        return {"event_title": "경복궁 방문", "start_time": _START}

    history = [
        {"role": "user", "content": "경복궁 가고 싶어"},
        {"role": "assistant", "content": "네, 일정을 추가할게요."},
    ]
    pool = _make_pool_mock()
    pq = {"keywords": ["경복궁"], "date_reference": "토요일"}

    with (
        patch(_EXTRACT_PATH, side_effect=mock_extract),
        patch("src.graph.calendar_node.get_pool", return_value=pool),
        patch("src.graph.calendar_node.get_settings") as mock_settings,
        respx.mock,
    ):
        mock_settings.return_value.google_calendar_client_id = "cid"
        mock_settings.return_value.google_calendar_client_secret = "csec"
        mock_settings.return_value.gemini_llm_api_key = "gkey"

        respx.post("https://oauth2.googleapis.com/token").mock(
            return_value=Response(200, json={"access_token": "test-access-token"})
        )
        respx.post("https://www.googleapis.com/calendar/v3/calendars/primary/events").mock(
            return_value=Response(200, json={"htmlLink": _LINK})
        )

        state: dict[str, Any] = {
            "user_id": 1,
            "processed_query": pq,
            "conversation_history": history,
        }
        await calendar_node(state)  # type: ignore[arg-type]

    assert captured["pq"] == pq
    assert captured["history"] == history
