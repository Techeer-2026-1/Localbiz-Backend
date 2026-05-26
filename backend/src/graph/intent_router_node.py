"""LangGraph intent router 노드 — 15 intent 분류.

기획서 section 3.1 권위. 14 핵심 intent + 1 fallback (GENERAL).
Gemini 2.5 Flash JSON-mode로 분류 (Phase 1 본작업에서 구현).
"""

from __future__ import annotations

import logging
from enum import StrEnum  # pyright: ignore[reportAttributeAccessIssue]
from typing import Any, Optional

logger = logging.getLogger(__name__)


class IntentType(StrEnum):  # pyright: ignore[reportAttributeAccessIssue]
    """기획서 section 3.1 — 15 intent (14+1). 추가/변경 시 PM 합의 + 기획서 동기화."""

    # Phase 1
    PLACE_SEARCH = "PLACE_SEARCH"
    PLACE_RECOMMEND = "PLACE_RECOMMEND"
    EVENT_SEARCH = "EVENT_SEARCH"
    EVENT_RECOMMEND = "EVENT_RECOMMEND"
    COURSE_PLAN = "COURSE_PLAN"
    DETAIL_INQUIRY = "DETAIL_INQUIRY"
    BOOKING = "BOOKING"
    CALENDAR = "CALENDAR"
    FAVORITE = "FAVORITE"
    # Phase 1 (기획서 v2 SSE L155)
    REVIEW_COMPARE = "REVIEW_COMPARE"
    ANALYSIS = "ANALYSIS"
    COST_ESTIMATE = "COST_ESTIMATE"
    CROWDEDNESS = "CROWDEDNESS"
    IMAGE_SEARCH = "IMAGE_SEARCH"
    REFINE = "REFINE"
    # Fallback
    GENERAL = "GENERAL"


# Phase 1 intent (기획서 기준 전체 목록)
PHASE1_INTENTS: frozenset[IntentType] = frozenset(  # pyright: ignore[reportAssignmentType]
    {
        IntentType.PLACE_SEARCH,
        IntentType.PLACE_RECOMMEND,
        IntentType.EVENT_SEARCH,
        IntentType.EVENT_RECOMMEND,
        IntentType.COURSE_PLAN,
        IntentType.DETAIL_INQUIRY,
        IntentType.BOOKING,
        IntentType.CALENDAR,
        IntentType.FAVORITE,
        IntentType.REVIEW_COMPARE,
        IntentType.CROWDEDNESS,
        IntentType.IMAGE_SEARCH,
        IntentType.REFINE,
        IntentType.ANALYSIS,
        IntentType.COST_ESTIMATE,
        IntentType.GENERAL,
    }
)

# 실제 그래프에 라우팅 가능한 intent (real_builder.py conditional_edges 기준)
# 여기에 없는 Phase 1 intent는 GENERAL fallback 처리
_ROUTABLE_INTENTS: frozenset[IntentType] = frozenset(  # pyright: ignore[reportAssignmentType]
    {
        IntentType.PLACE_SEARCH,
        IntentType.PLACE_RECOMMEND,
        IntentType.EVENT_SEARCH,
        IntentType.EVENT_RECOMMEND,
        IntentType.COURSE_PLAN,
        IntentType.DETAIL_INQUIRY,
        IntentType.BOOKING,
        IntentType.CALENDAR,
        IntentType.REVIEW_COMPARE,
        IntentType.CROWDEDNESS,
        IntentType.IMAGE_SEARCH,
        IntentType.REFINE,
        IntentType.ANALYSIS,
        IntentType.COST_ESTIMATE,
        IntentType.GENERAL,
    }
)


_GENERAL_FALLBACK: IntentType = IntentType.GENERAL  # pyright: ignore[reportAssignmentType]


