"""EVENT_SEARCH 노드 — PG 정형 + OS events_vector k-NN + LLM Rerank 행사 검색 (Phase 1).

검색 흐름 (#110 검색 정확도 고도화 v2):
  1. processed_query에서 district/category/keywords/expanded_query/date_*_resolved 추출
  2. ① PG 정형 검색 (events WHERE district/category/title ILIKE + is_deleted=FALSE + date)
     ② OS events_vector k-NN 의미 검색 (expanded_query 768d) — ①②는 asyncio.gather 병렬
  3. 병합 + PG 2차 보강 (OS 결과는 events_vector에 표시 필드가 없어 PG 조회로 보강)
  4. Rerank 후보 한도 컷 (_MAX_PRERANK) — Naver append 전
  5. PG ∪ OS 병합 < 3건 → Naver 블로그 검색 API fallback (graceful degradation)
  6. LLM Rerank (Gemini Flash — 순위 재배치 + per-event 소개 동시 생성)
  7. response_blocks: events[] + text_stream(요약) + references[]

불변식 #2: event_id == events_vector._id (PG 부재 OS hit는 폐기)
불변식 #4: PG 2차 보강 쿼리에 is_deleted = FALSE
불변식 #7: gemini-embedding-001 768d
불변식 #8: asyncpg $1,$2 바인딩 — f-string SQL 금지 (CodeRabbit #3 학습 적용)
불변식 #13: DB(PG+OS) 우선 → Naver fallback (외부 API)
불변식 #19: 사용자 query / API 키 logger 진입 금지

Naver API:
  - Endpoint: https://openapi.naver.com/v1/search/blog.json
  - 인증: X-Naver-Client-Id, X-Naver-Client-Secret 헤더
  - 일 25,000회 호출 한도 (Phase 1은 caching 미구현)
  - timeout 5초
  - 실패 시 graceful degradation (빈 list 반환)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date
from typing import Any, Optional

logger = logging.getLogger(__name__)

_EVENT_SEARCH_SYSTEM_PROMPT = (
    "당신은 서울 로컬 라이프 AI 챗봇 'AnyWay'입니다. "
    "자기소개나 인사로 시작하지 말고 바로 본론으로 답변하세요. "
    "각 행사는 이미 카드로 소개되었습니다. "
    "전체 검색 결과를 종합하여 2-3문장으로 간결하게 요약해주세요. "
    "없으면 '검색 결과가 없습니다'라고 안내하세요.\n\n"
    "## 응답 형식 규칙\n"
    "- 주제가 바뀔 때 빈 줄로 단락을 구분하세요.\n"
    "- 핵심 정보는 **굵게** 강조하세요."
)

_RERANK_SYSTEM_PROMPT = """\
사용자의 조건에 가장 적합한 행사 순서를 매기고, 각 행사의 간결한 소개를 작성하세요.
후보 행사 목록과 사용자 조건이 주어집니다.

JSON으로만 응답하세요:
{
  "ranked_indices": [가장 적합한 행사의 index, 두 번째 index, ...],
  "reasons": {
    "0": "index 0 행사의 1-2문장 소개",
    "2": "index 2 행사의 1-2문장 소개"
  }
}

ranked_indices에는 상위 5개 index만 포함하세요.
reasons의 키는 후보 목록의 index를 문자열로 쓰고, 각 행사가 사용자 조건에
어떻게 부합하는지 1-2문장으로 작성하세요.
"""

_MAX_RESULTS = 5
_MIN_MERGED_RESULTS = 3  # PG∪OS 병합 결과가 이 개수 미만이면 Naver fallback 호출
_MAX_PRERANK = 10  # Rerank에 넘기는 OS+PG 후보 한도
_PG_LIMIT = 10
_OS_CANDIDATE_K = 50  # events_vector over-fetch 크기 (date post-filter 전)
_OS_TOP_K = 10  # date post-filter 후 OS 채널이 반환하는 최대 건수
_OS_MIN_SCORE = 0.4
_NAVER_DISPLAY = 5  # Naver API에서 가져올 최대 결과 수
_NAVER_TIMEOUT = 5.0  # 초
_NAVER_BLOG_URL = "https://openapi.naver.com/v1/search/blog.json"


# ---------------------------------------------------------------------------
# Gemini 768d 임베딩 (place_recommend_node 동일 로직 복제 — Simplicity First)
# ---------------------------------------------------------------------------
async def _embed_query_768d(query: str, api_key: str) -> list[float]:
    """Gemini embedding-001 768d 단건 임베딩. 불변식 #7."""
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


