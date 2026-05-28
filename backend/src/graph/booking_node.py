"""BOOKING intent 노드 — 예약 정보 안내 (P1).

Google Search grounding으로 장소별 실제 예약 방법을 실시간 검색해서 안내.
하드코딩 URL 없이 Gemini가 판단: 공식 사이트 링크, 전화번호, 플랫폼 링크 등.

API 명세서 BOOKING 블록 순서: intent → text_stream → done
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from cachetools import TTLCache

from src.config import get_settings  # pyright: ignore[reportMissingImports]
from src.graph.state import AgentState  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

# 동일 장소 반복 질문 시 재검색 방지 (1시간)
_booking_cache: TTLCache = TTLCache(maxsize=200, ttl=3600)

_ACCOM_CATS = {"숙박", "호텔", "모텔", "게스트하우스", "민박", "여관", "accommodation", "hotel"}

_BOOKING_SYSTEM = (
    "너는 서울 로컬 라이프 AI 챗봇의 예약 안내 도우미야.\n"
    "사용자가 원하는 장소의 예약 방법을 웹 검색으로 찾아서 아래 기준으로 안내해:\n"
    "- 공식 온라인 예약 사이트가 있으면 → 링크 제공\n"
    "- 숙박이면 야놀자·공식 홈페이지 등 예약 플랫폼 링크\n"
    "- 전화 예약만 가능하면 → 전화번호 안내\n"
    "- 공공시설은 서울시 공공서비스예약 링크 또는 기관 연락처\n"
    "- 운영시간·가격·주의사항 핵심만 포함\n"
    "- 확실히 작동하는 링크만. 불확실하면 링크 제외\n"
    "- vertexaisearch.cloud.google.com, grounding-api-redirect 등 내부 검색 참조 URL은 절대 사용 금지\n"
    "- 예약 방법·링크·전화번호를 확실히 확인하지 못하면 추측하지 말고, "
    "'온라인 예약 정보를 찾지 못했어요. 방문 전 직접 확인을 권해요'라고 솔직히 안내해\n"
    "- 간결하게 핵심만. 불필요한 설명 없이."
)


async def booking_node(state: AgentState) -> dict[str, Any]:
    """BOOKING intent 노드.

    processed_query에서 place_name / category / 날짜를 꺼내
    Gemini grounding으로 실제 예약 정보를 검색 후 text_stream 블록 반환.
    """
    pq: Optional[dict[str, Any]] = state.get("processed_query")

    if not pq:
        return {"response_blocks": [_ask_block("예약할 장소를 알 수 없습니다. 장소 이름을 포함해 말씀해 주세요.")]}

    place_name: str = (pq.get("place_name") or "").strip()
    if not place_name:
        return {"response_blocks": [_ask_block("예약할 장소 이름을 알려주세요. 예) '스타벅스 강남 예약해줘'")]}

    category: str = (pq.get("category") or "").lower()
    check_in: str = pq.get("check_in") or ""
    check_out: str = pq.get("check_out") or ""

    # 숙박 카테고리인데 날짜 없으면 재질문
    if any(c in category for c in _ACCOM_CATS) and not (check_in and check_out):
        return {
            "response_blocks": [_ask_block("체크인/체크아웃 날짜를 알려주세요. 예) '6월 10일 체크인, 12일 체크아웃'")]
        }

    cache_key = f"{place_name}:{category}:{check_in}:{check_out}"
    if cache_key in _booking_cache:
        logger.info("booking_node: cache hit place=%s", place_name)
        return {"response_blocks": [_text_stream_block(_booking_cache[cache_key])]}

    booking_info = await _search_booking_info(place_name, category, check_in, check_out)
    if booking_info is not None:
        _booking_cache[cache_key] = booking_info
    else:
        booking_info = f"'{place_name}' 예약 정보 조회 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요."

    return {"response_blocks": [_text_stream_block(booking_info)]}


async def _search_booking_info(
    place_name: str,
    category: str,
    check_in: str,
    check_out: str,
) -> Optional[str]:
    """Gemini Google Search grounding으로 예약 정보 검색. 실패 시 None 반환."""
    from google import genai  # pyright: ignore[reportMissingImports,reportAttributeAccessIssue]
    from google.genai import types  # pyright: ignore[reportMissingImports]

    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_llm_api_key)

    date_ctx = f" (체크인 {check_in}, 체크아웃 {check_out})" if check_in and check_out else ""
    cat_ctx = f" (카테고리: {category})" if category and category != "unknown" else ""

    prompt = f"{_BOOKING_SYSTEM}\n\n사용자가 '{place_name}'{cat_ctx}{date_ctx} 예약을 원해."

    try:
        resp = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        return resp.text or f"'{place_name}' 예약 정보를 찾지 못했어요. 직접 검색하거나 전화 문의해 주세요."
    except Exception:
        logger.warning("booking_node: grounding 실패 place=%s", place_name)
        return None


def _text_stream_block(booking_info: str) -> dict[str, Any]:
    return {
        "type": "text_stream",
        "system": (
            "사용자에게 예약 정보를 안내합니다. 아래 내용을 친절하게 전달하되 URL과 전화번호는 절대 변경하지 마세요."
        ),
        "prompt": booking_info,
    }


def _ask_block(message: str) -> dict[str, Any]:
    return {
        "type": "text_stream",
        "system": "사용자에게 필요한 정보를 친절하게 요청하세요.",
        "prompt": message,
    }
