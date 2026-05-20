"""PLACE_RECOMMEND 노드 — SQL + Vector k-NN + LLM Rerank 3단계 (Phase 1).

검색 흐름:
  1. PG 정형 필터 (district/category + is_deleted=false)
  2. OS places_vector k-NN (expanded_query 768d 의미 검색)
  3. OS place_reviews k-NN (비정형 조건 리뷰 매칭)
  4. 병합 + PG 2차 보강 (place_id 중복 제거)
  5. LLM Rerank (Gemini Flash — 조건 적합도 순위 재배치)
  6. 블록: text_stream + places[] + map_markers + references

기획 변경: 기능 명세서 v2 "Google Places 병렬" → OS place_reviews k-NN 대체 (PM 승인).
ST_DWithin 공간 필터는 AgentState에 user_location 추가 후 도입 예정.

불변식 #2: place_id == places_vector._id
불변식 #7: gemini-embedding-001 768d
불변식 #8: asyncpg $1,$2 바인딩
불변식 #11: intent → text_stream → places[] → map_markers → references → done
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_RECOMMEND_SYSTEM_PROMPT = (
    "당신은 서울 로컬 라이프 AI 챗봇 'AnyWay'입니다. "
    "자기소개나 인사로 시작하지 말고 바로 본론으로 답변하세요. "
    "각 장소는 이미 카드로 소개되었습니다. "
    "전체 추천 결과를 종합하여 2-3문장으로 간결하게 요약해주세요.\n\n"
    "## 응답 형식 규칙\n"
    "- 주제가 바뀔 때 빈 줄로 단락을 구분하세요.\n"
    "- 핵심 정보는 **굵게** 강조하세요."
)

_MAX_RESULTS = 5
_OS_TOP_K = 10
_OS_MIN_SCORE = 0.4
_PG_LIMIT = 10


# ---------------------------------------------------------------------------
# Gemini 768d 임베딩 (place_search_node 동일 로직 복제 — Simplicity First)
# ---------------------------------------------------------------------------
async def _embed_query_768d(query: str, api_key: str) -> list[float]:
    """Gemini embedding-001 768d 단건 임베딩. 불변식 #7."""
    import httpx

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"
    body = {
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": query[:2000]}]},
        "outputDimensionality": 768,
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    return data.get("embedding", {}).get("values", [0.0] * 768)


# ---------------------------------------------------------------------------
# ① PG 정형 검색
# ---------------------------------------------------------------------------
async def _search_pg(
    pool: Any,
    district: Optional[str],
    category: Optional[str],
    keywords: list[str],
    neighborhood: Optional[str],
) -> list[dict[str, Any]]:
    """places 테이블 조건부 필터. is_deleted=false 필수. 불변식 #8."""
    sql = (
        "SELECT place_id, name, category, address, district, "
        "ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng "
        "FROM places WHERE is_deleted = false"
    )
    params: list[Any] = []

    if district:
        params.append(district)
        sql += f" AND district = ${len(params)}"

    if category:
        params.append(f"%{category}%")
        sql += f" AND category ILIKE ${len(params)}"

    if neighborhood:
        # 동/지역명으로 주소 검색만 (name ILIKE 제거 — 노이즈 방지)
        params.append(f"%{neighborhood}%")
        sql += f" AND address ILIKE ${len(params)}"

    if keywords:
        params.append(f"%{keywords[0]}%")
        sql += f" AND name ILIKE ${len(params)}"

    params.append(_PG_LIMIT)
    sql += f" LIMIT ${len(params)}"

    try:
        rows = await pool.fetch(sql, *params)
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("PG place recommend search failed")
        return []


# ---------------------------------------------------------------------------
# ② OS places_vector k-NN
# ---------------------------------------------------------------------------
async def _search_os_places(
    os_client: Any,
    query: str,
    api_key: str,
    district: Optional[str] = None,
) -> list[dict[str, Any]]:
    """places_vector k-NN HNSW. district 있으면 필터 적용."""
    try:
        query_vector = await _embed_query_768d(query, api_key)

        body: dict[str, Any] = {
            "size": _OS_TOP_K,
            "query": {
                "knn": {
                    "embedding": {"vector": query_vector, "k": _OS_TOP_K},
                }
            },
            "min_score": _OS_MIN_SCORE,
        }

        result = await os_client.search(index="places_vector", body=body)
        hits = result.get("hits", {}).get("hits", [])

        places: list[dict[str, Any]] = []
        for hit in hits:
            source = hit.get("_source", {})
            places.append(
                {
                    "place_id": hit.get("_id", ""),
                    "name": source.get("name", ""),
                    "category": source.get("category", ""),
                    "address": source.get("address", ""),
                    "district": source.get("district", ""),
                    "lat": source.get("lat"),
                    "lng": source.get("lng"),
                    "score": hit.get("_score", 0),
                }
            )
        if district:
            filtered = [p for p in places if p.get("district") == district]
            return filtered if filtered else places
        return places

    except Exception:
        logger.exception("OS places_vector search failed")
        return []


# ---------------------------------------------------------------------------
# ③ OS place_reviews k-NN (비정형 조건 매칭)
# ---------------------------------------------------------------------------
async def _search_os_reviews(
    os_client: Any,
    condition_text: str,
    api_key: str,
) -> list[dict[str, Any]]:
    """place_reviews k-NN. 비정형 조건 임베딩 → 리뷰 유사도 top-K.

    Returns:
        [{"place_id", "place_name", "keywords", "summary_text", "score"}]
    """
    try:
        query_vector = await _embed_query_768d(condition_text, api_key)

        body: dict[str, Any] = {
            "size": _OS_TOP_K,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": query_vector,
                        "k": _OS_TOP_K,
                    }
                }
            },
            "min_score": _OS_MIN_SCORE,
            "_source": ["place_id", "place_name", "keywords", "summary_text"],
        }

        result = await os_client.search(index="place_reviews", body=body)
        hits = result.get("hits", {}).get("hits", [])

        reviews: list[dict[str, Any]] = []
        for hit in hits:
            source = hit.get("_source", {})
            reviews.append(
                {
                    "place_id": source.get("place_id", ""),
                    "place_name": source.get("place_name", ""),
                    "keywords": source.get("keywords", []),
                    "summary_text": source.get("summary_text", ""),
                    "score": hit.get("_score", 0),
                }
            )
        return reviews

    except Exception:
        logger.exception("OS place_reviews search failed")
        return []