# ---------------------------------------------------------------------------
# OS events_vector k-NN 의미 검색 (over-fetch + Python date post-filter)
# ---------------------------------------------------------------------------
async def _search_os_events(
    os_client: Any,
    query: str,
    api_key: str,
    today_iso: str,
    date_start_resolved: Optional[str],
    date_end_resolved: Optional[str],
) -> list[dict[str, Any]]:
    """events_vector plain k-NN HNSW → Python date post-filter.

    events_vector(nmslib 엔진)는 k-NN 내부 filter 절을 지원하지 않으므로,
    _OS_CANDIDATE_K건을 over-fetch한 뒤 Python에서 date 조건으로 거른다.

    date post-filter (_search_pg overlap 조건 mirror, date 앞 10자 사전식 비교):
      - resolved date 둘 다 있을 때: date_end >= date_start_resolved
        AND date_start <= date_end_resolved (overlap, NULL date는 제외)
      - resolved date 없을 때: date_end >= today_iso (미종료 행사만, NULL 제외)

    Returns:
        date 유효 행사 상위 _OS_TOP_K건.
        [{"event_id", "title", "category", "district", "date_start",
          "date_end", "source", "score"}]
        실패 시 빈 list (graceful degradation).
    """
    try:
        query_vector = await _embed_query_768d(query, api_key)

        # zero-vector 가드: 임베딩 API 실패 시 k-NN이 무의미한 순위를 내므로 skip
        if not any(query_vector):
            logger.warning("events_vector search skipped: zero embedding vector")
            return []

        body: dict[str, Any] = {
            "size": _OS_CANDIDATE_K,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": query_vector,
                        "k": _OS_CANDIDATE_K,
                    }
                }
            },
            "min_score": _OS_MIN_SCORE,
            "_source": [
                "event_id",
                "title",
                "category",
                "district",
                "date_start",
                "date_end",
                "source",
            ],
        }

        result = await os_client.search(index="events_vector", body=body)
        hits = result.get("hits", {}).get("hits", [])

        events: list[dict[str, Any]] = []
        for hit in hits:
            source = hit.get("_source", {})

            # date post-filter — _search_pg overlap 조건 mirror (date 앞 10자 사전식 비교)
            date_end = source.get("date_end")
            date_start = source.get("date_start")
            de = str(date_end)[:10] if date_end else ""
            ds = str(date_start)[:10] if date_start else ""

            if date_start_resolved and date_end_resolved:
                # overlap: date_end >= start AND date_start <= end (NULL date는 제외)
                if not de or de < date_start_resolved[:10]:
                    continue
                if not ds or ds > date_end_resolved[:10]:
                    continue
            else:
                # resolved date 없을 때: 미종료 행사만 (date_end >= today)
                if not de or de < today_iso:
                    continue

            events.append(
                {
                    "event_id": hit.get("_id", "") or source.get("event_id", ""),
                    "title": source.get("title", ""),
                    "category": source.get("category", ""),
                    "district": source.get("district", ""),
                    "date_start": source.get("date_start"),
                    "date_end": source.get("date_end"),
                    "source": source.get("source", ""),
                    "score": hit.get("_score", 0),
                }
            )
            if len(events) >= _OS_TOP_K:
                break

        return events

    except Exception:
        logger.exception("OS events_vector search failed")
        return []


