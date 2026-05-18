"""CALENDAR intent 노드 — Google Calendar 일정 추가 (P1).

흐름:
  processed_query + conversation_history → Gemini 합산 → 이벤트 필드 추출
  → user_id로 user_oauth_tokens 테이블에서 refresh_token 조회
  → refresh_token으로 access_token 발급 (TTLCache 58분 캐시)
  → Google Calendar API 호출 → 이벤트 생성
  → text_stream 블록 + calendar 블록 반환

API 명세서 CALENDAR 블록 순서: intent → text_stream → calendar → done
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from cachetools import TTLCache

from src.config import get_settings  # pyright: ignore[reportMissingImports]
from src.db.postgres import get_pool  # pyright: ignore[reportMissingImports]
from src.graph.state import AgentState  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

# 접근 토큰 캐시 — user_id 기준, 58분 (구글 기본 만료 1시간보다 2분 짧게)
_token_cache: TTLCache = TTLCache(maxsize=1000, ttl=3480)


def clear_token_cache(user_id: int) -> None:
    """user_id의 access_token 캐시 무효화. Google 재연동 시 호출."""
    _token_cache.pop(user_id, None)


_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_KST = timezone(timedelta(hours=9))

# 캘린더 이벤트 필드 추출 시스템 프롬프트
# {{, }}로 이스케이프 — .format(today=...) 호출 시 { }로 변환됨
_CALENDAR_EXTRACT_PROMPT = """\
캘린더 이벤트 생성에 필요한 정보를 구조화하라.

아래 JSON 형식으로만 응답하라. 마크다운·설명 금지.
{{
  "event_title": "일정 제목 (문자열, 불명확하면 null)",
  "start_time": "ISO 8601 KST 예: 2026-05-02T14:00:00+09:00 (불명확하면 null)",
  "end_time": "ISO 8601 KST (언급 없으면 null)",
  "location": "장소명 (언급 없으면 null)",
  "description": "대화 맥락에서 파악한 장소 설명·코스 메모·특이사항 (없으면 null)",
  "url": "대화에서 언급된 예약 URL 또는 장소 상세 링크 (없으면 null)"
}}

규칙:
- 현재 일시(KST): {today}
- 상대 날짜("내일", "이번 주 토요일" 등)는 오늘 기준으로 절대 날짜로 변환.
- 시간은 KST(+09:00) 기준. 오전/오후 표현 그대로 반영.
- 이벤트 제목은 키워드에서 자연스럽게 유추. 예) keywords=["경복궁"] → "경복궁 방문".
- 캘린더와 무관한 값("바보" 등)은 null 처리.
"""


class _CalendarError(Exception):
    """사용자 안내가 필요한 캘린더 오류."""


async def _load_history_from_db(thread_id: str) -> list[dict[str, str]]:
    """messages 테이블에서 최근 대화 이력을 직접 조회. 불변식 #3: SELECT만."""
    try:
        rows = await get_pool().fetch(
            "SELECT role, blocks FROM messages WHERE thread_id = $1 ORDER BY message_id DESC LIMIT 10",
            thread_id,
        )
    except Exception:
        logger.warning("calendar_node: DB 이력 조회 실패 → 빈 이력으로 진행")
        return []

    history: list[dict[str, str]] = []
    for row in reversed(rows):
        role: str = row["role"]
        blocks = row["blocks"]
        if isinstance(blocks, str):
            try:
                blocks = json.loads(blocks)
            except Exception:
                continue
        text_parts: list[str] = []
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text" and role == "user":
                text_parts.append(block.get("content", ""))
            elif btype == "text_stream" and role == "assistant":
                text_parts.append(block.get("content", ""))
        content = " ".join(t for t in text_parts if t).strip()
        if content:
            history.append({"role": role, "content": content})
    return history


