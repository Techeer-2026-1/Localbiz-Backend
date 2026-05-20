"""refine_helpers 단위 테스트 — apply_remove, apply_replace, apply_add, extract 유틸."""

from __future__ import annotations

from typing import Any

_SAMPLE_ITEMS: list[dict[str, Any]] = [
    {"place_id": "p1", "name": "카페A"},
    {"place_id": "p2", "name": "카페B"},
    {"place_id": "p3", "name": "카페C"},
]


def test_apply_remove_valid_index() -> None:
    from src.graph.refine_helpers import apply_remove

    result = apply_remove(_SAMPLE_ITEMS, 2)
    assert len(result) == 2
    assert result[0]["place_id"] == "p1"
    assert result[1]["place_id"] == "p3"


def test_apply_remove_out_of_range() -> None:
    from src.graph.refine_helpers import apply_remove

    result = apply_remove(_SAMPLE_ITEMS, 10)
    assert len(result) == 3


def test_apply_remove_does_not_mutate_original() -> None:
    from src.graph.refine_helpers import apply_remove

    original = [{"place_id": "p1"}, {"place_id": "p2"}]
    apply_remove(original, 1)
    assert len(original) == 2


def test_apply_replace_valid_index() -> None:
    from src.graph.refine_helpers import apply_replace

    new_item = {"place_id": "p_new", "name": "새카페"}
    result = apply_replace(_SAMPLE_ITEMS, 2, new_item)
    assert len(result) == 3
    assert result[1]["place_id"] == "p_new"
    assert result[0]["place_id"] == "p1"
    assert result[2]["place_id"] == "p3"


def test_apply_replace_out_of_range() -> None:
    from src.graph.refine_helpers import apply_replace

    result = apply_replace(_SAMPLE_ITEMS, 0, {"place_id": "x"})
    assert len(result) == 3
    assert result[0]["place_id"] == "p1"


def test_apply_add() -> None:
    from src.graph.refine_helpers import apply_add

    new_item = {"place_id": "p4", "name": "카페D"}
    result = apply_add(_SAMPLE_ITEMS, new_item)
    assert len(result) == 4
    assert result[3]["place_id"] == "p4"


def test_extract_items_from_blocks_places() -> None:
    from src.graph.refine_helpers import extract_items_from_blocks

    blocks = [
        {"type": "intent", "intent": "PLACE_SEARCH"},
        {"type": "places", "items": _SAMPLE_ITEMS},
    ]
    items = extract_items_from_blocks(blocks, "places")
    assert len(items) == 3


def test_extract_items_from_blocks_course() -> None:
    from src.graph.refine_helpers import extract_items_from_blocks

    stops = [{"order": 1, "place": {"name": "A"}}, {"order": 2, "place": {"name": "B"}}]
    blocks = [{"type": "course", "stops": stops}]
    result = extract_items_from_blocks(blocks, "course")
    assert len(result) == 2


def test_extract_items_from_blocks_missing() -> None:
    from src.graph.refine_helpers import extract_items_from_blocks

    blocks = [{"type": "text", "content": "hello"}]
    result = extract_items_from_blocks(blocks, "places")
    assert result == []


def test_extract_excluded_ids() -> None:
    from src.graph.refine_helpers import extract_excluded_ids

    ids = extract_excluded_ids(_SAMPLE_ITEMS, "place_id")
    assert ids == {"p1", "p2", "p3"}


def test_extract_excluded_ids_with_target() -> None:
    from src.graph.refine_helpers import extract_excluded_ids

    ids = extract_excluded_ids(_SAMPLE_ITEMS, "place_id", target_index=2)
    assert ids == {"p1", "p3"}


def test_get_refinement_search_query_new_condition() -> None:
    from src.graph.refine_helpers import get_refinement_search_query

    q = get_refinement_search_query(
        {"new_condition": "조용한 카페"},
        {"expanded_query": "강남 카페"},
        "원본 쿼리",
    )
    assert q == "조용한 카페"


def test_get_refinement_search_query_fallback() -> None:
    from src.graph.refine_helpers import get_refinement_search_query

    q = get_refinement_search_query({}, {"expanded_query": "강남 카페"}, "원본 쿼리")
    assert q == "강남 카페"


def test_get_refinement_search_query_final_fallback() -> None:
    from src.graph.refine_helpers import get_refinement_search_query

    q = get_refinement_search_query({}, None, "원본 쿼리")
    assert q == "원본 쿼리"
