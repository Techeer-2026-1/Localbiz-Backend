"""REFINE 노드 — 이전 응답 수정 요청 처리.

수정 흐름:
  1. DB에서 이전 assistant 응답 블록 로드
  2. 수정 대상 특정 (기본 직전, 명시적 참조 시 Gemini 추론)
  3. Gemini JSON mode로 수정 지시 파싱 → RefinementInstruction
  4. state에 previous_blocks + refinement 주입
  5. 원본 노드 함수 직접 호출

수정 유형 5종:
  - replace: 특정 항목 교체
  - remove: 특정 항목 삭제
  - add: 항목 추가
  - change_condition: 조건 변경 후 전체 재검색
  - regenerate: 동일 조건 전체 재생성

불변식 #8: asyncpg $1,$2 바인딩
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from pydantic import BaseModel

from src.graph._tracing import traced_node  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RefinementInstruction 스키마
# ---------------------------------------------------------------------------
class RefinementInstruction(BaseModel):
    """Gemini가 파싱한 수정 지시."""

    action: str = "regenerate"  # replace | remove | add | change_condition | regenerate
    target_index: Optional[int] = None  # 1-indexed
    target_id: Optional[str] = None  # place_id / event_id
    new_condition: Optional[str] = None  # 교체/추가/조건변경 시 키워드
    full_instruction: str = ""  # 원본 수정 요청 텍스트


_VALID_ACTIONS = frozenset({"replace", "remove", "add", "change_condition", "regenerate"})

# P1-3: 정규식 prefilter — 명확 수정 지시는 LLM 호출 skip
_REGEX_REMOVE = re.compile(r"(?P<idx>[1-9]\d?)\s*번\s*(빼|삭제|제거|지워)")
_REGEX_REPLACE = re.compile(r"(?P<idx>[1-9]\d?)\s*번\s*(바꿔|교체|변경)")
_REGEX_ADD = re.compile(r"(하나\s*더|한\s*개\s*더|추가해)")
_REGEX_REGENERATE = re.compile(r"(다시\s*(해|만들)|마음에\s*안|별로|싫어|새로)")


def _try_regex_parse(query: str) -> Optional[RefinementInstruction]:
    """명확 수정 패턴을 정규식으로 직접 파싱. 매치 안 되면 None → LLM 위임."""
    m = _REGEX_REMOVE.search(query)
    if m:
        return RefinementInstruction(
            action="remove",
            target_index=int(m.group("idx")),
            full_instruction=query,
        )

    m = _REGEX_REPLACE.search(query)
    if m:
        return RefinementInstruction(
            action="replace",
            target_index=int(m.group("idx")),
            full_instruction=query,
        )

    if _REGEX_ADD.search(query):
        return RefinementInstruction(action="add", full_instruction=query)

    if _REGEX_REGENERATE.search(query):
        return RefinementInstruction(action="regenerate", full_instruction=query)

    return None


# 구조화 블록 → 원본 intent 매핑
_BLOCK_TYPE_TO_INTENT: dict[str, str] = {
    "places": "PLACE_SEARCH",
    "events": "EVENT_SEARCH",
    "course": "COURSE_PLAN",
    "chart": "REVIEW_COMPARE",
}


# ---------------------------------------------------------------------------
# Gemini 수정 지시 파싱 프롬프트
# ---------------------------------------------------------------------------
_PARSE_REFINEMENT_PROMPT = """\
You are a refinement parser for a Seoul local-life AI chatbot.

The user wants to modify a previous response. Analyze the user's request and the previous response blocks.

Respond in JSON with these fields:
- "action": one of "replace", "remove", "add", "change_condition", "regenerate"
  - "replace": swap a specific item for something else
  - "remove": delete a specific item
  - "add": add a new item to the list
  - "change_condition": change the overall search criteria (e.g., different area or category)
  - "regenerate": redo the entire response from scratch
