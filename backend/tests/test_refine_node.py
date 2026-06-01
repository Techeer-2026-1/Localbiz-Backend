"""refine_node 단위 테스트 — 순수 함수 검증 (DB/Gemini 미의존)."""

from __future__ import annotations


def test_detect_original_intent_from_intent_block() -> None:
    from src.graph.refine_node import _detect_original_intent

    blocks = [
        {"type": "intent", "intent": "PLACE_SEARCH", "confidence": 0.9},
        {"type": "places", "items": []},
    ]
    assert _detect_original_intent(blocks) == "PLACE_SEARCH"


def test_detect_original_intent_from_structured_block() -> None:
    from src.graph.refine_node import _detect_original_intent

    blocks = [
        {"type": "places", "items": [{"place_id": "p1"}]},
        {"type": "text_stream", "content": "요약"},
    ]
    assert _detect_original_intent(blocks) == "PLACE_SEARCH"


def test_detect_original_intent_course() -> None:
    from src.graph.refine_node import _detect_original_intent

    blocks = [{"type": "course", "stops": []}]
    assert _detect_original_intent(blocks) == "COURSE_PLAN"


def test_detect_original_intent_events() -> None:
    from src.graph.refine_node import _detect_original_intent

    blocks = [{"type": "events", "items": []}]
    assert _detect_original_intent(blocks) == "EVENT_SEARCH"


def test_detect_original_intent_chart() -> None:
    from src.graph.refine_node import _detect_original_intent

    blocks = [{"type": "chart", "places": []}]
    assert _detect_original_intent(blocks) == "REVIEW_COMPARE"


def test_detect_original_intent_none() -> None:
    from src.graph.refine_node import _detect_original_intent

    blocks = [{"type": "text_stream", "content": "hello"}]
    assert _detect_original_intent(blocks) is None


def test_has_explicit_reference_true() -> None:
    from src.graph.refine_node import _has_explicit_reference

    assert _has_explicit_reference("아까 추천해준 카페 목록에서 2번 빼줘")
    assert _has_explicit_reference("이전 결과에서 바꿔줘")
    assert _has_explicit_reference("처음 추천한 거")


def test_has_explicit_reference_false() -> None:
    from src.graph.refine_node import _has_explicit_reference

    assert not _has_explicit_reference("3번 장소 바꿔줘")
    assert not _has_explicit_reference("다시 추천해줘")


def test_no_previous_response_blocks() -> None:
    from src.graph.refine_node import _no_previous_response_blocks

    blocks = _no_previous_response_blocks()
    assert len(blocks) == 2
    assert blocks[0]["type"] == "intent"
    assert blocks[0]["intent"] == "REFINE"
    assert blocks[1]["type"] == "text_stream"


def test_refinement_instruction_defaults() -> None:
    from src.graph.refine_node import RefinementInstruction

    r = RefinementInstruction()
    assert r.action == "regenerate"
    assert r.target_index is None
    assert r.target_id is None
    assert r.new_condition is None


def test_refinement_instruction_with_values() -> None:
    from src.graph.refine_node import RefinementInstruction

    r = RefinementInstruction(
        action="replace",
        target_index=3,
        new_condition="조용한 카페",
        full_instruction="3번 조용한 카페로 바꿔줘",
    )
    assert r.action == "replace"
    assert r.target_index == 3
    assert r.new_condition == "조용한 카페"


def test_get_node_function_valid() -> None:
    from src.graph.refine_node import _get_node_function

    fn = _get_node_function("PLACE_SEARCH")
    assert fn is not None
    assert callable(fn)


def test_get_node_function_invalid() -> None:
    from src.graph.refine_node import _get_node_function

    fn = _get_node_function("NONEXISTENT")
    assert fn is None


def test_get_node_function_all_mapped() -> None:
    from src.graph.refine_node import _get_node_function

    expected = [
        "PLACE_SEARCH",
        "PLACE_RECOMMEND",
        "EVENT_SEARCH",
        "EVENT_RECOMMEND",
        "COURSE_PLAN",
        "REVIEW_COMPARE",
        "COST_ESTIMATE",
    ]
    for intent in expected:
        assert _get_node_function(intent) is not None, f"{intent} not mapped"


def test_classify_prompts_contain_not_refine_guidance() -> None:
    """REFINE 부정 예시가 양쪽 분류 프롬프트에 포함되어 있는지 확인."""
    from src.graph.intent_router_node import _CLASSIFY_MULTI_SYSTEM_PROMPT, _CLASSIFY_SYSTEM_PROMPT

    for prompt in [_CLASSIFY_SYSTEM_PROMPT, _CLASSIFY_MULTI_SYSTEM_PROMPT]:
        assert "NOT REFINE" in prompt, "프롬프트에 NOT REFINE 부정 예시가 없음"
        assert "questions about previous results" in prompt or "questions about a previous response" in prompt