_CLASSIFY_SYSTEM_PROMPT = """\
You are an intent classifier for a Seoul local-life AI chatbot.
Classify the user query into exactly ONE of these intents:

Phase 1 (active):
- PLACE_SEARCH: searching for specific places (restaurants, cafes, etc.)
- PLACE_RECOMMEND: asking for place recommendations
- EVENT_SEARCH: searching for cultural events, festivals, exhibitions
- EVENT_RECOMMEND: asking for event recommendations
- COURSE_PLAN: planning a course/itinerary with multiple stops
- DETAIL_INQUIRY: asking detailed info about a specific place or event
- BOOKING: requesting a reservation or booking link
- CALENDAR: adding an event to calendar
- FAVORITE: bookmarking or favoriting something
- REVIEW_COMPARE: comparing two or more places by 6 metrics (satisfaction/accessibility/cleanliness/value/atmosphere/expertise)
- CROWDEDNESS: asking about current crowdedness, busyness, or population density of an area.
  Accept noun-phrase queries without verbs (e.g. "홍대 혼잡도", "강남 사람 많아?", "지금 이태원").
  Trigger keywords: "혼잡", "혼잡도", "붐비", "사람 많", "사람 적", "한산", "유동인구", "생활인구", "지금 ~ 어때".
  Example: "현재 홍대 혼잡도", "강남역 사람 많아?", "이태원 지금 붐비나?", "성수동 한산해?" → CROWDEDNESS.
- COST_ESTIMATE: asking about expected cost or price range for a place, restaurant, or activity
- IMAGE_SEARCH: user sends an image URL (http/https link ending in image extension or storage URL) to identify a place or find similar places; also when user refers to a previously sent image ("아까 그 사진", "방금 올린 이미지", "그 사진 어딘지", "이전 사진") without a new URL
- REFINE: user wants to modify, replace, remove, add to, or regenerate a previous response. Examples: "3번 장소 바꿔줘", "그거 말고 다른 거", "카페 빼줘", "다시 추천해줘", "강남 말고 홍대로", "마음에 안 들어", "2번 행사 다른 걸로", "하나 더 추가해줘". NOT REFINE (classify as GENERAL instead): questions about a previous response ("이게 뭐야?", "씨엘이 홍대 씨엘인 거 아니야?", "여기 맛있어?", "몇 시에 문 닫아?"), opinions ("좋다", "괜찮네"), or follow-up questions that don't request a change.
- GENERAL: general conversation, greetings, questions about previous results, or anything else

Phase 2 (not yet active, classify as GENERAL for now):
- ANALYSIS: analyzing a single place with 6 metrics (satisfaction/accessibility/cleanliness/value/atmosphere/expertise)

SEARCH vs RECOMMEND (important):
- Use PLACE_SEARCH / EVENT_SEARCH when the user looks something up with concrete criteria
  already in mind — a location, a category, a name, or verbs like "찾아줘", "알려줘", "있어?",
  "어디야", "보여줘". Example: "강남 맛집 찾아줘", "주말 전시회 알려줘" → *_SEARCH.
- Use PLACE_RECOMMEND / EVENT_RECOMMEND ONLY when the user explicitly asks for a suggestion and
  leaves the choice open — "추천", "추천해줘", "갈 만한 곳", "괜찮은 데", "뭐가 좋아?".
  Example: "홍대 카페 추천해줘" → PLACE_RECOMMEND.
- When ambiguous between SEARCH and RECOMMEND, prefer SEARCH.

Respond in JSON: {"intent": "INTENT_NAME", "confidence": 0.0-1.0}
"""


