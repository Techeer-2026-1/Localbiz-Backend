"""LangGraph 그래프 빌더 — 노드/엣지 구성.

StateGraph(AgentState)를 구성하고 compiled graph를 반환.
각 노드의 실제 로직은 별도 *_node.py 파일에서 구현.

그래프 구조 (Phase 1):
  __start__ → intent_router → (조건부 분기) → [각 intent 노드] → response_builder → __end__

노드 추가/변경 시 코드리뷰 체크리스트 확인 필수.
"""

from __future__ import annotations

from typing import Any, Optional

from langgraph.graph import END, StateGraph

from src.graph.analysis_node import analysis_node  # pyright: ignore[reportMissingImports]
from src.graph.booking_node import booking_node  # pyright: ignore[reportMissingImports]
from src.graph.calendar_node import calendar_node  # pyright: ignore[reportMissingImports]
from src.graph.cost_estimate_node import cost_estimate_node  # pyright: ignore[reportMissingImports]
from src.graph.course_plan_node import course_plan_node  # pyright: ignore[reportMissingImports]  # noqa: F401
from src.graph.crowdedness_node import crowdedness_node  # pyright: ignore[reportMissingImports]
from src.graph.detail_inquiry_node import detail_inquiry_node  # pyright: ignore[reportMissingImports]  # noqa: F401
from src.graph.event_recommend_node import event_recommend_node  # pyright: ignore[reportMissingImports]
from src.graph.event_search_node import event_search_node  # pyright: ignore[reportMissingImports]
from src.graph.general_node import general_node  # pyright: ignore[reportMissingImports]
from src.graph.image_search_node import image_search_node  # pyright: ignore[reportMissingImports]
from src.graph.intent_router_node import intent_router_node  # pyright: ignore[reportMissingImports]
from src.graph.place_recommend_node import place_recommend_node  # pyright: ignore[reportMissingImports]  # noqa: F401
from src.graph.place_search_node import place_search_node  # pyright: ignore[reportMissingImports]  # noqa: F401
from src.graph.query_preprocessor_node import (  # pyright: ignore[reportMissingImports]  # noqa: F401
    query_preprocessor_node,
)
from src.graph.refine_node import refine_node  # pyright: ignore[reportMissingImports]
from src.graph.response_builder_node import response_builder_node  # pyright: ignore[reportMissingImports]
from src.graph.review_compare_node import review_compare_node  # pyright: ignore[reportMissingImports]
from src.graph.state import AgentState  # pyright: ignore[reportMissingImports]


# ---------------------------------------------------------------------------
# 조건부 라우팅 함수
# ---------------------------------------------------------------------------
def _route_by_intent(state: AgentState) -> str:
    """intent 값에 따라 다음 노드 결정.

    Returns:
        노드 이름 문자열. intent별 매핑:
        - PLACE_SEARCH → "place_search"
        - PLACE_RECOMMEND → "place_recommend"
        - EVENT_SEARCH → "event_search"
        - EVENT_RECOMMEND → "event_recommend"
        - COURSE_PLAN → "course_plan"
        - DETAIL_INQUIRY → "detail_inquiry"
        - BOOKING → "booking"
        - CALENDAR → "calendar"
        - ANALYSIS → "analysis"
        - COST_ESTIMATE → "cost_estimate"
        - GENERAL (fallback) → "general"
        - Phase 2 intents → "general" (Phase 2에서 확장)
    """
    intent = state.get("intent", "GENERAL")
    mapping: dict[str, str] = {
        "PLACE_SEARCH": "place_search",
        "PLACE_RECOMMEND": "place_recommend",
        "EVENT_SEARCH": "event_search",
        "EVENT_RECOMMEND": "event_recommend",
        "COURSE_PLAN": "course_plan",
        "DETAIL_INQUIRY": "detail_inquiry",
        "BOOKING": "booking",
        "CALENDAR": "calendar",
        "REVIEW_COMPARE": "review_compare",
        "CROWDEDNESS": "crowdedness",
        "IMAGE_SEARCH": "image_search",
        "ANALYSIS": "analysis",
        "COST_ESTIMATE": "cost_estimate",
        "REFINE": "refine",
    }
    return mapping.get(str(intent), "general")


# ---------------------------------------------------------------------------
# 그래프 빌드
# ---------------------------------------------------------------------------
def build_graph(checkpointer: Optional[Any] = None) -> Any:
    """StateGraph(AgentState) 구성 + 컴파일.

    Args:
        checkpointer: PostgresSaver 등 langgraph-checkpoint 인스턴스.

    Returns:
        CompiledGraph. astream() / ainvoke()로 실행 가능.
    """
    from src.config import get_settings  # pyright: ignore[reportMissingImports]

    _use_combined = get_settings().enable_combined_intent_preprocess

    graph = StateGraph(AgentState)

    # 노드 등록 — 모두 실제 구현 (stub 0건)
    if _use_combined:
        # P3-A: 단일 호출 통합 노드 (feature flag)
        from src.graph.combined_intent_preprocess_node import (  # pyright: ignore[reportMissingImports]
            combined_intent_preprocess_node,
        )

        graph.add_node("intent_router", combined_intent_preprocess_node)
    else:
        graph.add_node("intent_router", intent_router_node)
    graph.add_node("query_preprocessor", query_preprocessor_node)
    graph.add_node("place_search", place_search_node)
    graph.add_node("place_recommend", place_recommend_node)
    graph.add_node("event_search", event_search_node)
    graph.add_node("event_recommend", event_recommend_node)
    graph.add_node("course_plan", course_plan_node)
    graph.add_node("general", general_node)
    graph.add_node("detail_inquiry", detail_inquiry_node)
    graph.add_node("booking", booking_node)
    graph.add_node("calendar", calendar_node)
    graph.add_node("review_compare", review_compare_node)
    graph.add_node("crowdedness", crowdedness_node)
    graph.add_node("image_search", image_search_node)
    graph.add_node("analysis", analysis_node)
    graph.add_node("cost_estimate", cost_estimate_node)
    graph.add_node("refine", refine_node)
    graph.add_node("response_builder", response_builder_node)

    # 엣지 설정
    graph.set_entry_point("intent_router")
    graph.add_edge("intent_router", "query_preprocessor")

    # 전처리 후 intent에 따라 분기
    graph.add_conditional_edges(
        "query_preprocessor",
        _route_by_intent,
        {
            "place_search": "place_search",
            "place_recommend": "place_recommend",
            "event_search": "event_search",
            "event_recommend": "event_recommend",
            "course_plan": "course_plan",
            "detail_inquiry": "detail_inquiry",
            "booking": "booking",
            "calendar": "calendar",
            "review_compare": "review_compare",
            "crowdedness": "crowdedness",
            "image_search": "image_search",
            "analysis": "analysis",
            "cost_estimate": "cost_estimate",
            "refine": "refine",
            "general": "general",
        },
    )

    # 모든 intent 노드 → response_builder → END
    for node_name in [
        "place_search",
        "place_recommend",
        "event_search",
        "event_recommend",
        "course_plan",
        "general",
        "detail_inquiry",
        "booking",
        "calendar",
        "review_compare",
        "crowdedness",
        "image_search",
        "analysis",
        "cost_estimate",
        "refine",
    ]:
        graph.add_edge(node_name, "response_builder")

    graph.add_edge("response_builder", END)

    return graph.compile(checkpointer=checkpointer)