async def calendar_node(state: AgentState) -> dict[str, Any]:
    """CALENDAR intent 노드 — Google Calendar 이벤트 생성.

    processed_query + conversation_history를 Gemini로 합산해
    이벤트 필드를 추출한 뒤 Google Calendar API로 이벤트를 생성하고
    text_stream 블록 + calendar 블록을 반환한다.

    Args:
        state: AgentState. user_id 필수. processed_query, conversation_history 선택.

    Returns:
        {"response_blocks": [text_stream 블록, calendar 블록]} 또는 error 블록.
    """
    user_id: Optional[int] = state.get("user_id")
    if not user_id:
        return {"response_blocks": [_error_block("로그인이 필요합니다.")]}

    pq: Optional[dict[str, Any]] = state.get("processed_query")
    history: list[dict[str, str]] = state.get("conversation_history") or []

    # sse.py가 conversation_history를 빈 배열로 넘겨도
    # thread_id로 messages 테이블에서 직접 조회 (불변식 #3: SELECT만)
    if not history:
        thread_id: Optional[str] = state.get("thread_id")
        if thread_id:
            history = await _load_history_from_db(thread_id)

    extracted = await _extract_calendar_fields(pq, history)

    event_title: Optional[str] = extracted.get("event_title")
    start_time: Optional[str] = extracted.get("start_time")
    end_time: Optional[str] = extracted.get("end_time")
    location: Optional[str] = extracted.get("location")
    description: Optional[str] = extracted.get("description")
    url: Optional[str] = extracted.get("url")

    # 필수 필드 미입력 시 재질문 블록 반환
    if not event_title:
        return {"response_blocks": [_reask_block("일정 제목이 없어요. 어떤 일정인가요? 예) '경복궁 투어'")]}

    if not start_time:
        return {"response_blocks": [_reask_block("언제 시작하는 일정인가요? 예) '5월 2일 오후 2시'")]}

    # ISO 형식 유효성 검증
    try:
        datetime.fromisoformat(start_time)
    except ValueError:
        return {"response_blocks": [_reask_block("언제 시작하는 일정인가요? 예) '5월 2일 오후 2시'")]}

    if end_time:
        try:
            datetime.fromisoformat(end_time)
        except ValueError:
            end_time = None

    # end_time 미입력 시 1시간 자동 추가
    if not end_time:
        end_time = _add_one_hour(start_time)

    try:
        access_token = await _get_access_token(user_id)
        calendar_link = await _create_event(
            access_token=access_token,
            event_title=event_title,
            start_time=start_time,
            end_time=end_time,
            location=location,
            description=description,
            url=url,
        )
        status = "created"
    except _CalendarError as e:
        return {"response_blocks": [_error_block(str(e))]}
    except Exception:
        logger.exception("calendar_node: 이벤트 생성 실패")
        calendar_link = None
        status = "failed"

    return {
        "response_blocks": [
            _text_stream_block(event_title, start_time, status, end_time=end_time, location=location),
            _calendar_block(
                event_title=event_title,
                start_time=start_time,
                end_time=end_time,
                location=location,
                calendar_link=calendar_link,
                status=status,
            ),
        ]
    }


async def _extract_calendar_fields(
    processed_query: Optional[dict[str, Any]],
    conversation_history: list[dict[str, str]],
) -> dict[str, Any]:
    """processed_query + conversation_history에서 캘린더 이벤트 필드 추출.

    Gemini 2.5 Flash JSON mode로 event_title, start_time, end_time, location 반환.
    입력 없거나 추출 실패 시 빈 dict 반환 (caller가 재질문 처리).

    Args:
        processed_query: query_preprocessor 출력 dict (date_reference, time_reference 등).
        conversation_history: 과거 대화 내역 (role/content dict 리스트).

    Returns:
        {"event_title": ..., "start_time": ..., "end_time": ..., "location": ...}.
        누락 필드는 None, 전체 실패 시 빈 dict.
    """
    # 입력 없음 — Gemini 호출 불필요
    if not processed_query and not conversation_history:
        return {}

    from langchain_google_genai import ChatGoogleGenerativeAI  # pyright: ignore[reportMissingImports]

    settings = get_settings()
    if not settings.gemini_llm_api_key:
        logger.warning("calendar_node: GEMINI_LLM_API_KEY 미설정 — 필드 추출 생략")
        return {}

    now_kst = datetime.now(_KST).strftime("%Y-%m-%d %H:%M")
    system_prompt = _CALENDAR_EXTRACT_PROMPT.format(today=now_kst)

    # 사용자 메시지 구성
    parts: list[str] = []
    if processed_query:
        parts.append(f"processed_query:\n{json.dumps(processed_query, ensure_ascii=False)}")
    if conversation_history:
        # 과거 대화 최근 10턴 제한
        recent = conversation_history[-10:]
        history_text = "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in recent)
        parts.append(f"conversation_history:\n{history_text}")

    user_content = "\n\n".join(parts)

    try:
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=settings.gemini_llm_api_key,
            temperature=0,
        )
        response = await llm.ainvoke(
            [
                ("system", system_prompt),
                ("human", user_content),
            ]
        )
        raw = str(response.content).strip()
        # 마크다운 코드 블록 제거
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        result: dict[str, Any] = json.loads(raw)
        return result
    except Exception:
        logger.exception("calendar_node: 캘린더 필드 추출 실패")
        return {}


