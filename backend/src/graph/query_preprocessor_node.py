"""공통 쿼리 전처리 노드 — 불변식 #12 구현.

Intent Router 직후, 모든 검색 기능 공통으로 실행.
Gemini 2.5 Flash JSON mode로 카테고리/지역/키워드 추출.

GENERAL intent는 전처리 불필요 → 빈 dict 반환.
Gemini 실패 시 빈 dict fallback (검색 노드가 원본 query로 동작).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ISO date "YYYY-MM-DD" 형식 검증 정규식 (Gemini 응답 후처리)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 전처리 생략 대상 intent — GENERAL(대화형) + IMAGE_SEARCH(URL 쿼리, 이미지 노드가 직접 파싱)
_SKIP_INTENTS: frozenset[str] = frozenset({"GENERAL", "IMAGE_SEARCH"})

_PREPROCESS_SYSTEM_PROMPT = """\
You are a query preprocessor for a Seoul local-life AI chatbot.
Extract structured information from the user's query in Korean.
If conversation history is provided, use it to resolve references like "여기", "거기", "그곳" and to extract missing information (e.g. dates mentioned in a previous turn).

Return a JSON object with these fields:
- "original_query": the original user query (string)
- "expanded_query": expanded/clarified version of the query in Korean (string)
  Examples: "카공" → "콘센트가 있는 카페", "이번 주말 전시" → "서울 무료 전시 2026년"
- "district": Seoul administrative district if mentioned, null otherwise (string or null)
  Examples: "마포구", "강남구", "종로구"
- "neighborhood": neighborhood or area name if mentioned, null otherwise (string or null)
  Examples: "홍대", "이태원", "성수동", "강남역"
- "category": place/event category if mentioned, null otherwise (string or null)
  Examples: "카페", "음식점", "전시회", "축제", "맛집", "숙박", "호텔", "모텔"
- "keywords": list of key descriptive words (array of strings)
  Examples: ["분위기 좋은", "조용한"], ["가성비"], ["데이트"]
- "date_reference": date reference if mentioned, null otherwise (string or null)
  Examples: "이번 주말", "토요일", "내일", "3월"
- "date_start_resolved": resolved ISO date "YYYY-MM-DD" or null
  Convert date_reference to absolute date based on today={today}.
  Examples (today=2026-05-14 목요일):
    "이번 주말" → start="2026-05-16" (토)
    "토요일" → start="2026-05-16"
    "내일" → start="2026-05-15"
    "3월" → start="2026-03-01"
    "다음 주" → start="2026-05-18" (월)
  If date_reference is ambiguous or missing, return null.
- "date_end_resolved": resolved ISO date "YYYY-MM-DD" or null
  End date of the range (inclusive). For single-day references, same as start.
  Examples (today=2026-05-14 목요일):
    "이번 주말" → end="2026-05-17" (일)
    "토요일" → end="2026-05-16"
    "내일" → end="2026-05-15"
    "3월" → end="2026-03-31"
    "다음 주" → end="2026-05-24" (일)
  If date_reference is ambiguous or missing, return null.
- "time_reference": time reference if mentioned, null otherwise (string or null)
  Examples: "2시", "저녁", "오후"
- "place_name": specific place name if mentioned in current query or conversation history, null otherwise (string or null)
  Examples: "스타벅스 강남점", "롯데호텔 서울", "한강공원", "A모텔"
- "check_in": check-in date for accommodation if mentioned in current query or conversation history, null otherwise (string or null)
  Format: "YYYY-MM-DD" if exact date, otherwise the raw expression. Examples: "2026-05-10", "5월 10일"
- "check_out": check-out date for accommodation if mentioned in current query or conversation history, null otherwise (string or null)
  Format: "YYYY-MM-DD" if exact date, otherwise the raw expression. Examples: "2026-05-12", "5월 12일"

