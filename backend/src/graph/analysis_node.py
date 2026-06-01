"""ANALYSIS 노드 — 단일 장소 6지표 분석 (Phase 1).

기획서 §4.5 블록 순서: intent → status → text_stream → analysis_sources → done
불변식 #4: is_deleted=false 필터
불변식 #6: 6지표 키 (satisfaction/accessibility/cleanliness/value/atmosphere/expertise)
불변식 #8: asyncpg $1 바인딩
불변식 #18: Phase 1
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.graph._tracing import traced_node  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

# 불변식 #6: 6지표 키 고정 (이름·개수 변경 금지)
_VALID_SCORE_KEYS: frozenset[str] = frozenset(
    {"satisfaction", "accessibility", "cleanliness", "value", "atmosphere", "expertise"}
)

_ANALYSIS_SYSTEM_PROMPT = """\
당신은 서울 로컬 라이프 AI 챗봇 'AnyWay'입니다.
자기소개나 인사로 시작하지 말고 바로 본론으로 답변하세요.
장소의 6지표(만족도/접근성/청결도/가성비/분위기/전문성) 데이터를 바탕으로
해당 장소를 심층 분석하여 친절하게 설명해주세요.
각 지표의 의미와 강점/약점을 구체적으로 설명하고, 어떤 상황에 적합한 장소인지 추천 포인트를 제시하세요.
데이터가 없는 경우 솔직하게 안내하세요.
"""

_NO_SCORES_SYSTEM_PROMPT = """\
당신은 서울 로컬 라이프 AI 챗봇 'AnyWay'입니다.
자기소개나 인사로 시작하지 말고 바로 본론으로 답변하세요.
사용자가 요청한 장소의 리뷰 데이터가 아직 충분하지 않습니다.
장소의 기본 정보(이름, 카테고리, 지역)를 바탕으로 간단히 소개하고,
아직 리뷰 분석 데이터가 쌓이지 않았음을 안내해주세요.
"""


def _extract_place_name(
    processed_query: dict[str, Any],
    query: str,
) -> Optional[str]:
    """processed_query에서 단일 장소명 추출."""
    place_name = processed_query.get("place_name")
    if place_name and isinstance(place_name, str) and place_name.strip():
        return place_name.strip()

    keywords: list[str] = processed_query.get("keywords") or []
    if isinstance(keywords, list) and len(keywords) >= 1:
        first = keywords[0]
        if isinstance(first, str) and first.strip():
            return first.strip()

    return None


async def _fetch_place_pg(
    pool: Any,
    place_name: str,
    os_client: Any,
) -> Optional[dict[str, Any]]:
    """장소명 → PG places 조회. 동명 다중 매칭 시 OS stars 최댓값 채택."""
    from src.utils.resilience import retry_call  # pyright: ignore[reportMissingImports]

    rows = await pool.fetch(
        "SELECT place_id, name, category, district FROM places WHERE is_deleted = false AND name ILIKE $1 LIMIT 20",
        f"%{place_name}%",
    )
    if not rows:
        return None

    if len(rows) == 1:
        return dict(rows[0])

    # OS 미가용 시 PG 첫 번째 row 사용
    if os_client is None:
        return dict(rows[0])

    # 동명 다중 매칭 → OS stars 최댓값 1건
    place_ids = [row["place_id"] for row in rows]
    doc_ids = [f"review_{pid}" for pid in place_ids]
    try:
        mget_resp = await retry_call(
            lambda: os_client.mget(
                body={"ids": doc_ids},
                index="place_reviews",
                _source=["stars", "place_id"],
            ),
            attempts=3,
        )
        stars_map: dict[str, float] = {}
        for hit in mget_resp.get("docs", []):
            if hit.get("found"):
                src = hit.get("_source", {})
                pid = src.get("place_id", "")
                stars_map[pid] = float(src.get("stars", 0.0))
        best = max(rows, key=lambda r: stars_map.get(r["place_id"], 0.0))
        return dict(best)
    except Exception:
        logger.exception("_fetch_place_pg: OS mget 실패 → PG 첫 번째 row 사용")
        return dict(rows[0])


async def _fetch_scores_os(
    os_client: Any,
    place_id: str,
) -> tuple[dict[str, float], int]:
    """place_id → OS place_reviews._raw_scores + review_count 조회.

    Returns:
        (scores_dict, review_count). 문서 없으면 ({}, 0).
    """
    from src.utils.resilience import retry_call  # pyright: ignore[reportMissingImports]

    doc_id = f"review_{place_id}"
    try:
        mget_resp = await retry_call(
            lambda: os_client.mget(
                body={"ids": [doc_id]},
                index="place_reviews",
                _source=["_raw_scores", "review_count", "place_id"],
            ),
            attempts=3,
        )
        for hit in mget_resp.get("docs", []):
            if hit.get("found"):
                src = hit.get("_source", {})
                raw = src.get("_raw_scores", {})
                raw_count = src.get("review_count", 0)
                review_count = int(raw_count) if raw_count is not None else 0
                scores = {k: float(v) for k, v in raw.items() if k in _VALID_SCORE_KEYS and isinstance(v, (int, float))}
                return (scores, review_count)
        return ({}, 0)
    except Exception:
        logger.exception("_fetch_scores_os: OS mget 실패")
        return ({}, 0)


def _build_analysis_blocks(
    query: str,
    place: dict[str, Any],
    scores: dict[str, float],
    review_count: int,
) -> list[dict[str, Any]]:
    """text_stream + analysis_sources raw dict 블록 생성."""
    if scores:
        score_str = ", ".join(f"{k}: {v:.1f}" for k, v in scores.items())
        system = _ANALYSIS_SYSTEM_PROMPT
        prompt = (
            f"사용자 질문: {query}\n\n"
            f"장소: {place['name']} ({place.get('category', '')})\n"
            f"지역: {place.get('district', '')}\n"
            f"6지표: {score_str}\n"
            f"리뷰 수: {review_count}"
        )
    else:
        system = _NO_SCORES_SYSTEM_PROMPT
        prompt = (
            f"사용자 질문: {query}\n\n"
            f"장소: {place['name']} ({place.get('category', '')})\n"
            f"지역: {place.get('district', '')}\n"
            f"리뷰 데이터: 없음"
        )

    return [
        {
            "type": "text_stream",
            "system": system,
            "prompt": prompt,
        },
        {
            "type": "analysis_sources",
            "review_count": review_count,
        },
    ]


@traced_node("analysis")
async def analysis_node(state: dict[str, Any]) -> dict[str, Any]:
    """LangGraph 노드 — ANALYSIS intent 처리 (Phase 1)."""
    query: str = state.get("query", "")
    processed_query: dict[str, Any] = state.get("processed_query") or {}

    place_name = _extract_place_name(processed_query, query)

    if not place_name:
        return {
            "response_blocks": [
                {
                    "type": "disambiguation",
                    "message": "어떤 장소를 분석할까요? 장소명을 알려주세요.",
                    "candidates": [],
                }
            ]
        }

    from src.db.postgres import get_pool  # pyright: ignore[reportMissingImports]

    pool = get_pool()

    # OS 클라이언트 — 미가용 시 PG만으로 동작 (점수 빈 값)
    os_client: Any = None
    try:
        from src.db.opensearch import get_os_client  # pyright: ignore[reportMissingImports]

        os_client = get_os_client()
    except Exception:
        logger.warning("analysis_node: OS 클라이언트 초기화 실패 → PG만으로 진행")

    place = await _fetch_place_pg(pool, place_name, os_client)

    if not place:
        return {
            "response_blocks": [
                {
                    "type": "disambiguation",
                    "message": "장소를 찾을 수 없어요. 정확한 장소명으로 다시 입력해주세요.",
                    "candidates": [],
                }
            ]
        }

    if os_client:
        scores, review_count = await _fetch_scores_os(os_client, place["place_id"])
    else:
        scores, review_count = {}, 0
    blocks = _build_analysis_blocks(query, place, scores, review_count)

    return {"response_blocks": blocks}