_CLASSIFY_MULTI_SYSTEM_PROMPT = """\
You are an intent classifier for a Seoul local-life AI chatbot.
The user query may contain multiple distinct requests. Identify ALL intents.

Phase 1 (active):
- PLACE_SEARCH: searching for specific places (restaurants, cafes, etc.)
- PLACE_RECOMMEND: asking for place recommendations
- EVENT_SEARCH: searching for cultural events, festivals, exhibitions
- EVENT_RECOMMEND: asking for event recommendations
- COURSE_PLAN: planning a course/itinerary with multiple stops
- DETAIL_INQUIRY: asking detailed info about a specific place or event
- BOOKING: requesting a reservation or booking link
- CALENDAR: adding an event to calendar
- FAVORITE: bookmarking or favoriting something
- REVIEW_COMPARE: comparing two or more places by 6 metrics
- CROWDEDNESS: asking about current crowdedness, busyness, or population density of an area.
  Accept noun-phrase queries without verbs (e.g. "홍대 혼잡도", "강남 사람 많아?", "지금 이태원").
  Trigger keywords: "혼잡", "혼잡도", "붐비", "사람 많", "사람 적", "한산", "유동인구", "생활인구".
- COST_ESTIMATE: asking about expected cost or price range for a place, restaurant, or activity
- IMAGE_SEARCH: user sends an image URL to identify a place or find similar places; also when user refers to a previously sent image without a new URL
- REFINE: user wants to modify, replace, remove, add to, or regenerate a previous response. Examples: "3번 장소 바꿔줘", "그거 말고 다른 거", "카페 빼줘", "다시 추천해줘", "강남 말고 홍대로", "마음에 안 들어", "2번 행사 다른 걸로", "하나 더 추가해줘". NOT REFINE (classify as GENERAL instead): questions about a previous response ("이게 뭐야?", "씨엘이 홍대 씨엘인 거 아니야?", "여기 맛있어?", "몇 시에 문 닫아?"), opinions ("좋다", "괜찮네"), or follow-up questions that don't request a change.
- GENERAL: general conversation, greetings, questions about previous results, or anything else

Phase 2 (not yet active, classify as GENERAL for now):
- ANALYSIS: analyzing a single place with 6 metrics (satisfaction/accessibility/cleanliness/value/atmosphere/expertise)

SEARCH vs RECOMMEND (important):
- Use PLACE_SEARCH / EVENT_SEARCH when the user looks something up with concrete criteria
  already in mind — a location, a category, a name, or verbs like "찾아줘", "알려줘", "있어?",
  "어디야", "보여줘". Example: "강남 맛집 찾아줘", "주말 전시회 알려줘" → *_SEARCH.
- Use PLACE_RECOMMEND / EVENT_RECOMMEND ONLY when the user explicitly asks for a suggestion and
  leaves the choice open — "추천", "추천해줘", "갈 만한 곳", "괜찮은 데", "뭐가 좋아?".
  Example: "홍대 카페 추천해줘" → PLACE_RECOMMEND.
- When ambiguous between SEARCH and RECOMMEND, prefer SEARCH.

Rules:
- If the query has ONE purpose, return ONE intent. Example: "카페에서 전시회 가는 코스" → COURSE_PLAN only.
- If the query has MULTIPLE distinct requests joined by "~하고", "~그리고", "~도", return MULTIPLE intents.
- Maximum 3 intents per query.
- sub_query: the portion of the original query for each intent (in Korean).
- If only one intent, sub_query = the original query.
- IMPORTANT: each sub_query MUST be self-contained — preserve its OWN region/place/time
  context. Never drop the location from a sub_query, even if that location is mentioned
  only once in the original query. Carry the relevant region into every sub_query it applies to.
  Example: "명동에서 좋은 카페 찾아주고 홍대에서는 밥집 하나 찾아줘"
    → {"intents": [
         {"intent": "PLACE_SEARCH", "confidence": 0.9, "sub_query": "명동에서 좋은 카페 찾아줘"},
         {"intent": "PLACE_SEARCH", "confidence": 0.9, "sub_query": "홍대에서 밥집 하나 찾아줘"}
       ]}

Respond in JSON: {"intents": [{"intent": "...", "confidence": 0.0-1.0, "sub_query": "..."}, ...]}
"""

_MAX_INTENTS = 3