# ---------------------------------------------------------------------------
# ④ 병합 + PG 2차 보강
# ---------------------------------------------------------------------------
async def _merge_candidates(
    pool: Any,
    pg_results: list[dict[str, Any]],
    os_place_results: list[dict[str, Any]],
    os_review_results: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """3채널 결과 병합. place_id 중복 제거 + PG 2차 보강.

    Returns:
        (merged_places, review_data_map)
        review_data_map: {place_id: {"keywords", "summary_text"}}
    """
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    need_pg_lookup: list[str] = []

    # 리뷰 데이터 맵 (references 블록용) — 첫 번째 hit(최고 유사도)만 유지
    review_data_map: dict[str, dict[str, Any]] = {}
    for r in os_review_results:
        pid = r.get("place_id", "")
        if pid and pid not in review_data_map:
            review_data_map[pid] = {
                "keywords": r.get("keywords", []),
                "summary_text": r.get("summary_text", ""),
            }

    # OS places_vector 결과 우선 (의미 유사도 순)
    for place in os_place_results:
        pid = place.get("place_id", "")
        if pid and pid not in seen:
            seen.add(pid)
            merged.append(place)

    # OS place_reviews 결과 추가 (상세 정보 없으면 PG 조회 필요)
    for r in os_review_results:
        pid = r.get("place_id", "")
        if pid and pid not in seen:
            seen.add(pid)
            need_pg_lookup.append(pid)

    # PG 2차 조회: OS review에서만 나온 place_id의 상세 정보 보강
    # review-only 후보를 PG 결과보다 앞에 배치 (리뷰 유사도 신호 보존)
    if need_pg_lookup:
        try:
            rows = await pool.fetch(
                "SELECT place_id, name, category, address, district, "
                "ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng "
                "FROM places WHERE place_id = ANY($1::varchar[]) AND is_deleted = false",
                need_pg_lookup,
            )
            # need_pg_lookup 순서 보존 (유사도 내림차순)
            lookup_map = {row["place_id"]: dict(row) for row in rows}
            for pid in need_pg_lookup:
                if pid in lookup_map:
                    merged.append(lookup_map[pid])
        except Exception:
            logger.exception("PG 2차 보강 조회 실패")

    # PG 결과 추가 (review-only 후보 뒤에 배치)
    for place in pg_results:
        pid = place.get("place_id", "")
        if pid and pid not in seen:
            seen.add(pid)
            merged.append(place)

    return merged, review_data_map


# ---------------------------------------------------------------------------
# ⑤ LLM Rerank (Gemini Flash)
# ---------------------------------------------------------------------------
_RERANK_SYSTEM_PROMPT = """\
사용자의 조건에 가장 적합한 장소 순서를 매겨주세요.
후보 장소 목록과 사용자 조건이 주어집니다.

JSON으로만 응답하세요:
{
  "ranked_ids": ["가장 적합한 place_id", "두 번째", ...],
  "reasons": {
    "place_id_1": "추천 이유 한 줄",
    "place_id_2": "추천 이유 한 줄"
  }
}

ranked_ids에는 상위 5개만 포함하세요.
reasons에는 각 장소가 사용자 조건에 왜 적합한지 구체적으로 작성하세요.
"""


async def _llm_rerank(
    candidates: list[dict[str, Any]],
    query: str,
    keywords: list[str],
    review_data_map: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Gemini Flash로 조건 적합도 순위 재배치.

    Returns:
        (reranked_top5, reasons_map)
        graceful degradation: Gemini 실패 시 원본 순서 상위 5건.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI

    from src.config import get_settings

    settings = get_settings()
    if not settings.gemini_llm_api_key or not candidates:
        return candidates[:_MAX_RESULTS], {}

    # 후보 메타 구성
    candidate_lines: list[str] = []
    for c in candidates:
        pid = c.get("place_id", "")
        line = (
            f"- id={pid}, name={c.get('name', '')}, category={c.get('category', '')}, district={c.get('district', '')}"
        )
        review = review_data_map.get(pid)
        if review:
            kw = ", ".join(review.get("keywords", [])[:5])
            line += f", review_keywords=[{kw}]"
        candidate_lines.append(line)

    user_prompt = f"사용자 조건: {query}\n키워드: {', '.join(keywords)}\n\n후보 장소:\n" + "\n".join(candidate_lines)

    try:
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=settings.gemini_llm_api_key,
            temperature=0,
        )

        response = await llm.ainvoke(
            [
                ("system", _RERANK_SYSTEM_PROMPT),
                ("human", user_prompt),
            ]
        )
        text = str(response.content).strip()

        # Gemini ```json ... ``` 래핑 처리
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)
        ranked_ids: list[str] = result.get("ranked_ids", [])
        reasons: dict[str, str] = result.get("reasons", {})

        # ranked_ids 순서로 candidates 재배치
        id_to_candidate = {c.get("place_id", ""): c for c in candidates}
        reranked: list[dict[str, Any]] = []
        for pid in ranked_ids[:_MAX_RESULTS]:
            if pid in id_to_candidate:
                reranked.append(id_to_candidate[pid])

        # ranked_ids에 없는 후보도 원본 순서로 채움
        if len(reranked) < _MAX_RESULTS:
            for c in candidates:
                if c not in reranked and len(reranked) < _MAX_RESULTS:
                    reranked.append(c)

        return reranked, reasons

    except Exception:
        logger.exception("LLM rerank failed → fallback to original order")
        return candidates[:_MAX_RESULTS], {}


