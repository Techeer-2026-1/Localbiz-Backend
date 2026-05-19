"""BOOKING intent 노드 — 예약 딥링크 연동 (P1).

장소 카테고리별 예약 딥링크를 text_stream 블록으로 반환한다.
  - 음식점/카페/주점: Google Places API 실시간 조회 → 예약 URL / 전화번호 추출
  - 숙박: 야놀자/여기어때 URL 패턴 (check_in/check_out 필수)
  - 공공시설: yeyak.seoul.go.kr URL 패턴
  - 문화/관광: KOPIS + 인터파크 URL 패턴
  - 기타: 네이버/카카오 fallback

P1 범위 밖 (후속 plan):
  서울시 공공서비스예약 API 실시간 연동 / KOPIS API 실시간 연동

API 명세서 BOOKING 블록 순서: intent → text_stream → done
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import quote  # 한글 장소명을 URL에 안전하게 인코딩

import httpx
from cachetools import TTLCache  # 인메모리 LRU+TTL 캐시

from src.config import get_settings  # pyright: ignore[reportMissingImports]
from src.db.postgres import get_pool  # pyright: ignore[reportMissingImports]
from src.graph.state import AgentState  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 캐시 — Google Places API 호출 결과를 place_id 기준으로 1시간 보관
# 같은 장소를 여러 번 예약 요청해도 API를 재호출하지 않음
# ---------------------------------------------------------------------------
_places_cache: TTLCache = TTLCache(maxsize=500, ttl=3600)

# Google Places New API (Text Search v1) 엔드포인트
_GOOGLE_PLACES_URL = "https://places.googleapis.com/v1/places:searchText"

# ---------------------------------------------------------------------------
# 카테고리 분류 집합 — DB 18종 한글값 기준 + Gemini 추론값 alias 포함
# any(c in cat for c in _CATS) 패턴으로 substring 매칭
# ---------------------------------------------------------------------------
_RESTAURANT_CATS = {"음식점", "카페", "주점", "restaurant", "cafe", "pub", "bar"}
_ACCOMMODATION_CATS = {"숙박", "호텔", "모텔", "게스트하우스", "민박", "여관", "accommodation", "hotel"}
_PUBLIC_CATS = {"공공시설", "공원", "체육시설", "도서관", "복지시설", "public", "sports", "park", "library"}
_CULTURAL_CATS = {"문화시설", "문화", "공연", "전시", "영화관", "박물관", "미술관", "cultural"}
_TOURIST_CATS = {"관광지", "관광", "tourist"}
# 예약 연동 미지원 카테고리 — 안내 메시지만 반환
_UNAVAILABLE_CATS = {"의료", "쇼핑", "미용", "뷰티", "주차장", "지하철역", "노래방"}


# ---------------------------------------------------------------------------
# 내부 예외 — 사용자에게 알려야 할 오류 (에러 블록으로 변환)
# ---------------------------------------------------------------------------
class _BookingError(Exception):
    """예약 처리 중 사용자에게 안내가 필요한 오류."""


# ---------------------------------------------------------------------------
# 메인 노드 함수
# ---------------------------------------------------------------------------
async def booking_node(state: AgentState) -> dict[str, Any]:
    """BOOKING intent 노드.

    processed_query에서 place_id / place_name을 꺼내
    카테고리별 딥링크를 생성하고 text_stream 블록으로 반환.

    Args:
        state: AgentState. processed_query 필드에 place_id, place_name 필수.

    Returns:
        {"response_blocks": [text_stream 블록 또는 error 블록]}.
    """
    pq: Optional[dict[str, Any]] = state.get("processed_query")

    # ── 입력 검증 ──────────────────────────────────────────────────────────
    if not pq:
        logger.warning("booking_node: processed_query 없음")
        return {"response_blocks": [_ask_block("예약할 장소를 알 수 없습니다. 장소 이름을 포함해 말씀해 주세요.")]}

    place_name: str = pq.get("place_name", "").strip()
    if not place_name:
        logger.warning("booking_node: place_name 없음")
        return {"response_blocks": [_ask_block("예약할 장소 이름을 알려주세요. 예) '스타벅스 강남 예약해줘'")]}

    place_id: Optional[str] = pq.get("place_id")
    category: str = "unknown"
    phone: Optional[str] = None

    # ── place_id 있으면 캐시/DB 조회 ──────────────────────────────────────
    check_in_val: str = pq.get("check_in") or ""
    check_out_val: str = pq.get("check_out") or ""
    cache_key = f"{place_id}:{check_in_val}:{check_out_val}" if place_id else ""

    if place_id:
        if cache_key in _places_cache:
            logger.info("booking_node: cache hit place_id=%s", place_id)
            return {"response_blocks": [_text_stream_block(_places_cache[cache_key])]}

        # places 테이블에서 카테고리와 전화번호 조회 (불변식 #8: asyncpg $1 바인딩)
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT category, phone FROM places WHERE place_id = $1",
                place_id,
            )
        category = (row["category"] if row else "") or "unknown"
        phone = row["phone"] if row else None
        logger.info(
            "booking_node: place_id=%s category=%s check_in=%s check_out=%s",
            place_id,
            category,
            check_in_val,
            check_out_val,
        )
    else:
        # place_id 없으면 processed_query의 category 사용
        category = pq.get("category", "") or "unknown"
        logger.info(
            "booking_node: place_name=%s category=%s check_in=%s check_out=%s (no place_id)",
            place_name,
            category,
            check_in_val,
            check_out_val,
        )

    # ── 카테고리별 딥링크 생성 ────────────────────────────────────────────
    try:
        links_text = await _build_links(category, place_name, phone, pq)
    except _BookingError as e:
        # 날짜 누락 등 — text_stream으로 안내 (error 블록은 FE에서 빈 메시지로 처리됨)
        return {"response_blocks": [_ask_block(str(e))]}

    # ── 캐시 저장 후 반환 (place_id 있을 때만) ───────────────────────────
    if place_id:
        _places_cache[cache_key] = links_text
    return {"response_blocks": [_text_stream_block(links_text)]}


# ---------------------------------------------------------------------------
# 카테고리 분기
# ---------------------------------------------------------------------------
async def _build_links(
    category: str,
    place_name: str,
    phone: Optional[str],
    pq: dict[str, Any],
) -> str:
    """카테고리에 따라 딥링크 텍스트를 생성한다.

    Raises:
        _BookingError: 숙박 카테고리에서 날짜 누락 시.
    """
    cat = category.lower()

    # check_in/check_out 둘 다 있으면 관광지·unknown이라도 숙박으로 처리
    # (DB에 관광지로 잘못 저장된 호텔 대응)
    has_dates = bool(pq.get("check_in") and pq.get("check_out"))

    if any(c in cat for c in _UNAVAILABLE_CATS):
        return _build_unavailable_message(category)
    elif any(c in cat for c in _RESTAURANT_CATS):
        return await _build_restaurant_links(place_name, phone)
    elif any(c in cat for c in _ACCOMMODATION_CATS):
        return _build_accommodation_links(place_name, pq)  # 날짜 없으면 raise
    elif any(c in cat for c in _PUBLIC_CATS):
        return _build_public_links(place_name)
    elif any(c in cat for c in _CULTURAL_CATS):
        return _build_cultural_links(place_name)
    elif any(c in cat for c in _TOURIST_CATS):
        if has_dates:
            return _build_accommodation_links(place_name, pq)
        return _build_tourist_links(place_name)
    else:
        if has_dates:
            return _build_accommodation_links(place_name, pq)
        return _build_fallback_links(place_name)


# ---------------------------------------------------------------------------
# 카테고리별 딥링크 빌더
# ---------------------------------------------------------------------------
async def _build_restaurant_links(place_name: str, db_phone: Optional[str]) -> str:
    """음식점/카페/주점 — Google Places API 조회 후 딥링크 생성.

    Google Places API 필드 우선순위:
      1. websiteUri — 공식 홈페이지
      2. nationalPhoneNumber — 전화번호

    API 실패 / 키 없음 → 에러 전파 없이 URL 패턴 fallback.
    """
    settings = get_settings()
    # 한글 장소명을 URL에 안전하게 인코딩 (예: "롯데호텔" → "%EB%A1%AF%EB%8D%B0%ED%98%B8%ED%85%94")
    encoded = quote(place_name, safe="")

    website_uri: Optional[str] = None
    google_phone: Optional[str] = None

    # Google Places API 호출 — 키가 있을 때만 시도
    if settings.google_places_api_key:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    _GOOGLE_PLACES_URL,
                    headers={
                        "X-Goog-Api-Key": settings.google_places_api_key,
                        # FieldMask: 필요한 필드만 요청해서 비용 절감
                        "X-Goog-FieldMask": "places.websiteUri,places.nationalPhoneNumber",
                    },
                    json={"textQuery": place_name, "languageCode": "ko"},
                )
            if resp.status_code == 200:
                places = resp.json().get("places", [])
                if places:
                    p = places[0]
                    website_uri = p.get("websiteUri")
                    google_phone = p.get("nationalPhoneNumber")
        except Exception:
            # 타임아웃/네트워크 오류 → URL 패턴 fallback (사용자에게 에러 노출 안 함)
            logger.warning("booking_node: Google Places API 실패 → fallback")

    phone = google_phone or db_phone
    lines = ["📍 **예약하기**\n"]

    if website_uri:
        lines.append(f"🌐 [공식 홈페이지]({website_uri})")

    # URL 패턴 기반 링크 (항상 포함)
    lines.append(f"🔵 [네이버 예약](https://booking.naver.com/search?query={encoded})")
    lines.append(f"🟡 [카카오맵](https://place.map.kakao.com/search?q={encoded})")

    if phone:
        lines.append(f"📞 전화 예약: {phone}")

    return "\n".join(lines)


def _build_accommodation_links(place_name: str, pq: dict[str, Any]) -> str:
    """숙박 — 야놀자/여기어때 URL 패턴.

    Raises:
        _BookingError: check_in 또는 check_out이 없을 때.
            대화 담당 팀원이 날짜를 수집해서 넘겨줘야 하는데 누락된 경우.
    """
    check_in: Optional[str] = pq.get("check_in")
    check_out: Optional[str] = pq.get("check_out")

    # 날짜 없으면 노드 자체에서 방어 — "대화 팀원이 보장한다" 신뢰 가정 금지
    if not check_in or not check_out:
        raise _BookingError("체크인/체크아웃 날짜를 알려주세요. 예) '5월 10일 체크인, 5월 12일 체크아웃'")

    encoded = quote(place_name, safe="")
    lines = [
        "🏨 **숙박 예약하기**\n",
        f"🟠 [야놀자](https://www.yanolja.com/search?keyword={encoded}&checkIn={check_in}&checkOut={check_out})",
        f"🔴 [여기어때](https://www.goodchoice.kr/search?keyword={encoded}&checkIn={check_in}&checkOut={check_out})",
    ]
    return "\n".join(lines)


def _build_public_links(place_name: str) -> str:
    """공공시설 — 서울시 공공서비스예약 URL 패턴.

    P1: URL 패턴만. 서울시 API 실시간 연동은 후속 plan.
    """
    encoded = quote(place_name, safe="")
    lines = [
        "🏛️ **공공시설 예약하기**\n",
        f"🔵 [서울시 공공서비스예약](https://yeyak.seoul.go.kr/search?keyword={encoded})",
    ]
    return "\n".join(lines)


def _build_cultural_links(place_name: str) -> str:
    """문화/관광 — KOPIS + 인터파크 URL 패턴.

    P1: URL 패턴만. KOPIS API 실시간 연동은 후속 plan.
    """
    encoded = quote(place_name, safe="")
    lines = [
        "🎭 **문화/공연 예약하기**\n",
        f"🎫 [KOPIS](https://www.kopis.or.kr/search?query={encoded})",
        f"🎟️ [인터파크](https://ticket.interpark.com/search?query={encoded})",
    ]
    return "\n".join(lines)


def _build_tourist_links(place_name: str) -> str:
    """관광지 — 네이버/카카오/구글 검색 fallback."""
    encoded = quote(place_name, safe="")
    lines = [
        "🗺️ **관광지 예약/입장 안내**\n",
        f"🔵 [네이버 예약](https://booking.naver.com/search?query={encoded})",
        f"🟡 [카카오맵](https://place.map.kakao.com/search?q={encoded})",
        f"🌐 [구글 검색](https://www.google.com/search?q={encoded}+예약)",
    ]
    return "\n".join(lines)


def _build_unavailable_message(category: str) -> str:
    """예약 연동 미지원 카테고리 안내."""
    return (
        f"'{category}' 카테고리는 온라인 예약 연동을 지원하지 않아요. "
        "해당 장소에 직접 문의하시거나 네이버/카카오맵에서 검색해 보세요."
    )


def _build_fallback_links(place_name: str) -> str:
    """기타/unknown 카테고리 — 네이버/카카오 검색 fallback."""
    encoded = quote(place_name, safe="")
    lines = [
        "📍 **예약 링크**\n",
        f"🔵 [네이버 검색](https://search.naver.com/search.naver?query={encoded}+예약)",
        f"🟡 [카카오맵](https://place.map.kakao.com/search?q={encoded})",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 블록 생성 헬퍼
# ---------------------------------------------------------------------------
def _text_stream_block(links_text: str) -> dict[str, Any]:
    """text_stream 블록 생성.

    sse.py가 이 블록을 받으면 Gemini astream()으로 스트리밍.
    system에 URL 보존 지시, prompt에 딥링크 텍스트를 담아
    Gemini가 친절한 안내 문구와 함께 링크를 그대로 출력하게 함.

    API 명세서 BOOKING: "intent → text_stream → done / text_stream에 딥링크 포함"
    """
    return {
        "type": "text_stream",
        "system": (
            "사용자에게 예약 링크를 안내합니다. "
            "아래 제공된 링크와 내용을 그대로 전달하되 친절하게 안내 문구를 추가하세요. "
            "URL 주소는 절대 변경하거나 생략하지 마세요."
        ),
        "prompt": links_text,
    }


def _ask_block(message: str) -> dict[str, Any]:
    """추가 정보 요청 text_stream 블록 — FE가 error 이벤트를 빈 메시지로 처리하므로 text_stream 사용."""
    return {
        "type": "text_stream",
        "system": "사용자에게 필요한 정보를 친절하게 요청하세요. URL은 포함하지 마세요.",
        "prompt": message,
    }


def _error_block(message: str) -> dict[str, Any]:
    """error 블록 생성 (불변식 #10: 16종 SSE 타입 중 error)."""
    return {"type": "error", "message": message}
