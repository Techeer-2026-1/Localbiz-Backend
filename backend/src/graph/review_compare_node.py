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

logger = logging.getLogger(__name__)

_COMPARE_SYSTEM_PROMPT = """\
당신은 서울 로컬 라이프 AI 챗봇 'AnyWay'입니다.
자기소개나 인사로 시작하지 말고 바로 본론으로 답변하세요.
두 장소의 6지표(만족도/접근성/청결도/가성비/분위기/전문성)를 비교 분석하여 친절하게 설명해주세요.
데이터를 기반으로 각 장소의 강점과 약점을 명확하게 설명하고 상황에 맞는 추천을 제공하세요.
"""


def _extract_place_names(processed_query: dict[str, Any], query: str) -> list[str]:
    """vs/VS/와 구분자로 장소명 추출. 2개 미만이면 [] 반환."""
    for sep in (" vs ", " VS ", " 와 "):
        if sep in query:
            parts = [p.strip() for p in query.split(sep)]
            parts = [re.sub(r"\s*(비교|compare).*$", "", p, flags=re.IGNORECASE).strip() for p in parts]
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
            mget_resp = await os_client.mget(
                body={"ids": doc_ids},
                index="place_reviews",
                _source=["stars", "place_id"],
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


async def _fetch_scores_os(
    os_client: Any,
    place_ids: list[str],
) -> dict[str, dict[str, float]]:
    """place_ids → OS place_reviews._raw_scores mget 1회 조회."""
    if not place_ids:
        return {}
    doc_ids = [f"review_{pid}" for pid in place_ids]
    try:
        mget_resp = await os_client.mget(
            body={"ids": doc_ids},
            index="place_reviews",
            _source=["_raw_scores", "place_id"],
        )
        scores_map: dict[str, dict[str, float]] = {}
        for hit in mget_resp.get("docs", []):
            if hit.get("found"):
                src = hit.get("_source", {})
                pid = src.get("place_id", "")
                raw = src.get("_raw_scores", {})
                if pid and isinstance(raw, dict):
                    scores_map[pid] = {k: float(v) for k, v in raw.items() if isinstance(v, (int, float))}
        return scores_map
    except Exception:
        logger.exception("_fetch_scores_os: OS mget 실패")
        return {}


def _build_compare_blocks(
    query: str,
    places: list[dict[str, Any]],
    scores_map: dict[str, dict[str, float]],
) -> list[dict[str, Any]]:
    """text_stream + chart + analysis_sources raw dict 블록 생성."""
    compare_lines = []
    for p in places:
        pid = p["place_id"]
        scores = scores_map.get(pid, {})
        score_str = ", ".join(f"{k}: {v:.1f}" for k, v in scores.items()) if scores else "데이터 없음"
        compare_lines.append(f"- {p['name']} ({p.get('category', '')}): {score_str}")

    compare_text = "\n".join(compare_lines)

    return [
        {
            "type": "text_stream",
            "system": _COMPARE_SYSTEM_PROMPT,
            "prompt": f"사용자 질문: {query}\n\n장소 비교:\n{compare_text}",
        },
        {
            "type": "chart",
            "chart_type": "radar",
            "places": [{"name": p["name"], "scores": scores_map.get(p["place_id"], {})} for p in places],
        },
        {
            "type": "analysis_sources",
            "review_count": len([p for p in places if scores_map.get(p["place_id"])]),
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

    scores_map = await _fetch_scores_os(os_client, [p["place_id"] for p in places])
    blocks = _build_compare_blocks(query, places, scores_map)

    return {"response_blocks": blocks}
