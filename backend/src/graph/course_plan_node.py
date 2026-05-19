"""COURSE_PLAN 노드 — 카테고리별 병렬 검색 → Greedy NN → LLM 코스 구성 (Phase 1).

검색 흐름:
  1. 쿼리에서 복수 카테고리 파싱 ("카페+맛집" → ["카페", "맛집"])
  2. 카테고리별 PG + OS 병렬 검색 (place_recommend 패턴 재활용)
  3. Greedy Nearest Neighbor 경로 최적화 (Haversine 직선 거리)
  4. LLM 코스 구성 (Gemini Flash — 시간대 배분 + 추천 사유)
  5. 블록: text_stream + course + map_route

Phase 1 단순화: OSRM 미사용, polyline.type="straight". Phase 2에서 road polyline 교체.
ST_DWithin 공간 필터는 AgentState에 user_location 추가 후 도입 예정.

불변식 #2: place_id == places_vector._id
불변식 #7: gemini-embedding-001 768d
불변식 #8: asyncpg $1,$2 바인딩
불변식 #11: intent → text_stream → course → map_route → done
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

_COURSE_SYSTEM_PROMPT = (
    "당신은 서울 로컬 라이프 AI 챗봇 'AnyWay'입니다. "
    "자기소개나 인사로 시작하지 말고 바로 본론으로 답변하세요.\n\n"
    "## 절대 규칙\n"
    "- 코스의 각 장소는 카드로 이미 소개되었습니다.\n"
    "- 개별 장소를 하나씩 설명하지 마세요. 번호 매기기 금지.\n"
    "- 코스 전체의 테마와 매력을 2-3문장으로만 요약하세요.\n"
    "- 핵심 키워드는 **굵게** 강조하세요."
)

_MAX_STOPS = 5
_DEFAULT_DURATION_MIN = 60
_OS_TOP_K = 10
_OS_MIN_SCORE = 0.4
_PG_LIMIT = 10


# ---------------------------------------------------------------------------
# Gemini 768d 임베딩 (place_search/place_recommend 동일 로직 복제)
# TODO: Phase 1 이후 공유 유틸 추출 검토
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

    values = data.get("embedding", {}).get("values")
    if not values:
        logger.warning("_embed_query_768d: API 응답에 embedding.values 없음 → zero vector fallback")
        return [0.0] * 768
    return values


# ---------------------------------------------------------------------------
# ① 카테고리 파싱
# ---------------------------------------------------------------------------
def _parse_categories(query: str, pq_category: Optional[str]) -> list[str]:
    """쿼리에서 복수 카테고리 추출. "카페+맛집" → ["카페", "맛집"]."""
    categories: list[str] = []

    _KNOWN_CATEGORIES = ["카페", "맛집", "음식점", "술집", "주점", "관광지", "공원", "쇼핑", "문화시설"]

    # query에서 +, &, 와/과 구분자로 분리 — 첫 매칭 구분자만 사용
    for sep in ["+", "&", "와 ", "과 ", ", "]:
        if sep in query:
            parts = [p.strip() for p in query.split(sep)]
            for part in parts:
                for keyword in _KNOWN_CATEGORIES:
                    if keyword in part and keyword not in categories:
                        categories.append(keyword)
            break  # 첫 매칭 구분자로만 파싱

    # 구분자 없는 단일 카테고리 쿼리 ("홍대 카페 코스") — whole-query 키워드 스캔
    if not categories:
        for keyword in _KNOWN_CATEGORIES:
            if keyword in query and keyword not in categories:
                categories.append(keyword)

    if not categories and pq_category:
        categories = [pq_category]

    if not categories:
        categories = ["맛집"]

    return categories[:3]  # 최대 3 카테고리


# ---------------------------------------------------------------------------
# ② 카테고리별 PG + OS 병렬 검색
# ---------------------------------------------------------------------------
async def _search_pg(
    pool: Any,
    district: Optional[str],
    category: str,
    neighborhood: Optional[str],
) -> list[dict[str, Any]]:
    """places 테이블 카테고리별 검색. 불변식 #8."""
    sql = (
        "SELECT place_id, name, category, address, district, "
        "ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng "
        "FROM places WHERE is_deleted = false"
    )
    params: list[Any] = []

    params.append(f"%{category}%")
    sql += f" AND category ILIKE ${len(params)}"

    if district:
        params.append(district)
        sql += f" AND district = ${len(params)}"

    if neighborhood:
        # 동/지역명으로 주소 검색만 (name ILIKE 제거 — 노이즈 방지)
        params.append(f"%{neighborhood}%")
        sql += f" AND address ILIKE ${len(params)}"

    params.append(_PG_LIMIT)
    sql += f" LIMIT ${len(params)}"

    try:
        rows = await pool.fetch(sql, *params)
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("PG course search failed for category=%s", category)
        return []


