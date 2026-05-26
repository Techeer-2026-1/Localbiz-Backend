"""DETAIL_INQUIRY 노드 — 장소 상세 조회.

PLACE_SEARCH와의 차이:
  - DETAIL_INQUIRY: 단건 조회 + Gemini 자연어 소개 (특정 장소 상세)
  - PLACE_SEARCH: 목록 검색 (PG+OS 하이브리드, 여러 후보)

검색 흐름:
  1. processed_query에서 neighborhood/keywords/expanded_query 추출
  2. PostgreSQL: places WHERE name ILIKE + is_deleted=false → LIMIT 1
  3. 매칭 성공 → text_stream(Gemini 요약) + place(단일 PlaceBlock dict)
  4. 매칭 실패 → text_stream "장소를 특정할 수 없습니다" fallback

불변식 #1: places PK = UUID(VARCHAR 36)
불변식 #4: is_deleted=false 필터
불변식 #8: asyncpg $1 바인딩
불변식 #9: Optional[str] 사용
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DETAIL_INQUIRY_SYSTEM_PROMPT = (
    "당신은 서울 로컬 라이프 AI 챗봇 'AnyWay'입니다. "
    "자기소개나 인사로 시작하지 말고 바로 본론으로 답변하세요. "
    "사용자가 특정 장소의 상세 정보를 물어보고 있습니다. "
    "장소의 위치, 카테고리, 특징 등을 친절하게 소개해주세요."
)

_FALLBACK_MESSAGE = "장소를 특정할 수 없습니다. 장소명을 포함해서 다시 질문해주세요."

_DETAIL_SQL = (
    "SELECT place_id, name, category, address, district, "
    "ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng "
    "FROM places "
    "WHERE is_deleted = false AND name ILIKE $1 ESCAPE '\\' "
    "LIMIT 1"
)


# ---------------------------------------------------------------------------
# 검색어 추출
# ---------------------------------------------------------------------------
def _escape_like(term: str) -> str:
    """ILIKE 와일드카드 문자(%,_)를 이스케이프. 백슬래시 우선."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _extract_search_term(
    processed_query: dict[str, Any],
    fallback_query: str,
) -> str:
    """processed_query에서 장소 검색어를 추출.

    우선순위: place_name → keywords[0] → expanded_query → neighborhood → 원본 query.
    place_name은 대화 맥락에서 해소된 장소명 (예: "거기" → "스타벅스 강남점").
    """
    place_name: Optional[str] = processed_query.get("place_name")
    if place_name and place_name.strip():
        return place_name.strip()

    keywords: list[str] = processed_query.get("keywords", [])
    if keywords and keywords[0].strip():
        return keywords[0].strip()

    expanded: Optional[str] = processed_query.get("expanded_query")
    if expanded and expanded.strip():
        return expanded.strip()

    neighborhood: Optional[str] = processed_query.get("neighborhood")
    if neighborhood and neighborhood.strip():
        return neighborhood.strip()

    return fallback_query


# ---------------------------------------------------------------------------
# 블록 생성 (순수 함수 — 테스트 용이)
# ---------------------------------------------------------------------------
def _build_detail_blocks(
    query: str,
    place_row: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    """장소 조회 결과 → text_stream + place 블록 생성.

    Args:
        query: 사용자 원본 쿼리.
        place_row: places 테이블 SELECT 결과 dict. None이면 매칭 실패.

    Returns:
        response_blocks 리스트 (text_stream 1개 + place 0~1개).
    """
    blocks: list[dict[str, Any]] = []

    if place_row is None:
        # 매칭 실패 fallback
        blocks.append(
            {
                "type": "text_stream",
                "system": _DETAIL_INQUIRY_SYSTEM_PROMPT,
                "prompt": _FALLBACK_MESSAGE,
            }
        )
        return blocks

    # 매칭 성공 — Gemini 요약 프롬프트 구성
    name = place_row.get("name", "")
    category = place_row.get("category", "")
    address = place_row.get("address", "")
    district = place_row.get("district", "")

    info_lines = [
        f"장소명: {name}",
        f"카테고리: {category}" if category else "",
        f"주소: {address}" if address else "",
        f"자치구: {district}" if district else "",
    ]
    info_text = "\n".join(line for line in info_lines if line)

    prompt = f"사용자 질문: {query}\n\n장소 정보:\n{info_text}\n\n위 장소의 상세 정보를 친절하게 소개해주세요."

    blocks.append(
        {
            "type": "text_stream",
            "system": _DETAIL_INQUIRY_SYSTEM_PROMPT,
            "prompt": prompt,
        }
    )

    # place 블록 — PlaceBlock 필드만 사용 (phone 제외, place_id 필수)
    place_block: dict[str, Any] = {
        "type": "place",
        "place_id": place_row.get("place_id", ""),
        "name": name,
    }
    if category:
        place_block["category"] = category
    if address:
        place_block["address"] = address
    if district:
        place_block["district"] = district
    if place_row.get("lat") is not None:
        place_block["lat"] = place_row["lat"]
    if place_row.get("lng") is not None:
        place_block["lng"] = place_row["lng"]

    from src.models.blocks import attach_map_urls  # pyright: ignore[reportMissingImports]

    attach_map_urls(place_block)
    blocks.append(place_block)

    return blocks


# ---------------------------------------------------------------------------
# PostgreSQL 단건 조회
# ---------------------------------------------------------------------------
async def _fetch_place(
    pool: Any,
    search_term: str,
) -> Optional[dict[str, Any]]:
    """places 테이블에서 name ILIKE 단건 조회.

    불변식 #4: is_deleted=false.
    불변식 #8: asyncpg $1 파라미터 바인딩.
    """
    try:
        safe_term = _escape_like(search_term)
        row = await pool.fetchrow(_DETAIL_SQL, f"%{safe_term}%")
        if row is None:
            return None
        return dict(row)
    except Exception:
        logger.exception("detail_inquiry PG fetch failed")
        return None


# ---------------------------------------------------------------------------
# LangGraph 노드
# ---------------------------------------------------------------------------
async def detail_inquiry_node(state: dict[str, Any]) -> dict[str, Any]:
    """DETAIL_INQUIRY 노드 — 장소 상세 단건 조회 + Gemini 소개.

    Args:
        state: AgentState dict.

    Returns:
        {"response_blocks": [text_stream, place]}.
    """
    from src.db.postgres import get_pool  # pyright: ignore[reportMissingImports]

    query = state.get("query", "")
    pq = state.get("processed_query") or {}

    search_term = _extract_search_term(pq, query)

    if not search_term.strip():
        blocks = _build_detail_blocks(query, None)
        return {"response_blocks": blocks}

    pool = get_pool()
    place_row = await _fetch_place(pool, search_term)

    blocks = _build_detail_blocks(query, place_row)

    if place_row is not None:
        logger.info("detail_inquiry: found place=%s", place_row.get("name", "")[:50])
    else:
        logger.info("detail_inquiry: no match for search_term=%s", search_term[:50])

    return {"response_blocks": blocks}