async def classify_intents(
    query: str,
    conversation_history: Optional[list[dict[str, str]]] = None,
) -> list[tuple[IntentType, float, str]]:
    """복수 intent 분류. 최대 3개.

    Args:
        query: 사용자 원본 쿼리.
        conversation_history: 이전 대화 이력.

    Returns:
        [(IntentType, confidence, sub_query), ...]. 최소 1개 보장.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI

    from src.config import get_settings  # pyright: ignore[reportMissingImports]
    from src.utils.llm_parsing import parse_llm_json  # pyright: ignore[reportMissingImports]
    from src.utils.resilience import retry_call  # pyright: ignore[reportMissingImports]

    settings = get_settings()
    if not settings.gemini_llm_api_key:
        logger.warning("classify_intents: GEMINI_LLM_API_KEY 미설정 → GENERAL fallback")
        return [(_GENERAL_FALLBACK, 0.0, query)]

    try:
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=settings.gemini_llm_api_key,
            temperature=0,
        )

        messages: list[tuple[str, str]] = [("system", _CLASSIFY_MULTI_SYSTEM_PROMPT)]

        if conversation_history:
            for msg in conversation_history[-5:]:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                lc_role = "human" if role == "user" else "ai"
                messages.append((lc_role, content))

        messages.append(("human", query))

        response = await retry_call(lambda: llm.ainvoke(messages), attempts=3)
        text = str(response.content).strip()

        result = parse_llm_json(text)
        raw_intents = result.get("intents", [])
        if not isinstance(raw_intents, list) or len(raw_intents) == 0:
            return [(_GENERAL_FALLBACK, 0.0, query)]

        parsed: list[tuple[IntentType, float, str]] = []
        for item in raw_intents[:_MAX_INTENTS]:
            intent_str = item.get("intent", "GENERAL")
            confidence = float(item.get("confidence", 0.0))
            sub_query = item.get("sub_query", "") or query

            try:
                intent = IntentType(intent_str)
            except ValueError:
                logger.warning("classify_intents: 알 수 없는 intent=%s → GENERAL", intent_str)
                intent = _GENERAL_FALLBACK

            if intent not in PHASE1_INTENTS:
                logger.info("classify_intents: Phase 2 intent=%s → GENERAL", intent.value)
                intent = _GENERAL_FALLBACK

            if intent not in _ROUTABLE_INTENTS:
                logger.info("classify_intents: 라우팅 불가 intent=%s → GENERAL", intent.value)
                intent = _GENERAL_FALLBACK

            parsed.append((intent, confidence, sub_query))

        # 중복 GENERAL 제거 (GENERAL이 여러 개면 1개만)
        seen_general = False
        deduped: list[tuple[IntentType, float, str]] = []
        for item in parsed:
            if item[0] == _GENERAL_FALLBACK:
                if seen_general:
                    continue
                seen_general = True
            deduped.append(item)

        return deduped if deduped else [(_GENERAL_FALLBACK, 0.0, query)]

    except Exception:
        logger.exception("classify_intents failed → GENERAL fallback")
        return [(_GENERAL_FALLBACK, 0.0, query)]


async def classify_intent(
    query: str,
    conversation_history: Optional[list[dict[str, str]]] = None,
) -> tuple[IntentType, float]:
    """사용자 쿼리 → (IntentType, confidence) 분류.

    Gemini 2.5 Flash JSON-mode로 15 intent 중 하나를 분류.
    실패 시 (GENERAL, 0.0) fallback.

    Args:
        query: 사용자 원본 쿼리.
        conversation_history: 이전 대화 이력 (multi-turn 문맥용).

    Returns:
        (intent, confidence) 튜플.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI

    from src.config import get_settings  # pyright: ignore[reportMissingImports]
    from src.utils.llm_parsing import parse_llm_json  # pyright: ignore[reportMissingImports]
    from src.utils.resilience import retry_call  # pyright: ignore[reportMissingImports]

    settings = get_settings()
    if not settings.gemini_llm_api_key:
        logger.warning("classify_intent: GEMINI_LLM_API_KEY 미설정 → GENERAL fallback")
        return (_GENERAL_FALLBACK, 0.0)

    try:
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=settings.gemini_llm_api_key,
            temperature=0,
        )

        messages: list[tuple[str, str]] = [("system", _CLASSIFY_SYSTEM_PROMPT)]

        # 대화 이력 포함 (최근 5턴)
        if conversation_history:
            for msg in conversation_history[-5:]:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                lc_role = "human" if role == "user" else "ai"
                messages.append((lc_role, content))

        messages.append(("human", query))

        response = await retry_call(lambda: llm.ainvoke(messages), attempts=3)
        text = str(response.content).strip()

        # JSON 파싱 (parse_llm_json: 코드펜스 제거 + json.loads)
        result = parse_llm_json(text)
        intent_str = result.get("intent", "GENERAL")
        confidence = float(result.get("confidence", 0.0))

        # Phase 2 intent → GENERAL fallback
        try:
            intent = IntentType(intent_str)
        except ValueError:
            logger.warning("classify_intent: 알 수 없는 intent=%s → GENERAL", intent_str)
            return (_GENERAL_FALLBACK, 0.0)

        if intent not in PHASE1_INTENTS:
            logger.info("classify_intent: Phase 2 intent=%s → GENERAL", intent.value)
            return (_GENERAL_FALLBACK, confidence)

        # 라우팅 맵에 없는 intent는 GENERAL fallback (노드 미구현)
        if intent not in _ROUTABLE_INTENTS:
            logger.info("classify_intent: 라우팅 불가 intent=%s → GENERAL", intent.value)
            return (_GENERAL_FALLBACK, confidence)

        return (intent, confidence)

    except Exception:
        logger.exception("classify_intent failed → GENERAL fallback")
        return (_GENERAL_FALLBACK, 0.0)


async def intent_router_node(state: dict[str, Any]) -> dict[str, Any]:
    """LangGraph 노드 함수 — intent 분류 결과를 state에 기록.

    multi-intent 모드에서는 SSE 핸들러가 intent를 미리 주입하므로
    classify 호출을 스킵하고 intent 블록만 emit.

    Args:
        state: AgentState (TypedDict).

    Returns:
        {"intent": str, "response_blocks": [IntentBlock dict]}.
    """
    pre_injected = state.get("intent")
    if pre_injected:
        # SSE 핸들러가 intent를 주입한 경우 — classify 스킵
        return {
            "response_blocks": [
                {
                    "type": "intent",
                    "intent": pre_injected,
                    "confidence": 1.0,
                }
            ]
        }

    query = state.get("query", "")
    history = state.get("conversation_history")

    intent, confidence = await classify_intent(query, history)

    return {
        "intent": intent.value,
        "response_blocks": [
            {
                "type": "intent",
                "intent": intent.value,
                "confidence": confidence,
            }
        ],
    }
