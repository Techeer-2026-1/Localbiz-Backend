"""PLACE_SEARCH 노드 — SQL + Vector k-NN 하이브리드 검색.

검색 흐름:
  1. processed_query에서 district/category/keywords 추출
  2. PostgreSQL: places WHERE district/category/name ILIKE + is_deleted=false
  3. OpenSearch: places_vector k-NN HNSW top-10 (min_score 0.5)
  4. 병합: PG + OS → place_id 중복 제거 → 상위 5건
  5. response_blocks: text_stream(요약) + places[] + map_markers

불변식 #2: place_id == places_vector._id
불변식 #7: gemini-embedding-001 768d
불변식 #8: asyncpg $1,$2 바인딩
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_PLACE_SEARCH_SYSTEM_PROMPT = (
    "당신은 서울 로컬 라이프 AI 챗봇 'AnyWay'입니다. "
    "자기소개나 인사로 시작하지 말고 바로 본론으로 답변하세요. "
    "각 장소는 이미 카드로 소개되었습니다. "
    "전체 검색 결과를 종합하여 2-3문장으로 간결하게 요약해주세요. "
    "없으면 '검색 결과가 없습니다'라고 안내하세요.\n\n"
    "## 응답 형식 규칙\n"
    "- 주제가 바뀔 때 빈 줄로 단락을 구분하세요.\n"
    "- 핵심 정보는 **굵게** 강조하세요."
)

_DESC_SYSTEM_PROMPT = (
    "주어진 장소 목록의 각 장소에 대해 1-2문장의 간결한 소개를 작성해주세요.\n"
    "JSON 배열로만 응답하세요:\n"
    '[{"place_id": "...", "description": "1-2문장 소개"}, ...]'
)

_MAX_RESULTS = 5
_OS_TOP_K = 10
_OS_MIN_SCORE = 0.5
_PG_LIMIT = 10


# ---------------------------------------------------------------------------
# PostgreSQL 검색
# ---------------------------------------------------------------------------
async def _search_pg(
    pool: Any,
    district: Optional[str],
    category: Optional[str],
    keywords: list[str],
    neighborhood: Optional[str],
) -> list[dict[str, Any]]:
    """places 테이블에서 조건부 필터 검색. is_deleted=false 필수.

    필터 전략:
      - district: 자치구 정확 매칭 ("강남구")
      - category: 카테고리 ILIKE ("%카페%")
      - neighborhood: 동/지역명으로 address 또는 name 검색 ("%홍대%")
      - keywords: 첫 번째 키워드로 name 검색 ("%분위기%")
    """
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
        # 동/지역명으로 주소 검색만 (name ILIKE 제거 — "홍대쌀국수(영등포)" 등 노이즈 방지)
        params.append(f"%{neighborhood}%")
        sql += f" AND address ILIKE ${len(params)}"

    if keywords:
        # 키워드로 name 검색 (첫 번째만, 보조 필터)
        params.append(f"%{keywords[0]}%")
        sql += f" AND name ILIKE ${len(params)}"

    params.append(_PG_LIMIT)
    sql += f" LIMIT ${len(params)}"

    try:
        rows = await pool.fetch(sql, *params)
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("PG place search failed")
        return []


# ---------------------------------------------------------------------------
# OpenSearch k-NN 검색
# ---------------------------------------------------------------------------
async def _embed_query_768d(query: str, api_key: str) -> list[float]:
    """Gemini embedding-001 768d 단건 임베딩 (async). 불변식 #7."""

    import httpx  # pyright: ignore[reportMissingImports]

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


