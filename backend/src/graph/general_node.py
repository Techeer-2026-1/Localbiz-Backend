"""GENERAL intent 노드 — 일반 대화 Gemini 스트리밍.

사용자 쿼리에 대해 text_stream 블록을 response_blocks에 추가.
sse.py가 이 블록을 받아서 Gemini astream()으로 토큰 단위 스트리밍.
"""

from __future__ import annotations

import logging
from typing import Any

from src.graph._tracing import traced_node  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

# 시스템 프롬프트 — GENERAL intent 전용
_SYSTEM_PROMPT = (
    "당신은 서울 로컬 라이프 AI 챗봇 'AnyWay'입니다. "
    "자기소개나 인사로 시작하지 말고 바로 본론으로 답변하세요. "
    "서울의 맛집, 카페, 문화행사, 코스 추천 등 로컬 정보에 특화되어 있습니다. "
    "친절하고 자연스럽게 한국어로 대화하세요. "
    "장소/행사 검색이나 추천이 필요한 질문이면 해당 기능을 안내하세요.\n\n"
    "## 응답 형식 규칙\n"
    "- 주제가 바뀔 때 빈 줄로 단락을 구분하세요.\n"
    "- 여러 항목을 나열할 때 번호 목록(1. 2. 3.)을 사용하세요.\n"
    "- 핵심 정보는 **굵게** 강조하세요.\n"
    "- 긴 답변은 ## 소제목으로 구조화하세요."
)


@traced_node("general")
async def general_node(state: dict[str, Any]) -> dict[str, Any]:
    """GENERAL intent 노드 — text_stream 블록 생성.

    text_stream 블록에 system/prompt를 담으면 sse.py에서
    Gemini astream()으로 토큰 단위 스트리밍 처리.

    Args:
        state: AgentState dict.

    Returns:
        {"response_blocks": [text_stream_block]}.
    """
    query = state.get("query", "")
    history = state.get("conversation_history", [])

    # 대화 이력을 프롬프트에 포함
    prompt_parts: list[str] = []
    for msg in history[-10:]:  # 최근 10턴만
        role = msg.get("role", "user")
        content = msg.get("content", "")
        prompt_parts.append(f"{role}: {content}")
    prompt_parts.append(f"user: {query}")

    prompt = "\n".join(prompt_parts) if len(prompt_parts) > 1 else query

    logger.info("general_node: query=%s", query[:100])

    return {
        "response_blocks": [
            {
                "type": "text_stream",
                "system": _SYSTEM_PROMPT,
                "prompt": prompt,
            }
        ],
    }