# ---------------------------------------------------------------------------
# PostgreSQL 검색
# ---------------------------------------------------------------------------
async def _search_pg(
    pool: Any,
    district: Optional[str],
    category: Optional[str],
    keywords: list[str],
    date_start_resolved: Optional[str] = None,
    date_end_resolved: Optional[str] = None,
) -> list[dict[str, Any]]:
    """events 테이블에서 조건부 필터 검색. is_deleted=FALSE 강제.

    필터 전략 (#76 정확도 강화 v1):
      - is_deleted = FALSE: 소프트 삭제 행사 제외 (불변식 #4)
      - district: 자치구 정확 매칭 ("강남구")
      - category: 카테고리 ILIKE ("%전시회%")
      - keywords: **배열 전체** OR 매칭 — (title ILIKE $a OR title ILIKE $b ...)
      - date 범위 (date_start_resolved/end_resolved 둘 다 있을 때):
        events.date_end >= $start AND events.date_start <= $end (overlap 매칭)
      - date 범위 없을 때: date_end >= NOW() (Phase 1 fallback, 지난 행사 제외)

    NULL 행사 동작: events.date_start 또는 date_end가 NULL이면
    overlap 조건이 false → 자연 제외 (의도된 동작).

    SQL 구성 정책 (불변식 #8):
      - f-string SQL 사용 금지
      - placeholder는 str(len(params)) concat으로 생성
      - 값은 항상 params 리스트로 분리 → fetch(*params)로 바인딩
    """
    base_sql = (
        "SELECT event_id, title, category, place_name, address, district, "
        "ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng, "
        "date_start, date_end, price, poster_url, detail_url, summary, source "
        "FROM events WHERE is_deleted = FALSE"
    )
    conditions: list[str] = []
    params: list[Any] = []

    # date 범위 — 둘 다 있을 때만 overlap, 아니면 date_end >= NOW() fallback
    if date_start_resolved and date_end_resolved:
        params.append(date_start_resolved)
        start_ph = "$" + str(len(params))
        params.append(date_end_resolved)
        end_ph = "$" + str(len(params))
        conditions.append("date_end >= " + start_ph + " AND date_start <= " + end_ph)
    else:
        conditions.append("date_end >= NOW()")

    if district:
        params.append(district)
        conditions.append("district = $" + str(len(params)))

    if category:
        params.append("%" + category + "%")
        conditions.append("category ILIKE $" + str(len(params)))

    if keywords:
        # keywords 배열 전체로 title OR 매칭
        kw_placeholders: list[str] = []
        for kw in keywords:
            params.append("%" + kw + "%")
            kw_placeholders.append("title ILIKE $" + str(len(params)))
        conditions.append("(" + " OR ".join(kw_placeholders) + ")")

    sql = base_sql + " AND " + " AND ".join(conditions)

    params.append(_PG_LIMIT)
    sql = sql + " ORDER BY date_start ASC LIMIT $" + str(len(params))

    try:
        rows = await pool.fetch(sql, *params)
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("PG event search failed")
        return []


