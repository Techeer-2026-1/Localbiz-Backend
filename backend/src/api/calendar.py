"""Google Calendar 이벤트 조회/삭제 엔드포인트 (Phase 1).

흐름:
  GET  /api/v1/users/me/calendar/events
    → user_oauth_tokens에서 refresh_token 조회 → access_token 발급
    → Google Calendar events.list API 호출 → items[] 반환
  DELETE /api/v1/users/me/calendar/events/{event_id}
    → access_token 발급 → Google Calendar events.delete API 호출

우리 DB에 별도 저장 없음. Google Calendar가 source of truth.
FE CalendarPanel이 소비. 403 시 "Google Calendar 연동" CTA 표시.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from src.api.deps import get_current_user_id  # pyright: ignore[reportMissingImports]
from src.graph.calendar_node import _CalendarError, _get_access_token  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/users/me/calendar", tags=["calendar"])

_KST = timezone(timedelta(hours=9))
_GOOGLE_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


class CalendarEventItem(BaseModel):
    event_id: str
    title: str
    start_time: Optional[str]
    end_time: Optional[str]
    location: Optional[str]
    description: Optional[str]
    calendar_link: Optional[str]
    source: str  # "localbiz" | "user"


class CalendarEventsResponse(BaseModel):
    items: list[CalendarEventItem]
    next_page_token: Optional[str]


@router.get("/events", response_model=CalendarEventsResponse, status_code=status.HTTP_200_OK)
async def list_calendar_events(
    time_min: Optional[str] = Query(None),
    time_max: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=250),
    user_id: int = Depends(get_current_user_id),
) -> CalendarEventsResponse:
    """Google Calendar 이벤트 목록 조회. DB 미저장 — Google 직접 조회."""
    try:
        access_token = await _get_access_token(user_id)
    except _CalendarError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="google_calendar_not_connected",
        ) from e

    now = datetime.now(_KST)
    effective_time_min = time_min or now.isoformat()
    effective_time_max = time_max or (now + timedelta(days=90)).isoformat()

    params: dict[str, Any] = {
        "timeMin": effective_time_min,
        "timeMax": effective_time_max,
        "maxResults": limit,
        "singleEvents": "true",
        "orderBy": "startTime",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            _GOOGLE_EVENTS_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )

    if resp.status_code != 200:
        logger.warning("calendar: events.list 실패 status=%d user_id=%s", resp.status_code, user_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google Calendar API 호출 실패",
        )

    data = resp.json()
    items: list[CalendarEventItem] = []
    for raw in data.get("items", []):
        start = raw.get("start", {})
        end = raw.get("end", {})
        ext_private = raw.get("extendedProperties", {}).get("private", {})
        items.append(
            CalendarEventItem(
                event_id=raw.get("id", ""),
                title=raw.get("summary", ""),
                start_time=start.get("dateTime") or start.get("date"),
                end_time=end.get("dateTime") or end.get("date"),
                location=raw.get("location"),
                description=raw.get("description"),
                calendar_link=raw.get("htmlLink"),
                source=ext_private.get("source", "user"),
            )
        )

    return CalendarEventsResponse(
        items=items,
        next_page_token=data.get("nextPageToken"),
    )


@router.delete("/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_calendar_event(
    event_id: str,
    user_id: int = Depends(get_current_user_id),
) -> None:
    """Google Calendar 이벤트 삭제."""
    try:
        access_token = await _get_access_token(user_id)
    except _CalendarError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="google_calendar_not_connected",
        ) from e

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.delete(
            f"{_GOOGLE_EVENTS_URL}/{event_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if resp.status_code == 404:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="이벤트를 찾을 수 없습니다.",
        )
    if resp.status_code not in (200, 204):
        logger.warning("calendar: events.delete 실패 status=%d event_id=%s", resp.status_code, event_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google Calendar API 호출 실패",
        )