async def _search_os(
    os_client: Any,
    query: str,
    api_key: str,
    district: Optional[str] = None,
) -> list[dict[str, Any]]:
    """places_vector k-NN HNSW 검색. district 있으면 필터 적용."""
    try:
        query_vector = await _embed_query_768d(query, api_key)

        knn_params: dict[str, Any] = {
            "vector": query_vector,
            "k": _OS_TOP_K,
        }
        if district:
            knn_params["filter"] = {"term": {"district": district}}

        body: dict[str, Any] = {
            "size": _OS_TOP_K,
            "query": {
                "knn": {
                    "embedding": knn_params,
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
        return places

    except Exception:
        logger.exception("OS place search failed")
        return []


# ---------------------------------------------------------------------------
# 결과 병합
# ---------------------------------------------------------------------------
def _merge_results(
    pg_results: list[dict[str, Any]],
    os_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """PG + OS 결과 병합. place_id 중복 제거, 상위 N건."""
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []

    # OS 결과 우선 (유사도 순)
    for place in os_results:
        pid = place.get("place_id", "")
        if pid and pid not in seen:
            seen.add(pid)
            merged.append(place)

    # PG 결과 추가
    for place in pg_results:
        pid = place.get("place_id", "")
        if pid and pid not in seen:
            seen.add(pid)
            merged.append(place)

    return merged[:_MAX_RESULTS]


# ---------------------------------------------------------------------------
# per-place description 생성 (Gemini JSON mode)
# ---------------------------------------------------------------------------
async def _generate_place_descriptions(
    results: list[dict[str, Any]],
    query: str,
    api_key: str,
) -> dict[str, str]:
    """Gemini로 per-place 설명 생성. 실패 시 빈 dict (graceful degradation)."""
    import json

    if not results or not api_key:
        return {}

    from langchain_google_genai import ChatGoogleGenerativeAI

    items_text = "\n".join(
        f"- place_id={r.get('place_id', '')}, name={r.get('name', '')}, "
        f"category={r.get('category', '')}, district={r.get('district', '')}"
        for r in results
    )
    prompt = f"사용자 질문: {query}\n\n장소 목록:\n{items_text}"

    try:
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=api_key,
            temperature=0.3,
            timeout=10,
        )
        response = await llm.ainvoke(
            [
                ("system", _DESC_SYSTEM_PROMPT),
                ("human", prompt),
            ]
        )
        text = str(response.content).strip()

        # Gemini ```json ... ``` 래핑 처리
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        items_data = json.loads(text)
        return {item["place_id"]: item["description"] for item in items_data if "place_id" in item}
    except Exception:
        logger.exception("per-place description generation failed → fallback")
        return {}


# ---------------------------------------------------------------------------
# 블록 생성
# ---------------------------------------------------------------------------
def _build_blocks(
    query: str,
    results: list[dict[str, Any]],
    descriptions: dict[str, str],
) -> list[dict[str, Any]]:
    """검색 결과 → places(+summary) + text_stream(종합 요약) + map_markers 블록."""
    blocks: list[dict[str, Any]] = []

    # 1. places 블록 (카드 먼저 전송)
    place_items: list[dict[str, Any]] = []
    for r in results:
        item: dict[str, Any] = {
            "type": "place",
            "place_id": r.get("place_id", ""),
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
        # per-place description → summary 필드
        desc = descriptions.get(r.get("place_id", ""))
        if desc:
            item["summary"] = desc
        from src.models.blocks import attach_map_urls  # pyright: ignore[reportMissingImports]

        attach_map_urls(item)
        place_items.append(item)

    if place_items:
        blocks.append(
            {
                "type": "places",
                "items": place_items,
                "total_count": len(place_items),
            }
        )

    # 2. text_stream: 종합 요약 (카드 뒤에 스트리밍)
    if results:
        result_summary = "\n".join(
            f"- {r.get('name', '')} ({r.get('category', '')}, {r.get('district', '')})" for r in results
        )
        prompt = f"사용자 질문: {query}\n\n검색 결과:\n{result_summary}\n\n위 결과를 종합 요약해주세요."
    else:
        prompt = f"사용자 질문: {query}\n\n검색 결과가 없습니다. 다른 검색어를 제안해주세요."

    blocks.append(
        {
            "type": "text_stream",
            "system": _PLACE_SEARCH_SYSTEM_PROMPT,
            "prompt": prompt,
        }
    )

    # 3. map_markers 블록 (좌표 있는 결과만)
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

    return blocks


# ---------------------------------------------------------------------------
# LangGraph 노드
# ---------------------------------------------------------------------------
async def place_search_node(state: dict[str, Any]) -> dict[str, Any]:
    """PLACE_SEARCH 노드 — PG + OS 하이브리드 검색.

    Args:
        state: AgentState dict.

    Returns:
        {"response_blocks": [text_stream, places, map_markers]}.
    """
    from src.config import get_settings  # pyright: ignore[reportMissingImports]
    from src.db.opensearch import get_os_client  # pyright: ignore[reportMissingImports]
    from src.db.postgres import get_pool  # pyright: ignore[reportMissingImports]

    query = state.get("query", "")
    pq = state.get("processed_query") or {}

    district = pq.get("district")
    category = pq.get("category")
    keywords = pq.get("keywords", [])
    neighborhood = pq.get("neighborhood")
    expanded_query = pq.get("expanded_query")

    settings = get_settings()

    import asyncio

    # PG + OS 병렬 검색
    pool = get_pool()
    pg_task = _search_pg(pool, district, category, keywords, neighborhood)

    os_task: Optional[asyncio.Task[list[dict[str, Any]]]] = None
    if settings.gemini_llm_api_key:
        try:
            os_client = get_os_client()
            search_text = expanded_query or query
            os_task = asyncio.create_task(_search_os(os_client, search_text, settings.gemini_llm_api_key, district))
        except RuntimeError:
            logger.warning("OpenSearch client not initialized, skipping vector search")

    pg_results = await pg_task

    os_results: list[dict[str, Any]] = []
    if os_task is not None:
        os_results = await os_task

    # 병합
    results = _merge_results(pg_results, os_results)

    # congestion 주입 (district 기준 area_proxy, 실패 시 무시)
    try:
        from src.graph.crowdedness_node import fetch_congestion_by_district  # pyright: ignore[reportMissingImports]

        districts = list({r["district"] for r in results if r.get("district")})
        if districts:
            congs = await asyncio.gather(
                *(fetch_congestion_by_district(pool, d) for d in districts),
                return_exceptions=True,
            )
            cong_map = {d: c for d, c in zip(districts, congs, strict=True) if isinstance(c, dict)}
            for r in results:
                d = r.get("district")
                if d and d in cong_map:
                    r["congestion"] = cong_map[d]
    except Exception:
        logger.warning("congestion fetch skipped")
    # per-place description 생성
    descriptions: dict[str, str] = {}
    if results and settings.gemini_llm_api_key:
        descriptions = await _generate_place_descriptions(results, query, settings.gemini_llm_api_key)

    # 블록 생성
    blocks = _build_blocks(query, results, descriptions)

    logger.info("place_search: pg=%d, os=%d, merged=%d", len(pg_results), len(os_results), len(results))

    return {"response_blocks": blocks}