# ---------------------------------------------------------------------------
# Naver 검색 API fallback
# ---------------------------------------------------------------------------
async def _search_naver(
    query: str,
    client_id: str,
    client_secret: str,
) -> list[dict[str, Any]]:
    """Naver 블로그 검색 API 호출. 실패 시 graceful degradation (빈 list).

    Args:
        query: 검색어 (사용자 query 또는 키워드 조합)
        client_id: NAVER_CLIENT_ID
        client_secret: NAVER_CLIENT_SECRET

    Returns:
        Naver items[] 원본 list (변환은 _naver_to_event_dict).

    실패 시 (timeout, 5xx, 인증 오류):
        빈 list 반환 + logger.warning. 사용자에겐 PG 결과만 노출.
    """
    if not client_id or not client_secret:
        logger.warning("naver search skipped: client_id or client_secret empty")
        return []

    import httpx  # pyright: ignore[reportMissingImports]

    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }
    params = {
        "query": query,
        "display": _NAVER_DISPLAY,
        "sort": "sim",  # 정확도순
    }

    try:
        async with httpx.AsyncClient(timeout=_NAVER_TIMEOUT) as client:
            resp = await client.get(_NAVER_BLOG_URL, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
        return list(data.get("items", []))
    except Exception:
        logger.exception("naver event search failed (graceful degradation)")
        return []


def _naver_to_event_dict(item: dict[str, Any]) -> dict[str, Any]:
    """Naver 블로그 검색 응답 item → 우리 events dict 양식 변환.

    Naver 응답 양식:
      - title: 블로그 제목 (HTML <b> 태그 포함 가능)
      - link: 블로그 URL
      - description: 본문 일부 (HTML 태그 포함 가능)
      - bloggername: 블로거 이름
      - postdate: 게시일 (YYYYMMDD)

    우리 events 양식 (event_id 등 DB 필드는 None):
      - title, summary, detail_url, source 만 채움.
    """
    # <b> 태그 제거 (Naver 검색 결과 강조 표시)
    title_clean = re.sub(r"</?b>", "", item.get("title") or "")
    desc_clean = re.sub(r"</?b>", "", item.get("description") or "")

    return {
        "event_id": None,
        "title": title_clean,
        "category": None,
        "place_name": None,
        "address": None,
        "district": None,
        "lat": None,
        "lng": None,
        "date_start": None,
        "date_end": None,
        "price": None,
        "poster_url": None,
        "detail_url": item.get("link"),
        "summary": desc_clean,
        "source": "naver_blog",
    }


# ---------------------------------------------------------------------------
# 병합 + PG 2차 보강
# ---------------------------------------------------------------------------
async def _merge_candidates(
    pool: Any,
    pg_results: list[dict[str, Any]],
    os_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """PG 정형 + OS 의미 결과 병합. OS 결과는 PG 2차 보강으로 카드 필드를 채운다.

    events_vector에는 place_name/address/lat/lng/price/detail_url/summary 가
    없으므로 OS hit의 event_id로 PG를 다시 조회해 표시 필드를 보강한다.

    동작:
      - OS hit event_id → PG 조회(is_deleted=FALSE) → 카드 필드 보강.
      - PG에 없는 OS hit(하드 삭제 등 PG↔OS 비동기화)는 폐기 — 렌더 불가.
      - PG 2차 조회 자체가 예외(DB 일시 장애)면 graceful — OS 보강분 비우고
        PG 정형 결과로 진행.
      - event_id(VARCHAR) 기준 중복 제거. 병합 순서: OS 우선 → PG.

    Returns:
        병합 후보 list (Rerank 입력).
    """
    enriched_map: dict[str, dict[str, Any]] = {}
    os_ids = [str(e["event_id"]) for e in os_results if e.get("event_id")]

    if os_ids:
        try:
            rows = await pool.fetch(
                "SELECT event_id, title, category, place_name, address, district, "
                "ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng, "
                "date_start, date_end, price, poster_url, detail_url, summary, source "
                "FROM events WHERE event_id = ANY($1::varchar[]) AND is_deleted = FALSE",
                os_ids,
            )
            enriched_map = {str(r["event_id"]): dict(r) for r in rows}
        except Exception:
            logger.exception("PG 2차 보강 조회 실패 (graceful degradation)")

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    # OS 의미 결과 우선 — 보강 성공분만 (os_results 순서 = 유사도 desc 보존)
    for e in os_results:
        eid = str(e.get("event_id", ""))
        if not eid or eid in seen:
            continue
        enriched = enriched_map.get(eid)
        if enriched is None:
            continue  # PG 부재 → 폐기
        seen.add(eid)
        merged.append(enriched)

    # PG 정형 결과 추가
    for e in pg_results:
        eid = str(e.get("event_id", ""))
        if not eid or eid in seen:
            continue
        seen.add(eid)
        merged.append(e)

    return merged


# ---------------------------------------------------------------------------
# LLM Rerank (Gemini Flash) — 순위 재배치 + per-event 소개 동시 생성
# ---------------------------------------------------------------------------
async def _llm_rerank(
    candidates: list[dict[str, Any]],
    query: str,
    keywords: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Gemini Flash로 행사 후보 순위 재배치 + per-event 소개 생성.

    후보를 index 기반으로 LLM에 제시한다 (Naver fallback 행사는 event_id가
    None이라 event_id 키를 쓸 수 없으므로).

    Returns:
        (reranked_top5, descriptions)
        descriptions[i]는 reranked_top5[i]에 정렬된 소개 문자열.
        graceful degradation: 실패 시 (candidates[:_MAX_RESULTS], []) — 병합 순서 유지.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI

    from src.config import get_settings

    settings = get_settings()
    if not settings.gemini_llm_api_key or not candidates:
        return candidates[:_MAX_RESULTS], []

    candidate_lines: list[str] = []
    for i, c in enumerate(candidates):
        candidate_lines.append(
            f"- index={i}, title={c.get('title', '')}, "
            f"category={c.get('category') or '미정'}, district={c.get('district') or '미정'}"
        )
    user_prompt = f"사용자 조건: {query}\n키워드: {', '.join(keywords)}\n\n후보 행사:\n" + "\n".join(candidate_lines)

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
        ranked_indices: list[Any] = result.get("ranked_indices", [])
        reasons: dict[str, str] = result.get("reasons", {})

        reranked: list[dict[str, Any]] = []
        descriptions: list[str] = []
        used: set[int] = set()

        # ranked_indices 순서로 재배치
        for idx in ranked_indices:
            if isinstance(idx, int) and 0 <= idx < len(candidates) and idx not in used:
                used.add(idx)
                reranked.append(candidates[idx])
                descriptions.append(reasons.get(str(idx), ""))
                if len(reranked) >= _MAX_RESULTS:
                    break

        # ranked_indices에 없는 후보도 원본 순서로 채움
        if len(reranked) < _MAX_RESULTS:
            for i, c in enumerate(candidates):
                if i not in used and len(reranked) < _MAX_RESULTS:
                    used.add(i)
                    reranked.append(c)
                    descriptions.append(reasons.get(str(i), ""))

        return reranked, descriptions

    except Exception:
        logger.exception("LLM rerank failed → fallback to original order")
        return candidates[:_MAX_RESULTS], []


# ---------------------------------------------------------------------------
# 블록 생성
# ---------------------------------------------------------------------------
def _build_blocks(
    query: str,
    events: list[dict[str, Any]],
    descriptions: list[str],
) -> list[dict[str, Any]]:
    """검색 결과 → events(+description) + text_stream(종합 요약) + references 블록.

    events 중 source='naver_blog' 항목은 references[] 블록에 추가 노출 (출처 링크).
    """
    blocks: list[dict[str, Any]] = []

    # 1. events 블록 (카드 먼저 전송)
    event_items: list[dict[str, Any]] = []
    for i, e in enumerate(events):
        item: dict[str, Any] = {
            "type": "event",
            "title": e.get("title", ""),
        }
        # DB 행사만 가지는 필드 (Naver는 None)
        if e.get("event_id") is not None:
            item["event_id"] = e["event_id"]
        if e.get("category"):
            item["category"] = e["category"]
        if e.get("place_name"):
            item["place_name"] = e["place_name"]
        if e.get("address"):
            item["address"] = e["address"]
        if e.get("district"):
            item["district"] = e["district"]
        if e.get("lat") is not None:
            item["lat"] = e["lat"]
        if e.get("lng") is not None:
            item["lng"] = e["lng"]
        if e.get("date_start") is not None:
            item["date_start"] = str(e["date_start"])
        if e.get("date_end") is not None:
            item["date_end"] = str(e["date_end"])
        if e.get("price") is not None:
            item["price"] = e["price"]
        if e.get("poster_url"):
            item["poster_url"] = e["poster_url"]
        if e.get("detail_url"):
            item["detail_url"] = e["detail_url"]
        if e.get("summary"):
            item["summary"] = e["summary"]
        if e.get("source"):
            item["source"] = e["source"]
        # per-event description
        if descriptions and i < len(descriptions) and descriptions[i]:
            item["description"] = descriptions[i]
        event_items.append(item)

    if event_items:
        blocks.append(
            {
                "type": "events",
                "items": event_items,
                "total_count": len(event_items),
            }
        )

    # 2. text_stream: 종합 요약 (카드 뒤에 스트리밍)
    if events:
        result_summary = "\n".join(
            f"- {e.get('title', '')} ({e.get('category') or '카테고리 미정'}, "
            f"{e.get('district') or e.get('source', '')})"
            for e in events
        )
        prompt = f"사용자 질문: {query}\n\n검색 결과:\n{result_summary}\n\n위 결과를 종합 요약해주세요."
    else:
        prompt = f"사용자 질문: {query}\n\n검색 결과가 없습니다. 다른 검색어를 제안해주세요."

    blocks.append(
        {
            "type": "text_stream",
            "system": _EVENT_SEARCH_SYSTEM_PROMPT,
            "prompt": prompt,
        }
    )

    # 3. references 블록 (Naver fallback 결과만 — 출처 링크)
    references: list[dict[str, Any]] = []
    for e in events:
        if e.get("source") == "naver_blog" and e.get("detail_url"):
            references.append(
                {
                    "title": e.get("title", ""),
                    "url": e["detail_url"],
                    "source": "naver_blog",
                }
            )

    if references:
        blocks.append(
            {
                "type": "references",
                "items": references,
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
    """EVENT_SEARCH refinement 처리 — 이전 결과 기반 수정."""
    if state.get("_refine_depth", 0) > 1:
        clean = dict(state)
        clean["previous_blocks"] = None
        clean["refinement"] = None
        return await event_search_node(clean)

    from src.graph.refine_helpers import (  # pyright: ignore[reportMissingImports]
        apply_add,
        apply_remove,
        apply_replace,
        extract_items_from_blocks,
    )

    action = refinement.get("action", "regenerate")
    target_index = refinement.get("target_index")
    query = state.get("query", "")

    if action in ("regenerate", "change_condition"):
        if action == "change_condition":
            new_q = refinement.get("new_condition", query)
            state = dict(state)
            state["query"] = new_q
            state["previous_blocks"] = None
            state["refinement"] = None
        return await event_search_node(state)

    prev_items = extract_items_from_blocks(previous_blocks, "events")
    if not prev_items:
        return await event_search_node(state)

    if action == "remove" and target_index is not None:
        items = apply_remove(prev_items, target_index)
    elif action in ("replace", "add"):
        clean_state = dict(state)
        clean_state["previous_blocks"] = None
        clean_state["refinement"] = None
        if action == "replace" and refinement.get("new_condition"):
            clean_state["query"] = refinement["new_condition"]
        result = await event_search_node(clean_state)
        new_blocks = result.get("response_blocks", [])
        new_items = extract_items_from_blocks(new_blocks, "events")
        existing_ids = {it.get("event_id", "") for it in prev_items if isinstance(it, dict)}
        new_candidates = [it for it in new_items if it.get("event_id", "") not in existing_ids]

        no_candidate_found = not new_candidates
        if no_candidate_found:
            items = prev_items
        elif action == "replace" and target_index is not None:
            items = apply_replace(prev_items, target_index, new_candidates[0])
        else:
            items = apply_add(prev_items, new_candidates[0])
    else:
        items = prev_items
        no_candidate_found = False

    blocks: list[dict[str, Any]] = []
    if items:
        blocks.append({"type": "events", "items": items, "total_count": len(items)})
    result_summary = "\n".join(f"- {r.get('title', '')}" for r in items)
    if no_candidate_found:
        prompt = f"사용자 요청: {query}\n\n조건에 맞는 대체 행사를 찾지 못해 기존 결과를 유지합니다. 다른 조건으로 다시 요청해보라고 1-2문장으로 안내해주세요."
    else:
        prompt = f"사용자 요청: {query}\n\n수정된 행사 결과:\n{result_summary}\n\n위 결과를 종합 요약해주세요."
    blocks.append({"type": "text_stream", "system": _EVENT_SEARCH_SYSTEM_PROMPT, "prompt": prompt})
    return {"response_blocks": blocks}


async def event_search_node(state: dict[str, Any]) -> dict[str, Any]:
    """EVENT_SEARCH 노드 — PG 정형 + OS k-NN + LLM Rerank 행사 검색 (Phase 1).

    Args:
        state: AgentState dict (query, processed_query 등).

    Returns:
        {"response_blocks": [events, text_stream, references?]}.
    """
    previous_blocks = state.get("previous_blocks")
    refinement = state.get("refinement")
    if previous_blocks and refinement:
        return await _handle_refinement(state, previous_blocks, refinement)

    from src.config import get_settings  # pyright: ignore[reportMissingImports]
    from src.db.opensearch import get_os_client  # pyright: ignore[reportMissingImports]
    from src.db.postgres import get_pool  # pyright: ignore[reportMissingImports]

    query = state.get("query", "")
    pq = state.get("processed_query") or {}

    district = pq.get("district")
    category = pq.get("category")
    keywords = pq.get("keywords", [])
    date_start_resolved = pq.get("date_start_resolved")
    date_end_resolved = pq.get("date_end_resolved")
    expanded_query = pq.get("expanded_query") or query

    settings = get_settings()
    pool = get_pool()
    today_iso = date.today().isoformat()

    # 1) ① PG 정형 + ② OS events_vector k-NN 병렬
    pg_task = _search_pg(pool, district, category, keywords, date_start_resolved, date_end_resolved)

    os_task: Optional[asyncio.Task[list[dict[str, Any]]]] = None
    if settings.gemini_llm_api_key:
        try:
            os_client = get_os_client()
            os_task = asyncio.create_task(
                _search_os_events(
                    os_client,
                    expanded_query,
                    settings.gemini_llm_api_key,
                    today_iso,
                    date_start_resolved,
                    date_end_resolved,
                )
            )
        except RuntimeError:
            logger.warning("OpenSearch client not initialized, skipping vector search")

    pg_events = await pg_task

    os_events: list[dict[str, Any]] = []
    if os_task is not None:
        os_events = await os_task

    # 2) 병합 + PG 2차 보강
    merged = await _merge_candidates(pool, pg_events, os_events)

    # 3) Rerank 후보 한도 컷 (Naver append 전 — Naver 결과가 잘리지 않도록)
    merged = merged[:_MAX_PRERANK]
    merged_count = len(merged)

    # 4) PG∪OS 병합 부족 시 Naver fallback
    naver_events: list[dict[str, Any]] = []
    if merged_count < _MIN_MERGED_RESULTS:
        # 검색어 조합: keywords 우선, 없으면 query 자체 (앞 100자)
        naver_query = " ".join(keywords) if keywords else query[:100]
        naver_items = await _search_naver(
            naver_query,
            settings.naver_client_id,
            settings.naver_client_secret,
        )
        naver_events = [_naver_to_event_dict(item) for item in naver_items]
        merged += naver_events

    # 5) LLM Rerank (순위 재배치 + per-event 소개)
    reranked, descriptions = await _llm_rerank(merged, query, keywords)

    # 6) 블록 생성
    blocks = _build_blocks(query, reranked, descriptions)

    logger.info(
        "event_search: pg=%d, os=%d, merged(pg∪os)=%d, naver=%d, final=%d (district=%s, category=%s)",
        len(pg_events),
        len(os_events),
        merged_count,
        len(naver_events),
        len(reranked),
        district,
        category,
    )

    return {"response_blocks": blocks}
