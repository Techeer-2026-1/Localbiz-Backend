"""response_builder 노드 — done 블록 추가 + intent별 블록 순서 검증.

LangGraph 그래프의 마지막 노드. 모든 intent 노드가 response_blocks에
블록을 누적한 뒤, 이 노드가:
  1. done 블록 추가 (에러 시 status="error")
  2. intent별 블록 순서 검증 (불변식 #11, 기획서 section 4.5)
  3. 검증 실패 시 logger.warning만 (블록 제거하지 않음)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.graph._tracing import traced_node  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# intent별 기대 블록 순서 (기획서 section 4.5)
# ---------------------------------------------------------------------------
# 현재 GENERAL만 확정. 나머지 intent는 각 노드 구현 완료 후 추가.
# TODO: PLACE_SEARCH, PLACE_RECOMMEND, EVENT_SEARCH, EVENT_RECOMMEND,
#       COURSE_PLAN, DETAIL_INQUIRY, BOOKING, CALENDAR
# intent별 기대 블록 순서. 필수 블록만 포함.
# map_markers 등 선택적 블록은 _OPTIONAL_BLOCKS에서 관리.
_EXPECTED_BLOCK_ORDER: dict[str, list[str]] = {
    "GENERAL": ["intent", "text_stream", "done"],
    "PLACE_SEARCH": ["intent", "places", "text_stream", "done"],
    "PLACE_RECOMMEND": ["intent", "places", "text_stream", "done"],
    "EVENT_SEARCH": ["intent", "events", "text_stream", "done"],
    "EVENT_RECOMMEND": ["intent", "events", "text_stream", "done"],
    "COURSE_PLAN": ["intent", "text_stream", "course", "done"],
    "DETAIL_INQUIRY": ["intent", "text_stream", "done"],
    # 기획서 §4.5: chart 누락 silent 회귀 방지 — 신규 ChartBlock 의존 패널 추적용.
    "REVIEW_COMPARE": ["intent", "text_stream", "chart", "analysis_sources", "done"],
}

# 선택적 블록 — 있어도 되고 없어도 되는 블록 (순서 검증에서 제외)
_OPTIONAL_BLOCKS: frozenset[str] = frozenset({"map_markers", "map_route", "place", "references"})


# ---------------------------------------------------------------------------
# 블록 순서 검증 헬퍼
# ---------------------------------------------------------------------------
def _validate_block_order(
    intent: Optional[str],
    block_types: list[str],
) -> Optional[str]:
    """intent별 기대 순서와 실제 블록 type 시퀀스를 비교.

    선택적 블록(map_markers 등)은 제외하고 필수 블록만 검증.

    Args:
        intent: 분류된 intent 문자열. None이면 검증 스킵.
        block_types: response_blocks의 type 시퀀스 (done 포함).

    Returns:
        불일치 시 경고 메시지 문자열, 일치 시 None.
    """
    if intent is None:
        return "intent가 None — 블록 순서 검증 스킵"

    # REFINE은 원본 intent의 블록 순서를 따르므로 검증 스킵
    if intent == "REFINE":
        return None

    expected = _EXPECTED_BLOCK_ORDER.get(intent)
    if expected is None:
        return None

    # 선택적 블록 제외 후 비교
    actual_required = [t for t in block_types if t not in _OPTIONAL_BLOCKS]

    # 빈 결과 정적 응답 — 모든 intent 공통(#151). 향후 PLACE 등 동일 패턴 노드 확장 호환.
    if actual_required == ["intent", "text", "done"]:
        return None

    if actual_required != expected:
        return f"블록 순서 불일치 [{intent}]: expected={expected}, actual={actual_required}"

    return None


# ---------------------------------------------------------------------------
# 노드 함수
# ---------------------------------------------------------------------------
@traced_node("response_builder")
async def response_builder_node(state: dict[str, Any]) -> dict[str, Any]:
    """response_blocks에 done 블록 추가 + 블록 순서 검증.

    Args:
        state: AgentState dict. response_blocks / intent / error 참조.

    Returns:
        {"response_blocks": [done_block]}
        Annotated[list, operator.add]에 의해 기존 블록 뒤에 append.
    """
    error: Optional[str] = state.get("error")

    # 1) done 블록 생성
    if error:
        done_block: dict[str, Any] = {
            "type": "done",
            "status": "error",
            "error_message": error,
        }
    else:
        done_block = {
            "type": "done",
            "status": "done",
        }

    # 2) 블록 순서 검증 (done 포함한 최종 시퀀스)
    existing_blocks: list[dict[str, Any]] = state.get("response_blocks", [])
    block_types = [b.get("type", "") for b in existing_blocks]
    block_types.append("done")  # done은 아직 append 전이므로 수동 추가

    intent: Optional[str] = state.get("intent")
    warning = _validate_block_order(intent, block_types)
    if warning is not None:
        logger.warning("response_builder: %s", warning)

    return {"response_blocks": [done_block]}