async def _search_os(
    os_client: Any,
    query: str,
    api_key: str,
    district: Optional[str] = None,
) -> list[dict[str, Any]]:
    """places_vector k-NN 검색. NMSLIB 엔진은 filter 미지원 → post-filter."""
    try:
        query_vector = await _embed_query_768d(query, api_key)

        body: dict[str, Any] = {
            "size": _OS_TOP_K,
            "query": {"knn": {"embedding": {"vector": query_vector, "k": _OS_TOP_K}}},
            "min_score": _OS_MIN_SCORE,
        }

        result = await os_client.search(index="places_vector", body=body)
        hits = result.get("hits", {}).get("hits", [])

        places = [
            {
                "place_id": hit.get("_id", ""),
                "name": hit.get("_source", {}).get("name", ""),
                "category": hit.get("_source", {}).get("category", ""),
                "address": hit.get("_source", {}).get("address", ""),
                "district": hit.get("_source", {}).get("district", ""),
                "lat": hit.get("_source", {}).get("lat"),
                "lng": hit.get("_source", {}).get("lng"),
                "score": hit.get("_score", 0),
            }
            for hit in hits
        ]
        if district:
            filtered = [p for p in places if p.get("district") == district]
            return filtered if filtered else places
        return places
    except Exception:
        logger.exception("OS course search failed")
        return []


async def _search_by_categories(
    pool: Any,
    os_client: Optional[Any],
    categories: list[str],
    district: Optional[str],
    neighborhood: Optional[str],
    expanded_query: str,
    api_key: Optional[str],
) -> list[dict[str, Any]]:
    """카테고리별 PG + OS 병렬 검색 → 병합."""
    tasks: list[Any] = []

    for cat in categories:
        tasks.append(_search_pg(pool, district, cat, neighborhood))
        if os_client and api_key:
            tasks.append(_search_os(os_client, f"{expanded_query} {cat}", api_key, district))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for result in results:
        if isinstance(result, BaseException):
            logger.warning("Category search error: %s", result)
            continue
        for place in result:
            pid = place.get("place_id", "")
            if pid and pid not in seen:
                seen.add(pid)
                merged.append(place)

    return merged


# ---------------------------------------------------------------------------
# ②-b LLM Rerank (코스 후보 적합도 순위)
# ---------------------------------------------------------------------------
_COURSE_RERANK_PROMPT = """\
사용자의 코스 요청에 가장 적합한 장소를 골라 순위를 매겨주세요.

## 판단 기준
- 사용자가 구체적 장소명을 지정하지 않은 경우, 해당 카테고리에서 **일반 소비자가 기대하는 대표적·트렌디한 장소**를 우선하세요.
- **편의점(GS25, CU, 이마트24, 세븐일레븐, 미니스톱)**, 마트, 학교 매점, 관공서, 프랜차이즈 건강식품 매장(정관장 등)은 **반드시 제외**하세요.
- 사용자가 특정 장소나 브랜드를 명시한 경우에는 그것을 우선하세요.

JSON으로만 응답하세요:
{"ranked_ids": ["가장 적합한 place_id", "두 번째", ...]}

상위 10개만 포함하세요. 편의점·마트는 ranked_ids에 포함하지 마세요.
"""