# ---------------------------------------------------------------------------
# ⑥ 블록 생성
# ---------------------------------------------------------------------------
def _build_blocks(
    query: str,
    results: list[dict[str, Any]],
    reasons: dict[str, str],
    review_data_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """places(+summary) + text_stream(종합 요약) + map_markers + references 블록 생성.

    불변식 #11: places → text_stream → map_markers → references → done
    """
    blocks: list[dict[str, Any]] = []

    # 1. places 블록 (카드 먼저 전송, reasons를 summary로 사용)
    place_items: list[dict[str, Any]] = []
    for r in results:
        pid = r.get("place_id", "")
        item: dict[str, Any] = {
            "type": "place",
            "place_id": pid,
            "name": r.get("name", ""),
        }
        if r.get("category"):
            item["category"] = r["category"]
        if r.get("address"):
            item["address"] = r["address"]
        if r.get("district"):
            item["district"] = r["district"]
        if r.get("lat") is not None:
            item["lat"] = r["lat"]
        if r.get("lng") is not None:
            item["lng"] = r["lng"]
        if r.get("congestion"):
            item["congestion"] = r["congestion"]
        # LLM rerank reason → summary 필드 (추가 Gemini 호출 불필요)
        reason = reasons.get(pid)
        if reason:
            item["summary"] = reason
        from src.models.blocks import attach_map_urls  # pyright: ignore[reportMissingImports]

        attach_map_urls(item)
        place_items.append(item)

    # places 블록은 항상 반환 (빈 배열 포함 — 블록 순서 검증 일관성)
    blocks.append(
        {
            "type": "places",
            "items": place_items,
            "total_count": len(place_items),
        }
    )

    # 2. text_stream: 종합 요약 (카드 뒤에 스트리밍)
    if results:
        result_lines: list[str] = []
        for r in results:
            pid = r.get("place_id", "")
            line = f"- {r.get('name', '')} ({r.get('category', '')}, {r.get('district', '')})"
            review = review_data_map.get(pid)
            if review:
                kws = review.get("keywords", [])
                if kws:
                    line += f" [리뷰 키워드: {', '.join(kws[:5])}]"
            result_lines.append(line)

        prompt = (
            f"사용자 질문: {query}\n\n추천 결과:\n" + "\n".join(result_lines) + "\n\n위 추천 결과를 종합 요약해주세요."
        )
    else:
        prompt = f"사용자 질문: {query}\n\n추천할 장소를 찾지 못했습니다. 다른 조건으로 다시 검색해보시겠어요?"

    blocks.append(
        {
            "type": "text_stream",
            "system": _RECOMMEND_SYSTEM_PROMPT,
            "prompt": prompt,
        }
    )

    # 3. map_markers 블록
    markers: list[dict[str, Any]] = []
    for r in results:
        if r.get("lat") is not None and r.get("lng") is not None:
            markers.append(
                {
                    "place_id": r.get("place_id", ""),
                    "lat": r["lat"],
                    "lng": r["lng"],
                    "label": r.get("name", ""),
                }
            )

    if markers:
        blocks.append(
            {
                "type": "map_markers",
                "markers": markers,
            }
        )

    # 4. references 블록 (리뷰 데이터 있는 장소만)
    ref_items: list[dict[str, Any]] = []
    for r in results:
        pid = r.get("place_id", "")
        review = review_data_map.get(pid)
        if review and review.get("summary_text"):
            ref_items.append(
                {
                    "source_type": "review",
                    "source_id": pid,
                    "snippet": review["summary_text"][:200],
                }
            )

    if ref_items:
        blocks.append(
            {
                "type": "references",
                "items": ref_items,
            }
        )

    return blocks


# ---------------------------------------------------------------------------
# LangGraph 노드
# ---------------------------------------------------------------------------
async def _handle_refinement(
    state: dict[str, Any],
    previous_blocks: list[dict[str, Any]],
    refinement: dict[str, Any],
) -> dict[str, Any]:
    """PLACE_RECOMMEND refinement 처리 — 이전 결과 기반 수정."""
    from src.graph.refine_helpers import REFINE_SYSTEM_PROMPT  # pyright: ignore[reportMissingImports]

    if state.get("_refine_depth", 0) > 1:
        clean = dict(state)
        clean["previous_blocks"] = None
        clean["refinement"] = None
        return await place_recommend_node(clean)

    from src.config import get_settings  # pyright: ignore[reportMissingImports]
    from src.db.opensearch import get_os_client  # pyright: ignore[reportMissingImports]
    from src.db.postgres import get_pool  # pyright: ignore[reportMissingImports]
    from src.graph.refine_helpers import (  # pyright: ignore[reportMissingImports]
        apply_add,
        apply_remove,
        apply_replace,
        extract_excluded_ids,
        extract_items_from_blocks,
        get_refinement_search_query,
    )

    action = refinement.get("action", "regenerate")
    target_index = refinement.get("target_index")
    query = state.get("query", "")
    pq = state.get("processed_query") or {}

    if action in ("regenerate", "change_condition"):
        if action == "change_condition":
            new_q = refinement.get("new_condition", query)
            state = dict(state)
            state["query"] = new_q
            state["previous_blocks"] = None
            state["refinement"] = None
        return await place_recommend_node(state)

    prev_items = extract_items_from_blocks(previous_blocks, "places")
    if not prev_items:
        return await place_recommend_node(state)

    settings = get_settings()

    if action == "remove" and target_index is not None:
        items = apply_remove(prev_items, target_index)
    elif action in ("replace", "add"):
        search_query = get_refinement_search_query(refinement, pq, query)
        district = pq.get("district")
        category = pq.get("category")
        keywords = pq.get("keywords", [])
        neighborhood = pq.get("neighborhood")

        pool = get_pool()
        pg_results = await _search_pg(pool, district, category, keywords, neighborhood)

        os_results: list[dict[str, Any]] = []
        if settings.gemini_llm_api_key:
            try:
                os_client = get_os_client()
                os_results = await _search_os_places(os_client, search_query, settings.gemini_llm_api_key, district)
            except RuntimeError:
                pass

        merged = _merge_candidates(pg_results, os_results)
        excluded = extract_excluded_ids(prev_items, "place_id", target_index if action == "replace" else None)
        new_candidates = [r for r in merged if r.get("place_id", "") not in excluded]

        no_candidate_found = not new_candidates
        if no_candidate_found:
            items = prev_items
        elif action == "replace" and target_index is not None:
            new_item = new_candidates[0]
            from src.models.blocks import attach_map_urls  # pyright: ignore[reportMissingImports]

            attach_map_urls(new_item)
            items = apply_replace(prev_items, target_index, new_item)
        else:
            new_item = new_candidates[0]
            from src.models.blocks import attach_map_urls  # pyright: ignore[reportMissingImports]

            attach_map_urls(new_item)
            items = apply_add(prev_items, new_item)
    else:
        items = prev_items
        no_candidate_found = False

    blocks: list[dict[str, Any]] = []
    if items:
        for item in items:
            from src.models.blocks import attach_map_urls  # pyright: ignore[reportMissingImports]

            attach_map_urls(item)
        blocks.append({"type": "places", "items": items, "total_count": len(items)})

    result_summary = "\n".join(
        f"- {r.get('name', '')} ({r.get('category', '')}, {r.get('district', '')})" for r in items
    )
    if no_candidate_found:
        prompt = f"사용자 요청: {query}\n\n조건에 맞는 대체 장소를 찾지 못해 기존 결과를 유지합니다. 다른 조건으로 다시 요청해보라고 1-2문장으로 안내해주세요."
    elif action == "replace" and target_index is not None:
        old_name = prev_items[target_index - 1].get("name", "") if 0 < target_index <= len(prev_items) else ""
        new_name = items[target_index - 1].get("name", "") if 0 < target_index <= len(items) else ""
        prompt = f"사용자 요청: {query}\n\n{target_index}번 장소를 **{old_name}**에서 **{new_name}**(으)로 변경했습니다.\n수정된 목록:\n{result_summary}\n\n변경 사항을 포함해 1-2문장으로 요약해주세요."
    elif action == "remove":
        prompt = f"사용자 요청: {query}\n\n요청하신 장소를 목록에서 제거했습니다.\n수정된 목록:\n{result_summary}\n\n1-2문장으로 요약해주세요."
    elif action == "add":
        added = items[-1].get("name", "") if items else ""
        prompt = f"사용자 요청: {query}\n\n**{added}**을(를) 목록에 추가했습니다.\n수정된 목록:\n{result_summary}\n\n1-2문장으로 요약해주세요."
    else:
        prompt = f"사용자 요청: {query}\n\n수정된 추천 결과:\n{result_summary}\n\n위 결과를 종합 요약해주세요."
    blocks.append({"type": "text_stream", "system": REFINE_SYSTEM_PROMPT, "prompt": prompt})

    markers = [
        {"place_id": r.get("place_id", ""), "lat": r["lat"], "lng": r["lng"], "label": r.get("name", "")}
        for r in items
        if r.get("lat") is not None and r.get("lng") is not None
    ]
    if markers:
        blocks.append({"type": "map_markers", "markers": markers})

    return {"response_blocks": blocks}


async def place_recommend_node(state: dict[str, Any]) -> dict[str, Any]:
    """PLACE_RECOMMEND 노드 — PG + OS 2채널 + LLM Rerank (Phase 1).

    Args:
        state: AgentState dict.

    Returns:
        {"response_blocks": [text_stream, places, map_markers, references]}.
    """
    previous_blocks = state.get("previous_blocks")
    refinement = state.get("refinement")
    if previous_blocks and refinement:
        return await _handle_refinement(state, previous_blocks, refinement)

    from src.config import get_settings
    from src.db.opensearch import get_os_client
    from src.db.postgres import get_pool

    query = state.get("query", "")
    pq = state.get("processed_query") or {}

    district = pq.get("district")
    category = pq.get("category")
    keywords: list[str] = pq.get("keywords", [])
    neighborhood = pq.get("neighborhood")
    expanded_query = pq.get("expanded_query") or query

    settings = get_settings()
    pool = get_pool()

    # 비정형 조건 텍스트 (keywords 조합 → place_reviews 검색용)
    condition_text = " ".join(keywords) if keywords else query

    # ①②③ 병렬 검색
    pg_task = _search_pg(pool, district, category, keywords, neighborhood)

    os_places_task: Optional[asyncio.Task[list[dict[str, Any]]]] = None
    os_reviews_task: Optional[asyncio.Task[list[dict[str, Any]]]] = None

    if settings.gemini_llm_api_key:
        try:
            os_client = get_os_client()
            os_places_task = asyncio.create_task(
                _search_os_places(os_client, expanded_query, settings.gemini_llm_api_key, district)
            )
            os_reviews_task = asyncio.create_task(
                _search_os_reviews(os_client, condition_text, settings.gemini_llm_api_key)
            )
        except RuntimeError:
            logger.warning("OpenSearch client not initialized, skipping vector search")

    pg_results = await pg_task

    os_place_results: list[dict[str, Any]] = []
    if os_places_task is not None:
        os_place_results = await os_places_task

    os_review_results: list[dict[str, Any]] = []
    if os_reviews_task is not None:
        os_review_results = await os_reviews_task

    # ④ 병합
    candidates, review_data_map = await _merge_candidates(pool, pg_results, os_place_results, os_review_results)

    # ⑤ LLM Rerank
    reranked, reasons = await _llm_rerank(candidates, query, keywords, review_data_map)

    # congestion 주입 (district 기준 area_proxy, 실패 시 무시)
    try:
        from src.graph.crowdedness_node import fetch_congestion_by_district  # pyright: ignore[reportMissingImports]

        districts = list({r["district"] for r in reranked if r.get("district")})
        if districts:
            congs = await asyncio.gather(
                *(fetch_congestion_by_district(pool, d) for d in districts),
                return_exceptions=True,
            )
            cong_map = {d: c for d, c in zip(districts, congs, strict=True) if isinstance(c, dict)}
            for r in reranked:
                d = r.get("district")
                if d and d in cong_map:
                    r["congestion"] = cong_map[d]
    except Exception:
        logger.warning("congestion fetch skipped")

    # ⑥ 블록 생성
    blocks = _build_blocks(query, reranked, reasons, review_data_map)

    logger.info(
        "place_recommend: pg=%d, os_places=%d, os_reviews=%d, merged=%d, final=%d",
        len(pg_results),
        len(os_place_results),
        len(os_review_results),
        len(candidates),
        len(reranked),
    )

    return {"response_blocks": blocks}