Always respond with valid JSON only. No markdown, no explanation.
"""


async def _extract_query_fields(
    query: str,
    intent: Optional[str] = None,
    conversation_history: Optional[list[dict[str, str]]] = None,
    current_date: Optional[str] = None,
) -> dict[str, Any]:
    """Gemini JSON mode로 쿼리에서 공통 필드 추출.

    Args:
        query: 사용자 원본 쿼리.
        intent: 분류된 intent (GENERAL이면 생략).
        conversation_history: 이전 대화 이력 (place_name/날짜 등 문맥 파싱용).
        current_date: 오늘 날짜 (ISO "YYYY-MM-DD"). None이면 date.today() 사용.
            date_reference → date_start/end_resolved 변환 기준. 테스트 결정성 위해 주입 가능.

    Returns:
        공통 필드 dict. 실패 시 빈 dict.
    """
    if intent in _SKIP_INTENTS:
        return {}

    today_str = current_date if current_date else date.today().isoformat()

    from langchain_google_genai import ChatGoogleGenerativeAI  # pyright: ignore[reportMissingImports]

    from src.config import get_settings  # pyright: ignore[reportMissingImports]

    settings = get_settings()
    if not settings.gemini_llm_api_key:
        logger.warning("query_preprocessor: GEMINI_LLM_API_KEY 미설정 → 빈 dict")
        return {}

    try:
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=settings.gemini_llm_api_key,
            temperature=0,
        )

        system_prompt = _PREPROCESS_SYSTEM_PROMPT.replace("{today}", today_str)
        messages: list[tuple[str, str]] = [
            ("system", system_prompt),
        ]

        # 최근 5턴 대화 이력 포함 — place_name/날짜 문맥 파싱에 활용
        if conversation_history:
            for msg in conversation_history[-5:]:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                lc_role = "human" if role == "user" else "ai"
                messages.append((lc_role, content))

        messages.append(("human", query))

        response = await llm.ainvoke(messages)
        text = str(response.content).strip()

        # Gemini가 ```json ... ``` 래핑할 수 있음
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)

        # 필수 필드 보장
        if not isinstance(result, dict):
            logger.warning("query_preprocessor: Gemini 응답이 dict 아님 → 빈 dict")
            return {}

        # original_query 보장
        result.setdefault("original_query", query)
        result.setdefault("expanded_query", query)
        result.setdefault("keywords", [])

        # date_*_resolved 보장 + ISO "YYYY-MM-DD" 형식 검증 (Gemini 응답 후처리)
        # Gemini가 잘못된 형식("2026/05/16", "다음 주" 등) 반환 시 None으로 정정
        result.setdefault("date_start_resolved", None)
        result.setdefault("date_end_resolved", None)
        for key in ("date_start_resolved", "date_end_resolved"):
            val = result.get(key)
            if val is not None and (not isinstance(val, str) or not _ISO_DATE_RE.match(val)):
                result[key] = None

        # PII 유출 방지를 위해 결과의 키 목록과 intent만 로깅
        logger.info("query_preprocessor: intent=%s, extracted_keys=%s", intent, list(result.keys()))
        return result

    except Exception:
        logger.exception("query_preprocessor failed → 빈 dict fallback")
        return {}


async def _load_history_from_db(thread_id: str) -> list[dict[str, str]]:
    """messages 테이블에서 최근 대화 이력을 직접 조회해 conversation_history 포맷으로 반환.

    sse.py가 conversation_history를 빈 배열로 넘겨도 이 함수로 자체 조회.
    최근 5턴(10메시지) 기준. 불변식 #3 준수 (SELECT만).

    Returns:
        [{"role": "user"/"assistant", "content": "..."}] 리스트. 실패 시 [].
    """
    try:
        from src.db.postgres import get_pool  # pyright: ignore[reportMissingImports]

        pool = get_pool()
        rows = await pool.fetch(
            """
            SELECT role, blocks FROM messages
            WHERE thread_id = $1
            ORDER BY message_id DESC
            LIMIT 10
            """,
            thread_id,
        )
    except Exception:
        logger.warning("query_preprocessor: DB 이력 조회 실패 → 빈 이력으로 진행")
        return []

    history: list[dict[str, str]] = []
    for row in reversed(rows):  # 오래된 순으로 정렬
        role: str = row["role"]
        blocks = row["blocks"]
        if isinstance(blocks, str):
            try:
                blocks = json.loads(blocks)
            except Exception:
                continue

        # blocks에서 텍스트 + 구조화된 엔티티 이름 추출
        # user: type=="text" 블록의 content
        # assistant: text_stream content + places/events/course 블록의 엔티티 이름
        text_parts: list[str] = []
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text" and role == "user":
                text_parts.append(block.get("content", ""))
            elif btype == "text_stream" and role == "assistant":
                text_parts.append(block.get("content", ""))
            elif btype == "place" and role == "assistant":
                name = block.get("name", "")
                if name:
                    text_parts.append(f"[장소: {name}]")
            elif btype == "places" and role == "assistant":
                for item in block.get("items", []):
                    name = item.get("name", "") if isinstance(item, dict) else ""
                    if name:
                        text_parts.append(f"[장소: {name}]")
            elif btype == "events" and role == "assistant":
                for item in block.get("items", []):
                    title = item.get("title", "") if isinstance(item, dict) else ""
                    if title:
                        text_parts.append(f"[행사: {title}]")
            elif btype == "course" and role == "assistant":
                for stop in block.get("stops", []):
                    if isinstance(stop, dict):
                        place = stop.get("place", {})
                        name = place.get("name", "") if isinstance(place, dict) else ""
                        if name:
                            text_parts.append(f"[코스 장소: {name}]")

        content = " ".join(t for t in text_parts if t).strip()
        if content:
            history.append({"role": role, "content": content})

    return history


async def query_preprocessor_node(state: dict[str, Any]) -> dict[str, Any]:
    """LangGraph 공통 쿼리 전처리 노드 (불변식 #12).

    Args:
        state: AgentState dict.

    Returns:
        {"processed_query": dict} — 추출된 공통 필드 또는 빈 dict.
    """
    query = state.get("query", "")
    intent = state.get("intent")
    thread_id: Optional[str] = state.get("thread_id")

    # sse.py가 conversation_history를 채워줬으면 그걸 쓰고,
    # 비어 있으면 messages 테이블에서 직접 조회
    conversation_history: Optional[list[dict[str, str]]] = state.get("conversation_history")
    if not conversation_history and thread_id:
        conversation_history = await _load_history_from_db(thread_id)

    processed = await _extract_query_fields(query, intent, conversation_history)

    # neighborhood → district 자동 추론 (Gemini가 district 미추출 시 geo_mapping 폴백)
    neighborhood = processed.get("neighborhood")
    if isinstance(neighborhood, str) and neighborhood and not processed.get("district"):
        from src.utils.geo_mapping import resolve_district

        inferred = resolve_district(neighborhood)
        if inferred:
            processed["district"] = inferred
            logger.info(
                "query_preprocessor: inferred district=%s from neighborhood=%s", inferred, processed["neighborhood"]
            )

    return {"processed_query": processed}