# 코스 추천에서 제외할 장소명 패턴 (편의점·마트·매점)
_COURSE_EXCLUDE_NAMES = [
    "GS25",
    "CU",
    "이마트24",
    "세븐일레븐",
    "미니스톱",
    "정관장",
    "매점",
    "마트",
    "관공서",
]


async def _llm_rerank_candidates(
    candidates: list[dict[str, Any]],
    query: str,
    categories: list[str],
) -> list[dict[str, Any]]:
    """Gemini로 코스 후보 적합도 rerank. 실패 시 원본 순서."""
    from langchain_google_genai import ChatGoogleGenerativeAI

    from src.config import get_settings

    settings = get_settings()
    if not settings.gemini_llm_api_key or len(candidates) <= _MAX_STOPS:
        return candidates

    candidate_lines = "\n".join(
        f"- id={c.get('place_id', '')}, name={c.get('name', '')}, "
        f"category={c.get('category', '')}, district={c.get('district', '')}"
        for c in candidates
    )
    user_prompt = f"사용자 요청: {query}\n카테고리: {', '.join(categories)}\n\n후보 장소:\n{candidate_lines}"

    try:
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=settings.gemini_llm_api_key,
            temperature=0,
        )
        response = await llm.ainvoke([("system", _COURSE_RERANK_PROMPT), ("human", user_prompt)])
        text = str(response.content).strip()

        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)
        ranked_ids: list[str] = result.get("ranked_ids", [])

        id_to_candidate = {c.get("place_id", ""): c for c in candidates}
        reranked: list[dict[str, Any]] = []
        for pid in ranked_ids:
            if pid in id_to_candidate:
                reranked.append(id_to_candidate[pid])

        # ranked_ids에 없는 후보도 원본 순서로 채움
        seen = {c.get("place_id", "") for c in reranked}
        for c in candidates:
            if c.get("place_id", "") not in seen:
                reranked.append(c)

        logger.info("course rerank: %d candidates → top=%s", len(candidates), ranked_ids[:3])
        return reranked

    except Exception:
        logger.exception("course LLM rerank failed → fallback to original order")
        return candidates