- "target_index": 1-indexed position of the item to modify (null if not applicable)
- "target_id": the ID of the item to modify if identifiable (null if not applicable)
- "new_condition": the new search keyword or condition for replace/add/change_condition (null if not applicable)
- "full_instruction": the user's original modification request as-is

Rules:
- "3번 바꿔줘" with a condition → action=replace, target_index=3
- "2번 빼줘" → action=remove, target_index=2
- "하나 더 추가해줘" → action=add
- "강남 말고 홍대로" → action=change_condition, new_condition includes "홍대"
- "다시 해줘", "마음에 안 들어" → action=regenerate
- If ambiguous, default to regenerate.

Always respond with valid JSON only. No markdown, no explanation.
"""

_IDENTIFY_TARGET_PROMPT = """\
You are a response identifier for a Seoul local-life AI chatbot.

The user is referring to a previous response in the conversation. Given the conversation history and the user's current query, identify WHICH previous assistant response the user wants to modify.

Previous assistant responses (most recent first):
{responses_summary}

User query: {query}

Respond in JSON:
- "target_message_index": 0-indexed position in the responses list above (0 = most recent)
- "reasoning": brief explanation

If the user is clearly referring to the most recent response, return target_message_index=0.
Always respond with valid JSON only.
"""


# ---------------------------------------------------------------------------
# DB 헬퍼
# ---------------------------------------------------------------------------
async def _load_previous_assistant_blocks(
    thread_id: str,
    limit: int = 5,
) -> list[list[dict[str, Any]]]:
    """최근 assistant 응답의 블록 목록을 로드. 최신순.

    Returns:
        [[blocks of msg1], [blocks of msg2], ...] 최신순. 실패 시 [].
    """
    try:
        from src.db.postgres import get_pool  # pyright: ignore[reportMissingImports]

        pool = get_pool()
        rows = await pool.fetch(
            "SELECT blocks FROM messages WHERE thread_id = $1 AND role = 'assistant' ORDER BY message_id DESC LIMIT $2",
            thread_id,
            limit,
        )
    except Exception:
        logger.exception("refine_node: 이전 응답 로드 실패 thread_id=%s", thread_id)
        return []

    result: list[list[dict[str, Any]]] = []
    for row in rows:
        blocks = row["blocks"]
        if isinstance(blocks, str):
            try:
                blocks = json.loads(blocks)
            except Exception:
                continue
        if isinstance(blocks, list):
            result.append(blocks)
    return result


def _detect_original_intent(blocks: list[dict[str, Any]]) -> Optional[str]:
    """블록 목록에서 원본 intent를 추론.

    intent 블록이 있으면 그 값 사용, 없으면 구조화 블록 타입으로 추론.
    """
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "intent":
            intent_val = block.get("intent")
            # REFINE 자체는 원본 intent가 아님 — 구조화 블록으로 재추론
            if intent_val and intent_val != "REFINE":
                return intent_val

    # intent 블록 없으면 구조화 블록으로 추론
    has_references = any(isinstance(b, dict) and b.get("type") == "references" for b in blocks)
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "places" and has_references:
            return "PLACE_RECOMMEND"
        if btype in _BLOCK_TYPE_TO_INTENT:
            return _BLOCK_TYPE_TO_INTENT[btype]

    return None


def _has_explicit_reference(query: str) -> bool:
    """쿼리에 원거리 참조 키워드가 있는지 간단 체크."""
    ref_keywords = ["아까", "이전", "전에", "처음", "첫 번째", "그때", "아까 그"]
    return any(kw in query for kw in ref_keywords)


# ---------------------------------------------------------------------------
# 수정 대상 특정
# ---------------------------------------------------------------------------
def _filter_refineable(
    all_assistant_blocks: list[list[dict[str, Any]]],
) -> tuple[list[list[dict[str, Any]]], list[Optional[str]]]:
    """REFINE 자기 응답(target 후보로 부적격)을 걸러내고, 각 블록의 원본 intent도 함께 반환.

    REFINE 노드가 "이전 응답이 없어요" / 실패 안내로 응답한 경우, 그 응답을 다음 turn의
    수정 대상으로 잡으면 self-loop가 되며 _detect_original_intent도 None을 반환한다.
    원본 intent가 None이거나 'REFINE'인 응답은 후보에서 제외.

    Returns:
        (refineable_blocks, refineable_intents) — 같은 길이, 최신순 유지.
    """
    refineable_blocks: list[list[dict[str, Any]]] = []
    refineable_intents: list[Optional[str]] = []
    for blocks in all_assistant_blocks:
        intent = _detect_original_intent(blocks)
        if intent and intent != "REFINE":
            refineable_blocks.append(blocks)
            refineable_intents.append(intent)
    return refineable_blocks, refineable_intents


async def _identify_target(
    query: str,
    all_assistant_blocks: list[list[dict[str, Any]]],
    conversation_history: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """수정 대상 응답 블록과 원본 intent를 특정.

    REFINE 자기 응답은 후보에서 제외 후 최신 refineable 응답을 기본으로 잡는다.
    명시적 참조(아까/이전/그때 등)가 있을 때만 Gemini로 더 거슬러 올라가 추론.

    Returns:
        (target_blocks, original_intent). 후보가 0건이거나 실패 시 ([], None).
    """
    if not all_assistant_blocks:
        return [], None

    refineable_blocks, refineable_intents = _filter_refineable(all_assistant_blocks)
    if not refineable_blocks:
        logger.info(
            "refine_node._identify_target: refineable 응답 0건 — 전체 %d건 모두 REFINE 자기응답 또는 intent 추론 실패",
            len(all_assistant_blocks),
        )
        return [], None

    # 기본: 가장 최근 refineable 응답
    if not _has_explicit_reference(query) or len(refineable_blocks) == 1:
        return refineable_blocks[0], refineable_intents[0]

    # 명시적 참조 → Gemini 추론
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI  # pyright: ignore[reportMissingImports]

        from src.config import get_settings  # pyright: ignore[reportMissingImports]

        settings = get_settings()
        if not settings.gemini_llm_api_key:
            return refineable_blocks[0], refineable_intents[0]

        # 응답 요약 생성 — refineable 후보만 (전체 인덱스가 아닌 refineable 인덱스 기준)
        summaries: list[str] = []
        for i, blocks in enumerate(refineable_blocks):
            intent = refineable_intents[i]
            block_types = [b.get("type", "") for b in blocks if isinstance(b, dict)]
            summaries.append(f"[{i}] intent={intent}, blocks={block_types}")

        prompt = _IDENTIFY_TARGET_PROMPT.format(
            responses_summary="\n".join(summaries),
            query=query,
        )

        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=settings.gemini_llm_api_key,
            temperature=0,
        )
        response = await llm.ainvoke([("system", prompt)])
        text = str(response.content).strip()

        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)
        idx = int(result.get("target_message_index", 0))
        idx = max(0, min(idx, len(refineable_blocks) - 1))

        return refineable_blocks[idx], refineable_intents[idx]

    except Exception:
        logger.exception("refine_node: target 추론 실패 → 직전 refineable 응답 fallback")
        return refineable_blocks[0], refineable_intents[0]


# ---------------------------------------------------------------------------
# 수정 지시 파싱
# ---------------------------------------------------------------------------
async def _parse_refinement(
    query: str,
    target_blocks: list[dict[str, Any]],
) -> RefinementInstruction:
    """Gemini JSON mode로 수정 지시 파싱.

    P1-3: 명확 패턴은 정규식으로 직접 파싱 (LLM 호출 skip).
    실패 시 regenerate fallback.
    """
    # P1-3: 정규식 prefilter
    regex_result = _try_regex_parse(query)
    if regex_result is not None:
        logger.info(
            "refine_node: regex parsed action=%s target_index=%s (LLM skip)",
            regex_result.action,
            regex_result.target_index,
        )
        return regex_result

    try:
        from langchain_google_genai import ChatGoogleGenerativeAI  # pyright: ignore[reportMissingImports]

        from src.config import get_settings  # pyright: ignore[reportMissingImports]

        settings = get_settings()
        if not settings.gemini_llm_api_key:
            return RefinementInstruction(action="regenerate", full_instruction=query)

        # 타겟 블록 요약 (토큰 절약)
        block_summary_parts: list[str] = []
        for block in target_blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "places":
                items = block.get("items", [])
                for i, item in enumerate(items, 1):
                    if isinstance(item, dict):
                        block_summary_parts.append(
                            f"  {i}. {item.get('name', '')} (place_id={item.get('place_id', '')})"
                        )
            elif btype == "events":
                items = block.get("items", [])
                for i, item in enumerate(items, 1):
                    if isinstance(item, dict):
                        block_summary_parts.append(
                            f"  {i}. {item.get('title', '')} (event_id={item.get('event_id', '')})"
                        )
            elif btype == "course":
                stops = block.get("stops", [])
                for stop in stops:
                    if isinstance(stop, dict):
                        order = stop.get("order", "?")
                        place = stop.get("place", {})
                        name = place.get("name", "") if isinstance(place, dict) else ""
                        pid = place.get("place_id", "") if isinstance(place, dict) else ""
                        block_summary_parts.append(f"  {order}. {name} (place_id={pid})")
            elif btype == "chart":
                places = block.get("places", [])
                for i, p in enumerate(places, 1):
                    if isinstance(p, dict):
                        block_summary_parts.append(f"  {i}. {p.get('name', '')}")

        block_summary = "\n".join(block_summary_parts) if block_summary_parts else "(구조화 블록 없음)"
        user_prompt = f"이전 응답 항목:\n{block_summary}\n\n사용자 수정 요청: {query}"

        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=settings.gemini_llm_api_key,
            temperature=0,
        )
        response = await llm.ainvoke(
            [
                ("system", _PARSE_REFINEMENT_PROMPT),
                ("human", user_prompt),
            ]
        )
        text = str(response.content).strip()

        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)

        action = result.get("action", "regenerate")
        if action not in _VALID_ACTIONS:
            action = "regenerate"

        return RefinementInstruction(
            action=action,
            target_index=result.get("target_index"),
            target_id=result.get("target_id"),
            new_condition=result.get("new_condition"),
            full_instruction=query,
        )

    except Exception:
        logger.exception("refine_node: 수정 지시 파싱 실패 → regenerate fallback")
        return RefinementInstruction(action="regenerate", full_instruction=query)


# ---------------------------------------------------------------------------
# 원본 노드 함수 매핑
# ---------------------------------------------------------------------------
def _get_node_function(original_intent: Optional[str]) -> Any:
    """원본 intent에 해당하는 노드 함수 반환. 없으면 None."""
    from src.graph.cost_estimate_node import cost_estimate_node  # pyright: ignore[reportMissingImports]
    from src.graph.course_plan_node import course_plan_node  # pyright: ignore[reportMissingImports]
    from src.graph.event_recommend_node import event_recommend_node  # pyright: ignore[reportMissingImports]
    from src.graph.event_search_node import event_search_node  # pyright: ignore[reportMissingImports]
    from src.graph.place_recommend_node import place_recommend_node  # pyright: ignore[reportMissingImports]
    from src.graph.place_search_node import place_search_node  # pyright: ignore[reportMissingImports]
    from src.graph.review_compare_node import review_compare_node  # pyright: ignore[reportMissingImports]

    mapping: dict[str, Any] = {
        "PLACE_SEARCH": place_search_node,
        "PLACE_RECOMMEND": place_recommend_node,
        "EVENT_SEARCH": event_search_node,
        "EVENT_RECOMMEND": event_recommend_node,
        "COURSE_PLAN": course_plan_node,
        "REVIEW_COMPARE": review_compare_node,
        "COST_ESTIMATE": cost_estimate_node,
    }
    return mapping.get(str(original_intent))


# ---------------------------------------------------------------------------
# 안내 메시지 블록 생성
# ---------------------------------------------------------------------------
def _no_previous_response_blocks() -> list[dict[str, Any]]:
    """이전 응답이 없을 때 안내 텍스트 블록."""
    return [
        {
            "type": "intent",
            "intent": "REFINE",
            "confidence": 1.0,
        },
        {
            "type": "text_stream",
            "system": (
                "당신은 서울 로컬 라이프 AI 챗봇 'AnyWay'입니다. "
                "자기소개나 인사로 시작하지 말고 바로 본론으로 답변하세요."
            ),
            "prompt": (
                "사용자가 이전 응답을 수정하려 했지만, 수정할 이전 응답이 없습니다. "
                "장소 검색, 코스 추천 등을 먼저 요청하면 결과를 수정할 수 있다고 안내해주세요. "
                "1-2문장으로 간결하게."
            ),
        },
    ]


# ---------------------------------------------------------------------------
# LangGraph 노드
# ---------------------------------------------------------------------------
@traced_node("refine")
async def refine_node(state: dict[str, Any]) -> dict[str, Any]:
    """REFINE 노드 — 수정 요청 파싱 + 원본 노드 재호출.

    Args:
        state: AgentState dict.

    Returns:
        {"response_blocks": [...]} — 원본 노드의 수정된 결과.
    """
    thread_id: Optional[str] = state.get("thread_id")
    query = state.get("query", "")
    conversation_history = state.get("conversation_history", [])

    logger.info("refine_node: thread_id=%s, query_len=%d", thread_id, len(query))

    if not thread_id:
        return {"response_blocks": _no_previous_response_blocks()}

    # 1. 이전 응답 블록 로드
    all_blocks = await _load_previous_assistant_blocks(thread_id)
    if not all_blocks:
        logger.info(
            "refine_node: 이전 assistant 응답 0건 thread_id=%s → 안내 메시지",
            thread_id,
        )
        return {"response_blocks": _no_previous_response_blocks()}

    # 2. 수정 대상 특정
    target_blocks, original_intent = await _identify_target(query, all_blocks, conversation_history)

    if not target_blocks or not original_intent:
        logger.info(
            "refine_node: target 추정 실패 thread_id=%s, all_blocks_count=%d, "
            "target_blocks_empty=%s, original_intent=%s → 안내 메시지",
            thread_id,
            len(all_blocks),
            not target_blocks,
            original_intent,
        )
        return {"response_blocks": _no_previous_response_blocks()}

    # 3. 수정 지시 파싱
    refinement = await _parse_refinement(query, target_blocks)

    logger.info(
        "refine_node: original_intent=%s, action=%s, target_index=%s",
        original_intent,
        refinement.action,
        refinement.target_index,
    )

    # 4. 원본 노드 함수 가져오기
    node_fn = _get_node_function(original_intent)
    if node_fn is None:
        logger.warning("refine_node: 원본 노드 함수 없음 intent=%s → 안내 메시지", original_intent)
        return {"response_blocks": _no_previous_response_blocks()}

    # 5. state 복사 + refinement 컨텍스트 주입 → 원본 노드 호출
    refined_state = dict(state)
    refined_state["previous_blocks"] = target_blocks
    refined_state["refinement"] = refinement.model_dump()
    refined_state["original_intent"] = original_intent
    refined_state["intent"] = original_intent
    refined_state["_refine_depth"] = state.get("_refine_depth", 0) + 1

    try:
        result = await node_fn(refined_state)
        return result
    except Exception:
        logger.exception("refine_node: 원본 노드 재호출 실패 intent=%s → regenerate", original_intent)
        # fallback: refinement 없이 원본 노드 그대로 실행
        fallback_state = dict(state)
        fallback_state["intent"] = original_intent
        try:
            return await node_fn(fallback_state)
        except Exception:
            logger.exception("refine_node: fallback도 실패")
            return {"response_blocks": _no_previous_response_blocks()}
