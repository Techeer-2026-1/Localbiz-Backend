"""P3-A: intent_router + query_preprocessor 단일 호출 통합 노드 (feature flag).

`settings.enable_combined_intent_preprocess=True` 일 때만 real_builder에서 사용.
기본 OFF — 정확도 evaluation 통과 후 ON.

단일 Gemini Flash JSON mode 호출에서:
  - intent 분류 (12+1 종)
  - confidence
  - processed_query 8필드 (district/category/keywords/expanded_query/date_*/time_reference)

정규식 prefilter(`utils.query_regex.extract_regex_fields`)로 LLM 응답의 빈 필드를 보완.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from src.graph._tracing import traced_node  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

_VALID_INTENTS: frozenset[str] = frozenset(
    {
        "PLACE_SEARCH",
        "PLACE_RECOMMEND",
        "EVENT_SEARCH",
        "EVENT_RECOMMEND",
        "COURSE_PLAN",
        "DETAIL_INQUIRY",
        "ANALYSIS",
        "COST_ESTIMATE",
        "CROWDEDNESS",
        "REVIEW_COMPARE",
        "IMAGE_SEARCH",
        "BOOKING",
        "CALENDAR",
        "REFINE",
        "GENERAL",
    }
)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_COMBINED_SYSTEM_PROMPT = """\
You are an intent classifier and query preprocessor for a Seoul local-life AI chatbot.

Classify the user's query into ONE intent below and extract structured fields in a single JSON response.

Intents:
- PLACE_SEARCH / PLACE_RECOMMEND
- EVENT_SEARCH / EVENT_RECOMMEND
- COURSE_PLAN
- DETAIL_INQUIRY / ANALYSIS / COST_ESTIMATE / CROWDEDNESS / REVIEW_COMPARE
- IMAGE_SEARCH / BOOKING / CALENDAR / REFINE / GENERAL

Return JSON only:
{
  "intent": "<one of above>",
  "confidence": <float 0~1>,
  "original_query": "<verbatim>",
  "expanded_query": "<clarified Korean>",
  "district": "<Seoul 자치구 or null>",
  "neighborhood": "<동/지역 or null>",
  "category": "<카페|음식점|... or null>",
  "keywords": ["..."],
  "date_reference": "<original date or null>",
  "date_start_resolved": "<YYYY-MM-DD or null>",
  "date_end_resolved": "<YYYY-MM-DD or null>",
  "time_reference": "<time or null>",
  "place_name": "<specific place or null>",
  "check_in": "<YYYY-MM-DD or null>",
  "check_out": "<YYYY-MM-DD or null>"
}

For GENERAL/IMAGE_SEARCH/CALENDAR/REFINE/BOOKING/COST_ESTIMATE/CROWDEDNESS intents,
structured fields beyond intent can be null/empty — keep minimal.

today={today}
"""


@traced_node("combined_intent_preprocess")
async def combined_intent_preprocess_node(state: dict[str, Any]) -> dict[str, Any]:
    """state["query"]에서 intent + processed_query 동시 추출.

    실패 시 GENERAL fallback + 빈 processed_query.
    """
    query: str = state.get("query") or ""
    if not query:
        return {"intent": "GENERAL", "processed_query": {}, "intent_confidence": 0.0}

    from langchain_google_genai import ChatGoogleGenerativeAI  # pyright: ignore[reportMissingImports]

    from src.config import get_settings  # pyright: ignore[reportMissingImports]
    from src.utils.llm_parsing import parse_llm_json  # pyright: ignore[reportMissingImports]
    from src.utils.query_regex import extract_regex_fields  # pyright: ignore[reportMissingImports]
    from src.utils.resilience import retry_call  # pyright: ignore[reportMissingImports]

    settings = get_settings()
    if not settings.gemini_llm_api_key:
        logger.warning("combined_intent_preprocess: GEMINI_LLM_API_KEY 미설정 → GENERAL fallback")
        return {"intent": "GENERAL", "processed_query": {}, "intent_confidence": 0.0}

    today_str = date.today().isoformat()
    system_prompt = _COMBINED_SYSTEM_PROMPT.replace("{today}", today_str)

    try:
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=settings.gemini_llm_api_key,
            temperature=0,
        )
        response = await retry_call(
            lambda: llm.ainvoke([("system", system_prompt), ("human", query)]),
            attempts=3,
        )
        raw = str(response.content).strip()
        result = parse_llm_json(raw)
    except Exception:
        logger.exception("combined_intent_preprocess failed → GENERAL fallback")
        return {"intent": "GENERAL", "processed_query": {}, "intent_confidence": 0.0}

    if not isinstance(result, dict):
        logger.warning("combined_intent_preprocess: 응답이 dict 아님 → GENERAL fallback")
        return {"intent": "GENERAL", "processed_query": {}, "intent_confidence": 0.0}

    intent = result.get("intent", "GENERAL")
    if intent not in _VALID_INTENTS:
        logger.warning("combined_intent_preprocess: 알 수 없는 intent=%s → GENERAL", intent)
        intent = "GENERAL"

    try:
        confidence = float(result.get("confidence", 0.0))
    except (ValueError, TypeError):
        confidence = 0.0

    pq: dict[str, Any] = {
        k: result.get(k)
        for k in (
            "original_query",
            "expanded_query",
            "district",
            "neighborhood",
            "category",
            "keywords",
            "date_reference",
            "date_start_resolved",
            "date_end_resolved",
            "time_reference",
            "place_name",
            "check_in",
            "check_out",
        )
    }
    pq.setdefault("original_query", query)
    pq.setdefault("expanded_query", query)
    pq.setdefault("keywords", [])

    try:
        regex_result = extract_regex_fields(query)
        for k, v in regex_result.items():
            if pq.get(k) is None:
                pq[k] = v
    except Exception:
        logger.exception("combined_intent_preprocess: regex 보완 실패 — 무시")

    for key in ("date_start_resolved", "date_end_resolved"):
        val = pq.get(key)
        if val is not None and (not isinstance(val, str) or not _ISO_DATE_RE.match(val)):
            pq[key] = None

    logger.info(
        "combined_intent_preprocess: intent=%s confidence=%.2f keys=%s",
        intent,
        confidence,
        list(pq.keys()),
    )
    return {"intent": intent, "processed_query": pq, "intent_confidence": confidence}