# ---------------------------------------------------------------------------
# ③ Greedy Nearest Neighbor 경로 최적화
# ---------------------------------------------------------------------------
def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """두 좌표 간 Haversine 거리 (미터)."""
    r = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _greedy_nn_route(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Greedy Nearest Neighbor — 직선 거리 기반 최근접 이웃 순서 결정.

    좌표 없는 후보는 뒤에 배치. 최대 _MAX_STOPS건.
    """
    with_coords = [c for c in candidates if c.get("lat") is not None and c.get("lng") is not None]
    without_coords = [c for c in candidates if c.get("lat") is None or c.get("lng") is None]

    if not with_coords:
        return candidates[:_MAX_STOPS]

    route: list[dict[str, Any]] = [with_coords[0]]
    remaining = with_coords[1:]

    while remaining and len(route) < _MAX_STOPS:
        last = route[-1]
        nearest_idx = 0
        nearest_dist = float("inf")
        for i, c in enumerate(remaining):
            dist = _haversine_m(last["lat"], last["lng"], c["lat"], c["lng"])
            if dist < nearest_dist:
                nearest_dist = dist
                nearest_idx = i
        route.append(remaining.pop(nearest_idx))

    # 좌표 없는 후보로 남은 슬롯 채우기
    for c in without_coords:
        if len(route) >= _MAX_STOPS:
            break
        route.append(c)

    return route


# ---------------------------------------------------------------------------
# ④ LLM 코스 구성
# ---------------------------------------------------------------------------
_COURSE_COMPOSE_PROMPT = """\
사용자의 코스 추천 요청에 맞춰 코스를 구성해주세요.
장소 목록과 순서가 주어집니다. 각 장소에 대해:

JSON으로만 응답하세요:
{
  "title": "코스 제목 (예: '홍대 카페+맛집 한나절 코스')",
  "description": "코스 한줄 설명",
  "stops": [
    {
      "order": 1,
      "arrival_time": "HH:mm (예: 11:00)",
      "duration_min": 체류시간(분),
      "summary": "장소 한줄 소개 (15자 이내, 예: '감성 빈티지 카페')",
      "recommendation_reason": "추천 이유 한 줄",
      "transit_mode": "walk|subway|bus|taxi (다음 장소까지 이동 수단)"
    }
  ]
}

시간대 배분 규칙:
- 시작 시간은 11:00 기본
- 각 장소 체류 30-90분
- 이동 시간은 도보 기준 예상
- 마지막 장소의 transit_mode는 null
"""


async def _llm_course_compose(
    route: list[dict[str, Any]],
    query: str,
) -> tuple[Optional[str], Optional[str], list[dict[str, Any]]]:
    """Gemini Flash로 코스 구성. 실패 시 균등 배분 fallback.

    Returns:
        (title, description, stop_details[{arrival_time, duration_min, recommendation_reason, transit_mode}])
    """
    from langchain_google_genai import ChatGoogleGenerativeAI

    from src.config import get_settings

    settings = get_settings()
    if not settings.gemini_llm_api_key or not route:
        return None, None, []

    place_lines = "\n".join(
        f"- {i + 1}. {p.get('name', '')} ({p.get('category', '')}, {p.get('district', '')})"
        for i, p in enumerate(route)
    )

    try:
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=settings.gemini_llm_api_key,
            temperature=0,
        )

        response = await llm.ainvoke(
            [
                ("system", _COURSE_COMPOSE_PROMPT),
                ("human", f"사용자 요청: {query}\n\n경유 장소 (순서대로):\n{place_lines}"),
            ]
        )
        text = str(response.content).strip()

        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)
        if not isinstance(result, dict):
            logger.warning("LLM course compose: 응답이 dict 아님 → fallback")
            return None, None, []

        title = result.get("title")
        description = result.get("description")
        raw_stops = result.get("stops", [])

        # stops 요소 검증 — dict가 아니거나 order 없으면 제외
        stop_details: list[dict[str, Any]] = []
        for s in raw_stops:
            if not isinstance(s, dict):
                continue
            # duration_min 타입 정규화
            dur = s.get("duration_min")
            if isinstance(dur, str) and dur.isdigit():
                s["duration_min"] = int(dur)
            stop_details.append(s)

        return title, description, stop_details

    except Exception:
        logger.exception("LLM course compose failed → fallback")
        return None, None, []


def _apply_fallback_times(route: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """LLM 실패 시 균등 배분 fallback."""
    details: list[dict[str, Any]] = []
    hour = 11
    minute = 0

    for i in range(len(route)):
        details.append(
            {
                "order": i + 1,
                "arrival_time": f"{hour:02d}:{minute:02d}",
                "duration_min": _DEFAULT_DURATION_MIN,
                "recommendation_reason": None,
                "transit_mode": "walk" if i < len(route) - 1 else None,
            }
        )
        minute += _DEFAULT_DURATION_MIN + 15  # 체류 + 이동 15분
        while minute >= 60:
            hour += 1
            minute -= 60

    return details


# ---------------------------------------------------------------------------
# ⑤ 블록 생성
# ---------------------------------------------------------------------------
def _build_blocks(
    query: str,
    route: list[dict[str, Any]],
    title: Optional[str],
    description: Optional[str],
    stop_details: list[dict[str, Any]],
    course_id: str,
) -> list[dict[str, Any]]:
    """course + text_stream(종합 요약) + map_route 블록 생성."""
    blocks: list[dict[str, Any]] = []

    # fallback 적용
    if not stop_details:
        stop_details = _apply_fallback_times(route)

    if not title:
        title = "추천 코스"
    if not description:
        description = f"{len(route)}곳을 둘러보는 코스"

    # --- course 블록 (카드 먼저 전송) ---
    total_distance_m = 0
    total_stay_min = 0
    total_transit_min = 0

    # order 기준 dict lookup (LLM이 순서 바꿔 반환할 수 있음)
    detail_by_order: dict[int, dict[str, Any]] = {}
    for d in stop_details:
        order = d.get("order")
        if isinstance(order, int):
            detail_by_order[order] = d
    # order 키 없으면 index fallback
    if not detail_by_order:
        detail_by_order = {i + 1: d for i, d in enumerate(stop_details)}

    course_stops: list[dict[str, Any]] = []
    for i, place in enumerate(route):
        detail = detail_by_order.get(i + 1, {})

        duration = detail.get("duration_min", _DEFAULT_DURATION_MIN)
        total_stay_min += duration

        transit_to_next: Optional[dict[str, Any]] = None
        if i < len(route) - 1:
            next_place = route[i + 1]
            if place.get("lat") is not None and next_place.get("lat") is not None:
                dist = int(_haversine_m(place["lat"], place["lng"], next_place["lat"], next_place["lng"]))
                transit_min = max(1, dist // 80)  # 도보 약 80m/min
            else:
                dist = 0
                transit_min = 10

            total_distance_m += dist
            total_transit_min += transit_min

            mode = detail.get("transit_mode", "walk") or "walk"
            mode_ko_map = {"walk": "도보", "subway": "지하철", "bus": "버스", "taxi": "택시"}
            transit_to_next = {
                "mode": mode,
                "mode_ko": mode_ko_map.get(mode, "도보"),
                "distance_m": dist,
                "duration_min": transit_min,
            }

        stop: dict[str, Any] = {
            "order": i + 1,
            "arrival_time": detail.get("arrival_time"),
            "duration_min": duration,
            "place": {
                "place_id": place.get("place_id", ""),
                "name": place.get("name", ""),
                "category": place.get("category"),
                "category_label": place.get("category"),
                "address": place.get("address"),
                "district": place.get("district"),
                "location": {"lat": place["lat"], "lng": place["lng"]}
                if place.get("lat") is not None and place.get("lng") is not None
                else None,
                "summary": detail.get("summary"),
            },
            "transit_to_next": transit_to_next,
            "recommendation_reason": detail.get("recommendation_reason"),
        }
        from src.models.blocks import attach_map_urls  # pyright: ignore[reportMissingImports]

        attach_map_urls(stop["place"])
        course_stops.append(stop)

    blocks.append(
        {
            "type": "course",
            "course_id": course_id,
            "title": title,
            "description": description,
            "total_distance_m": total_distance_m,
            "total_duration_min": total_stay_min + total_transit_min,
            "total_stay_min": total_stay_min,
            "total_transit_min": total_transit_min,
            "stops": course_stops,
        }
    )

    # --- text_stream (종합 요약, 카드 뒤에 스트리밍) ---
    prompt = (
        f"사용자 질문: {query}\n\n"
        f"코스 제목: {title}\n"
        f"코스 설명: {description}\n"
        f"경유 장소 수: {len(route)}곳\n\n"
        "위 코스의 전체 테마와 매력을 2-3문장으로 요약해주세요."
    )
    blocks.append({"type": "text_stream", "system": _COURSE_SYSTEM_PROMPT, "prompt": prompt})

    # --- map_route 블록 ---
    markers: list[dict[str, Any]] = []
    for i, place in enumerate(route):
        if place.get("lat") is not None and place.get("lng") is not None:
            markers.append(
                {
                    "order": i + 1,
                    "position": {"lat": place["lat"], "lng": place["lng"]},
                    "label": place.get("name", ""),
                    "category": place.get("category"),
                }
            )

    segments: list[dict[str, Any]] = []
    for i in range(len(route) - 1):
        p1 = route[i]
        p2 = route[i + 1]
        if p1.get("lat") is not None and p2.get("lat") is not None:
            detail = stop_details[i] if i < len(stop_details) else {}
            segments.append(
                {
                    "from_order": i + 1,
                    "to_order": i + 2,
                    "mode": detail.get("transit_mode", "walk") or "walk",
                    "coordinates": [
                        [p1["lng"], p1["lat"]],  # GeoJSON [lng, lat]
                        [p2["lng"], p2["lat"]],
                    ],
                }
            )

    # bounds 계산
    lats = [p["lat"] for p in route if p.get("lat") is not None]
    lngs = [p["lng"] for p in route if p.get("lng") is not None]

    if markers:
        blocks.append(
            {
                "type": "map_route",
                "course_id": course_id,
                "bounds": {
                    "sw": {"lat": min(lats), "lng": min(lngs)},
                    "ne": {"lat": max(lats), "lng": max(lngs)},
                },
                "center": {"lat": sum(lats) / len(lats), "lng": sum(lngs) / len(lngs)},
                "suggested_zoom": 14,
                "markers": markers,
                "polyline": {"type": "straight", "segments": segments},
            }
        )

    return blocks


# ---------------------------------------------------------------------------
# LangGraph 노드
# ---------------------------------------------------------------------------
async def course_plan_node(state: dict[str, Any]) -> dict[str, Any]:
    """COURSE_PLAN 노드 — 카테고리별 병렬 검색 → Greedy NN → LLM 코스 구성 (Phase 1).

    Args:
        state: AgentState dict.

    Returns:
        {"response_blocks": [text_stream, course, map_route]}.
    """
    from src.config import get_settings
    from src.db.opensearch import get_os_client
    from src.db.postgres import get_pool

    query = state.get("query", "")
    pq = state.get("processed_query") or {}

    district = pq.get("district")
    neighborhood = pq.get("neighborhood")
    pq_category = pq.get("category")
    expanded_query = pq.get("expanded_query") or query

    settings = get_settings()
    pool = get_pool()

    # ① 카테고리 파싱
    categories = _parse_categories(query, pq_category)

    # ② 카테고리별 병렬 검색
    os_client: Optional[Any] = None
    api_key: Optional[str] = settings.gemini_llm_api_key
    try:
        os_client = get_os_client()
    except RuntimeError:
        logger.warning("OpenSearch client not initialized, skipping vector search")

    candidates = await _search_by_categories(
        pool, os_client, categories, district, neighborhood, expanded_query, api_key
    )

    if not candidates:
        return {
            "response_blocks": [
                {
                    "type": "text_stream",
                    "system": _COURSE_SYSTEM_PROMPT,
                    "prompt": f"사용자 질문: {query}\n\n코스를 구성할 장소를 찾지 못했습니다. 다른 지역이나 카테고리로 다시 시도해보세요.",
                },
                {"type": "course", "course_id": str(uuid.uuid4()), "stops": [], "total_duration_min": 0},
            ]
        }

    # ②-b 편의점·마트 사전 필터링 + LLM Rerank
    candidates = [c for c in candidates if not any(ex in (c.get("name") or "") for ex in _COURSE_EXCLUDE_NAMES)]
    if not candidates:
        return {
            "response_blocks": [
                {
                    "type": "text_stream",
                    "system": _COURSE_SYSTEM_PROMPT,
                    "prompt": f"사용자 질문: {query}\n\n코스를 구성할 장소를 찾지 못했습니다. 다른 지역이나 카테고리로 다시 시도해보세요.",
                },
                {"type": "course", "course_id": str(uuid.uuid4()), "stops": [], "total_duration_min": 0},
            ]
        }
    candidates = await _llm_rerank_candidates(candidates, query, categories)
    # 안전망: LLM이 제외 대상을 포함시킨 경우 다시 제거
    candidates = [c for c in candidates if not any(ex in (c.get("name") or "") for ex in _COURSE_EXCLUDE_NAMES)]

    # ③ Greedy NN 경로 최적화
    route = _greedy_nn_route(candidates)

    # ④ LLM 코스 구성
    title, description, stop_details = await _llm_course_compose(route, query)

    # ⑤ 블록 생성
    course_id = str(uuid.uuid4())
    blocks = _build_blocks(query, route, title, description, stop_details, course_id)

    logger.info(
        "course_plan: categories=%s, candidates=%d, reranked→route=%d stops",
        categories,
        len(candidates),
        len(route),
    )

    return {"response_blocks": blocks}