async def _get_access_token(user_id: int) -> str:
    """user_oauth_tokens 테이블에서 refresh_token 조회 후 access_token 발급.

    access_token은 TTLCache(user_id 기준)로 캐시해 중복 발급 방지.

    Raises:
        _CalendarError: refresh_token 없거나 발급 실패 시.
    """
    if user_id in _token_cache:
        return str(_token_cache[user_id])

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT refresh_token FROM user_oauth_tokens
            WHERE user_id = $1
              AND provider = 'google'
              AND scope LIKE '%calendar%'
              AND is_deleted = false
            ORDER BY created_at DESC
            LIMIT 1
            """,
            user_id,
        )

    if not row:
        raise _CalendarError("Google Calendar 연동이 필요합니다. Google 계정으로 로그인해 주세요.")

    settings = get_settings()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            _GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.google_calendar_client_id,
                "client_secret": settings.google_calendar_client_secret,
                "refresh_token": row["refresh_token"],
                "grant_type": "refresh_token",
            },
        )

    if resp.status_code != 200:
        logger.warning("calendar_node: access_token 발급 실패 status=%d", resp.status_code)
        raise _CalendarError("Google Calendar 연동에 실패했습니다. 다시 시도해 주세요.")

    access_token: str = resp.json()["access_token"]
    _token_cache[user_id] = access_token
    return access_token


async def _create_event(
    access_token: str,
    event_title: str,
    start_time: str,
    end_time: Optional[str],
    location: Optional[str],
    description: Optional[str] = None,
    url: Optional[str] = None,
) -> str:
    """Google Calendar API로 이벤트 생성 후 htmlLink 반환.

    Raises:
        httpx.HTTPStatusError: API 호출 실패 시 (caller에서 status="failed" 처리).
    """
    event_body: dict[str, Any] = {
        "summary": event_title,
        "start": {"dateTime": start_time, "timeZone": "Asia/Seoul"},
        "end": {"dateTime": end_time, "timeZone": "Asia/Seoul"},
        "extendedProperties": {"private": {"source": "localbiz"}},
    }
    if location:
        event_body["location"] = location
    if description:
        event_body["description"] = description
    if url:
        event_body["source"] = {"title": "AnyWay", "url": url}

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            _GOOGLE_CALENDAR_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            json=event_body,
        )

    resp.raise_for_status()
    return str(resp.json()["htmlLink"])


def _add_one_hour(iso_time: str) -> Optional[str]:
    """ISO 8601 문자열에 1시간 추가. 파싱 실패 시 None 반환."""
    try:
        dt = datetime.fromisoformat(iso_time)
        return (dt + timedelta(hours=1)).isoformat()
    except ValueError:
        logger.warning("calendar_node: end_time 계산 실패 — iso_time=%s", iso_time)
        return None


def _text_stream_block(
    event_title: str,
    start_time: str,
    status: str,
    end_time: Optional[str] = None,
    location: Optional[str] = None,
) -> dict[str, Any]:
    """이벤트 생성 결과 안내용 text_stream 블록 생성."""
    if status == "created":
        details: list[str] = [start_time]
        if end_time:
            details.append(f"~{end_time}")
        if location:
            details.append(location)
        prompt = f"'{event_title}' 일정을 Google Calendar에 추가했어요. ({', '.join(details)})"
    else:
        prompt = "죄송합니다. Google Calendar 일정 추가에 실패했습니다. 잠시 후 다시 시도해 주세요."

    return {
        "type": "text_stream",
        "system": "Google Calendar 일정 추가 결과를 친절하게 안내하세요. 불필요한 내용은 추가하지 마세요. 마크다운 강조(**) 없이 대화하듯 자연스럽게 답하세요.",
        "prompt": prompt,
    }


def _calendar_block(
    event_title: str,
    start_time: str,
    end_time: Optional[str],
    location: Optional[str],
    calendar_link: Optional[str],
    status: str,
) -> dict[str, Any]:
    """calendar SSE 블록 생성."""
    block: dict[str, Any] = {
        "type": "calendar",
        "event_title": event_title,
        "start_time": start_time,
        "status": status,
    }
    if end_time:
        block["end_time"] = end_time
    if location:
        block["location"] = location
    if calendar_link:
        block["calendar_link"] = calendar_link
    return block


def _reask_block(prompt: str) -> dict[str, Any]:
    """필수 정보 부족 시 재질문용 text_stream 블록 생성."""
    return {
        "type": "text_stream",
        "system": "캘린더 일정 추가를 위해 필요한 정보가 부족합니다. 친절하고 자연스럽게 추가 정보를 요청하세요. 마크다운 강조(**) 없이 대화하듯 짧고 간결하게 답하세요.",
        "prompt": prompt,
    }


def _error_block(message: str) -> dict[str, Any]:
    """error SSE 블록 생성."""
    return {"type": "error", "message": message}
