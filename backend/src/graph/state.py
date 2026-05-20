"""LangGraph AgentState 정의.

모든 그래프 노드가 공유하는 상태 스키마.
response_blocks는 Annotated[list, operator.add]로 각 노드가 append.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Optional

from typing_extensions import TypedDict


class AgentState(TypedDict, total=False):
    """LangGraph 그래프 전체가 공유하는 상태.

    Fields:
        query: 사용자 원본 쿼리 텍스트.
        intent: classify_intent 결과 (IntentType enum value).
        processed_query: 공통 쿼리 전처리 결과 (카테고리, 지역, 키워드 등).
        response_blocks: SSE로 전송할 블록 목록 (operator.add로 누적).
        thread_id: 대화 스레드 ID.
        user_id: 인증된 사용자 ID (BIGINT). 미인증 시 None.
        error: 에러 발생 시 메시지.
        conversation_history: LangGraph checkpoint에서 관리하는 대화 이력 (불변식 #14).
    """

    query: str
    intent: Optional[str]
    processed_query: Optional[dict[str, Any]]
    response_blocks: Annotated[list[dict[str, Any]], operator.add]
    thread_id: str
    user_id: Optional[int]
    error: Optional[str]
    conversation_history: list[dict[str, str]]
    previous_blocks: Optional[list[dict[str, Any]]]
    refinement: Optional[dict[str, Any]]
    original_intent: Optional[str]
