"""calendar.py list_calendar_events 단위 테스트.

privateExtendedProperty URL 인코딩 검증 + 기본 조회/삭제 흐름.
httpx는 respx로, _get_access_token은 patch로 대체.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import respx
from fastapi import HTTPException
from httpx import Response

from src.api.calendar import list_calendar_events

_ACCESS_TOKEN = "test-access-token"
_USER_ID = 1

_PATCH_TOKEN = "src.api.calendar._get_access_token"


def _mock_event(event_id: str = "evt1", source: str = "localbiz") -> dict[str, Any]:
    return {
        "id": event_id,
        "summary": "테스트 일정",
        "start": {"dateTime": "2026-06-01T10:00:00+09:00"},
        "end": {"dateTime": "2026-06-01T11:00:00+09:00"},
        "extendedProperties": {"private": {"source": source}},
        "htmlLink": "https://calendar.google.com/event?eid=test",
    }


# ---------------------------------------------------------------------------
# privateExtendedProperty 인코딩 검증
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_private_extended_property_not_percent_encoded() -> None:
    """privateExtendedProperty=source=localbiz 의 '='가 %3D로 인코딩되지 않아야 함."""
    captured_url: dict[str, str] = {}

    def capture(request: Any) -> Response:
        captured_url["url"] = str(request.url)
        return Response(200, json={"items": [_mock_event()], "nextPageToken": None})

    with (
        patch(_PATCH_TOKEN, new=AsyncMock(return_value=_ACCESS_TOKEN)),
        respx.mock,
    ):
        respx.get("https://www.googleapis.com/calendar/v3/calendars/primary/events").mock(side_effect=capture)
        await list_calendar_events(user_id=_USER_ID)

    assert "privateExtendedProperty=source=localbiz" in captured_url["url"], (
        f"URL에 raw 'source=localbiz' 가 없음: {captured_url['url']}"
    )
    assert "source%3Dlocalbiz" not in captured_url["url"], f"'=' 가 %3D 로 인코딩됨: {captured_url['url']}"


# ---------------------------------------------------------------------------
# 정상 조회
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_events_returns_items() -> None:
    """정상 응답 시 CalendarEventsResponse.items 반환."""
    with (
        patch(_PATCH_TOKEN, new=AsyncMock(return_value=_ACCESS_TOKEN)),
        respx.mock,
    ):
        respx.get("https://www.googleapis.com/calendar/v3/calendars/primary/events").mock(
            return_value=Response(200, json={"items": [_mock_event()], "nextPageToken": None})
        )
        result = await list_calendar_events(user_id=_USER_ID)

    assert len(result.items) == 1
    assert result.items[0].event_id == "evt1"
    assert result.items[0].source == "localbiz"


@pytest.mark.asyncio
async def test_list_events_empty() -> None:
    """결과 없으면 빈 items 반환."""
    with (
        patch(_PATCH_TOKEN, new=AsyncMock(return_value=_ACCESS_TOKEN)),
        respx.mock,
    ):
        respx.get("https://www.googleapis.com/calendar/v3/calendars/primary/events").mock(
            return_value=Response(200, json={"items": [], "nextPageToken": None})
        )
        result = await list_calendar_events(user_id=_USER_ID)

    assert result.items == []


# ---------------------------------------------------------------------------
# 오류 처리
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_events_google_api_failure_raises_502() -> None:
    """Google API 500 응답 시 502 반환."""
    with (
        patch(_PATCH_TOKEN, new=AsyncMock(return_value=_ACCESS_TOKEN)),
        respx.mock,
    ):
        respx.get("https://www.googleapis.com/calendar/v3/calendars/primary/events").mock(
            return_value=Response(500, json={"error": "internal"})
        )
        with pytest.raises(HTTPException) as exc_info:
            await list_calendar_events(user_id=_USER_ID)

    assert exc_info.value.status_code == 502