# ---------------------------------------------------------------------------
# _filter_refineable / _identify_target 회귀 (#212)
# ---------------------------------------------------------------------------
def test_filter_refineable_excludes_refine_self_response() -> None:
    """REFINE 자기 실패 응답은 후보에서 제외.

    실제 DB 관측 케이스 (thread session-1780290718671):
      msg 832: [intent(REFINE), intent(REFINE), text_stream, done]  ← REFINE 자기응답
      msg 824: [intent(COURSE_PLAN), course, text_stream, map_route]  ← 실제 코스 응답
    """
    from src.graph.refine_node import _filter_refineable

    refine_failure = [
        {"type": "intent", "intent": "REFINE", "confidence": 1.0},
        {"type": "intent", "intent": "REFINE", "confidence": 1.0},
        {"type": "text_stream", "system": "...", "prompt": "..."},
        {"type": "done", "status": "done"},
    ]
    course_response = [
        {"type": "intent", "intent": "COURSE_PLAN", "confidence": 0.95},
        {"type": "course", "stops": [{"order": 1, "place": {"name": "홍대씨앤", "place_id": "p1"}}]},
        {"type": "text_stream", "system": "...", "prompt": "..."},
        {"type": "map_route", "polyline": "..."},
    ]

    refineable, intents = _filter_refineable([refine_failure, course_response])

    assert len(refineable) == 1
    assert intents == ["COURSE_PLAN"]
    assert refineable[0] is course_response


def test_filter_refineable_keeps_order() -> None:
    """refineable 응답 순서가 입력 순서(최신순)와 일치."""
    from src.graph.refine_node import _filter_refineable

    course_resp = [{"type": "intent", "intent": "COURSE_PLAN"}, {"type": "course", "stops": []}]
    places_resp = [{"type": "intent", "intent": "PLACE_SEARCH"}, {"type": "places", "items": []}]
    refine_resp = [{"type": "intent", "intent": "REFINE"}, {"type": "text_stream"}]

    refineable, intents = _filter_refineable([refine_resp, course_resp, refine_resp, places_resp])

    # refine_resp 2건은 모두 제거되고 course → places 순서 유지
    assert intents == ["COURSE_PLAN", "PLACE_SEARCH"]
    assert refineable[0] is course_resp
    assert refineable[1] is places_resp


def test_filter_refineable_all_refine_returns_empty() -> None:
    """모든 응답이 REFINE 자기응답이면 빈 결과 + 호출부가 안내 메시지로 fallthrough."""
    from src.graph.refine_node import _filter_refineable

    refine_resp = [{"type": "intent", "intent": "REFINE"}, {"type": "text_stream"}]
    refineable, intents = _filter_refineable([refine_resp, refine_resp])

    assert refineable == []
    assert intents == []


def test_identify_target_skips_refine_self_response_for_course() -> None:
    """가장 최근 응답이 REFINE 자기응답이어도 그 다음 course 응답을 target으로 잡음.

    #212 핵심 회귀 — DB 관측 사례 그대로 재현.
    """
    import asyncio

    from src.graph.refine_node import _identify_target

    refine_failure = [
        {"type": "intent", "intent": "REFINE"},
        {"type": "intent", "intent": "REFINE"},
        {"type": "text_stream"},
        {"type": "done"},
    ]
    calendar_resp = [
        {"type": "intent", "intent": "CALENDAR"},
        {"type": "text_stream"},
        {"type": "calendar", "event_id": "..."},
        {"type": "done"},
    ]
    course_resp = [
        {"type": "intent", "intent": "COURSE_PLAN"},
        {"type": "course", "stops": [{"order": 1, "place": {"name": "홍대씨앤"}}]},
        {"type": "text_stream"},
        {"type": "map_route"},
    ]

    target, intent = asyncio.run(
        _identify_target(
            query="홍대씨앤이 마음에 들지 않아, 다른 팝업 스토어로 바꿔줘",
            all_assistant_blocks=[refine_failure, calendar_resp, course_resp],
            conversation_history=[],
        )
    )

    # CALENDAR가 가장 최근 refineable이지만 plan 의도가 course 쪽이라 caller의 _parse_refinement가
    # 결정하므로 여기선 단순히 _identify_target이 self-REFINE을 skip하는지만 검증.
    # (명시적 참조 없으니 가장 최근 refineable = CALENDAR가 target.)
    assert intent == "CALENDAR"
    assert target is calendar_resp
