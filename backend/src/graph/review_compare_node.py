"""REVIEW_COMPARE 노드 — 2개 이상 장소 6지표 레이더 차트 비교 (Phase 1).

기획서 §4.5 블록 순서: intent → status → text_stream → chart → analysis_sources → done
불변식 #4: is_deleted=false 필터
불변식 #6: 6지표 키 그대로 (satisfaction/accessibility/cleanliness/value/atmosphere/expertise)
불변식 #8: asyncpg $1 바인딩
불변식 #18: Phase 1 (기획서 v2 SSE L155)
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.graph._tracing import traced_node  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

_COMPARE_SYSTEM_PROMPT = """\
당신은 서울 로컬 라이프 AI 챗봇 'AnyWay'입니다.
자기소개나 인사로 시작하지 말고 바로 본론으로 답변하세요.
두 장소의 6지표(만족도/접근성/청결도/가성비/분위기/전문성)를 비교 분석하여 친절하게 설명해주세요.
데이터를 기반으로 각 장소의 강점과 약점을 명확하게 설명하고 상황에 맞는 추천을 제공하세요.
"""


def _extract_place_names(processed_query: dict[str, Any], query: str) -> list[str]:
    """vs/VS/와 구분자로 장소명 추출. 2개 미만이면 [] 반환."""
    # 한국어 비교 구분자 — 부착형('점이랑')까지 포함. 첫 매칭이 2개 이상 분할되면 채택.
    # REVIEW_COMPARE intent로 이미 분류된 쿼리이므로 과분할 위험은 낮다.
    for sep in (" vs ", " VS ", "이랑", " 와 ", " 과 ", "하고", " 그리고 ", " 랑 "):
        if sep in query:
            parts = [p.strip() for p in query.split(sep)]
            # 꼬리말(리뷰/비교/compare …) 제거 — '명동점 리뷰 비교해줘' → '명동점'
            parts = [re.sub(r"\s*(리뷰|비교|compare).*$", "", p, flags=re.IGNORECASE).strip() for p in parts]
            parts = [p for p in parts if p]
            if len(parts) >= 2:
                return parts

    keywords: list[str] = processed_query.get("keywords") or []
    if isinstance(keywords, list) and len(keywords) >= 2:
        return list(keywords)

    return []


async def _fetch_places_pg(
    pool: Any,
    names: list[str],
    os_client: Any,
) -> list[dict[str, Any]]:
    """장소명 목록 → PG places 조회. 동명 다중 매칭 시 OS stars 최댓값 채택."""
    from src.utils.resilience import retry_call  # pyright: ignore[reportMissingImports]

    results: list[dict[str, Any]] = []
    for name in names:
        rows = await pool.fetch(
            "SELECT place_id, name, category, district FROM places WHERE is_deleted = false AND name ILIKE $1",
            f"%{name}%",
        )
        if not rows:
            continue

        if len(rows) == 1:
            results.append(dict(rows[0]))
            continue

        # 동명 다중 매칭 → OS stars 최댓값 1건 선택 (mget 1회)
        place_ids = [row["place_id"] for row in rows]
        doc_ids = [f"review_{pid}" for pid in place_ids]
        try:
            mget_resp = await retry_call(
                lambda ids=doc_ids: os_client.mget(
                    body={"ids": ids},
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
            results.append(dict(best))
        except Exception:
            logger.exception("_fetch_places_pg: OS mget 실패 → PG 첫 번째 row 사용")
            results.append(dict(rows[0]))
    return results


# 6지표 키 고정 (analysis_node._VALID_SCORE_KEYS와 동일 — DRY 위해 별도 정의보다 import도 가능하지만
# 본 노드 단독에서 chart axis fill에만 사용되므로 분리. analysis_node와 일치 유지 필수 — 불변식 #6).
_VALID_SCORE_KEYS: frozenset[str] = frozenset(
    {"satisfaction", "accessibility", "cleanliness", "value", "atmosphere", "expertise"}
)

# 비교 키워드 self-check — intent_router 오분류 backstop.
# "비교" 동사·명사 / "어느"·"어디가 더" 선택 의문 / "리뷰"(스코어 비교 전제) 셋 중 하나는 있어야 비교 의도로 인정.
_COMPARE_KEYWORDS: frozenset[str] = frozenset({"비교", "어느", "어디가 더", "리뷰"})


def _fill_score_keys(scores: dict[str, float]) -> dict[str, float]:
    """6 지표 키 중 누락된 것은 0.0으로 fill. 차트 axis 누락 방지 — 불변식 #6.

    원본 점수가 빈 dict이면 6 키 모두 0.0 — FE가 'no data' 차트를 명시적으로 그릴 수 있음.
    """
    return {k: float(scores.get(k, 0.0)) for k in _VALID_SCORE_KEYS}


async def _fetch_scores_os(
    os_client: Any,
    place_ids: list[str],
) -> tuple[dict[str, dict[str, float]], dict[str, int]]:
    """place_ids → OS place_reviews._raw_scores + review_count mget 1회 조회.

    Returns:
        (scores_map, review_count_map). 둘 다 place_id → 값 dict. 실패 시 빈 dict 짝.
        review_count_map은 analysis_sources.review_count 합산용 (analysis_node 패턴).
    """
    from src.utils.resilience import retry_call  # pyright: ignore[reportMissingImports]

    if not place_ids:
        return {}, {}
    doc_ids = [f"review_{pid}" for pid in place_ids]
    try:
        mget_resp = await retry_call(
            lambda: os_client.mget(
                body={"ids": doc_ids},
                index="place_reviews",
                _source=["_raw_scores", "review_count", "place_id"],
            ),
            attempts=3,
        )
        scores_map: dict[str, dict[str, float]] = {}
        review_count_map: dict[str, int] = {}
        for hit in mget_resp.get("docs", []):
            if hit.get("found"):
                src = hit.get("_source", {})
                pid = src.get("place_id", "")
                if not pid:
                    continue
                raw = src.get("_raw_scores", {})
                if isinstance(raw, dict):
                    scores_map[pid] = {k: float(v) for k, v in raw.items() if isinstance(v, (int, float))}
                rc = src.get("review_count", 0)
                review_count_map[pid] = int(rc) if rc is not None else 0
        return scores_map, review_count_map
    except Exception:
        logger.exception("_fetch_scores_os: OS mget 실패")
        return {}, {}


def _build_compare_blocks(
    query: str,
    places: list[dict[str, Any]],
    scores_map: dict[str, dict[str, float]],
    review_count_map: dict[str, int],
) -> list[dict[str, Any]]:
    """text_stream + chart + analysis_sources raw dict 블록 생성.

    text_stream prompt에는 raw 점수를 포함하지 않는다 — 수치는 chart 블록 단독 책임.
    Gemini가 점수를 prose로 풀어 출력하면 차트가 무의미해지고 화면이 줄글로 덮임.
    """
    place_names = ", ".join(p["name"] for p in places)
    place_lines = "\n".join(f"- {p['name']} ({p.get('category', '')})" for p in places)

    return [
        {
            "type": "text_stream",
            "system": _COMPARE_SYSTEM_PROMPT,
            "prompt": (
                f"사용자 질문: {query}\n\n"
                f"비교 장소:\n{place_lines}\n\n"
                "(6지표 점수는 레이더 차트로 별도 시각화됩니다. "
                f"{place_names} 각 장소의 강점·약점·추천 상황을 한국어 1-2 문장으로만 요약하세요. "
                "구체적인 점수 수치는 절대 언급하지 마세요.)"
            ),
        },
        {
            "type": "chart",
            "chart_type": "radar",
            "places": [
                {"name": p["name"], "scores": _fill_score_keys(scores_map.get(p["place_id"], {}))} for p in places
            ],
        },
        {
            "type": "analysis_sources",
            # 비교 장소 전체의 리뷰 합산 — analysis_node 패턴.
            "review_count": sum(review_count_map.get(p["place_id"], 0) for p in places),
        },
    ]


async def _handle_refinement(
    state: dict[str, Any],
    previous_blocks: list[dict[str, Any]],
    refinement: dict[str, Any],
) -> dict[str, Any]:
    """REVIEW_COMPARE refinement — 비교 대상 변경 시 전체 재실행."""
    if refinement.get("action") == "change_condition" and refinement.get("new_condition"):
        state = dict(state)
        state["query"] = refinement["new_condition"]
    state = dict(state)
    state["previous_blocks"] = None
    state["refinement"] = None
    return await review_compare_node(state)


@traced_node("review_compare")
async def review_compare_node(state: dict[str, Any]) -> dict[str, Any]:
    """LangGraph 노드 — REVIEW_COMPARE intent 처리 (Phase 1)."""
    previous_blocks = state.get("previous_blocks")
    refinement = state.get("refinement")
    if previous_blocks and refinement:
        return await _handle_refinement(state, previous_blocks, refinement)

    query: str = state.get("query", "")
    processed_query: dict[str, Any] = state.get("processed_query") or {}

    place_names = _extract_place_names(processed_query, query)

    if len(place_names) < 2:
        return {
            "response_blocks": [
                {
                    "type": "disambiguation",
                    "message": "어느 장소와 비교하시겠어요?",
                    "candidates": [],
                }
            ]
        }

    # 비교 키워드 self-check — intent_router 오분류 backstop.
    # 2 장소가 추출돼도 사용자가 "비교/리뷰/어디가 더" 같은 비교 의도를 명시하지 않으면 안내로 빠진다.
    # 예) "A랑 B 어디냐?" 는 위치 질문(DETAIL_INQUIRY 영역)이지 비교가 아님.
    if not any(kw in query for kw in _COMPARE_KEYWORDS):
        logger.info(
            "review_compare_node: 비교 키워드 없음 query_len=%d places=%d → disambiguation",
            len(query),
            len(place_names),
        )
        return {
            "response_blocks": [
                {
                    "type": "disambiguation",
                    "message": (
                        "두 장소를 비교하시려는 거라면 '리뷰 비교해줘'라고 말씀해 주세요. "
                        "위치나 정보가 궁금하시면 한 장소씩 물어봐 주세요."
                    ),
                    "candidates": [],
                }
            ]
        }

    from src.db.opensearch import get_os_client  # pyright: ignore[reportMissingImports]
    from src.db.postgres import get_pool  # pyright: ignore[reportMissingImports]

    pool = get_pool()
    os_client = get_os_client()

    places = await _fetch_places_pg(pool, place_names, os_client)

    if len(places) == 0:
        return {
            "response_blocks": [
                {
                    "type": "disambiguation",
                    "message": "장소를 찾을 수 없어요. 정확한 장소명으로 다시 입력해주세요.",
                    "candidates": [],
                }
            ]
        }

    if len(places) == 1:
        found = places[0]["name"]
        not_found = next((n for n in place_names if n not in found), place_names[-1])
        return {
            "response_blocks": [
                {
                    "type": "text_stream",
                    "system": _COMPARE_SYSTEM_PROMPT,
                    "prompt": f"'{not_found}'은(는) 찾을 수 없었어요. 대신 '{found}'을(를) 소개해드릴게요.\n\n사용자 질문: {query}",
                }
            ]
        }

    scores_map, review_count_map = await _fetch_scores_os(os_client, [p["place_id"] for p in places])
    blocks = _build_compare_blocks(query, places, scores_map, review_count_map)

    return {"response_blocks": blocks}
