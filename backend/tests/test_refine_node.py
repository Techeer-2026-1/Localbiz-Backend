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
