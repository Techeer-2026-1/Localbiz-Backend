"""review_compare_node 단위 테스트 — 8개 케이스.

순수 함수 직접 호출 + AsyncMock으로 pool.fetch / os_client.mget mock.
DB 실연결 없음.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# _extract_place_names
# ---------------------------------------------------------------------------


async def test_extract_place_names_vs() -> None:
    from src.graph.review_compare_node import _extract_place_names  # pyright: ignore[reportMissingImports]

    result = _extract_place_names({}, "스타벅스 vs 블루보틀")
    assert result == ["스타벅스", "블루보틀"]


async def test_extract_place_names_wa() -> None:
    from src.graph.review_compare_node import _extract_place_names  # pyright: ignore[reportMissingImports]

    result = _extract_place_names({}, "스타벅스 와 블루보틀 비교")
    assert result == ["스타벅스", "블루보틀"]


async def test_extract_place_names_single() -> None:
    from src.graph.review_compare_node import _extract_place_names  # pyright: ignore[reportMissingImports]

    result = _extract_place_names({"keywords": ["스타벅스"]}, "스타벅스 리뷰")
    assert result == []


async def test_extract_place_names_disambiguous() -> None:
    """공백 포함 구분자 — 장소명 내 false positive 없음 확인."""
    from src.graph.review_compare_node import _extract_place_names  # pyright: ignore[reportMissingImports]

    result = _extract_place_names({}, "강남대로 vs 홍대")
    assert result == ["강남대로", "홍대"]


# ---------------------------------------------------------------------------
# _fetch_places_pg — 동명 다중 매칭
# ---------------------------------------------------------------------------


async def test_fetch_places_pg_multiple_match() -> None:
    """동명 3개 중 OS stars 최댓값 채택."""
    from src.graph.review_compare_node import _fetch_places_pg  # pyright: ignore[reportMissingImports]

    rows: list[dict[str, Any]] = [
        {"place_id": "uuid-1", "name": "스타벅스 A", "category": "카페", "district": "강남구"},
        {"place_id": "uuid-2", "name": "스타벅스 B", "category": "카페", "district": "마포구"},
        {"place_id": "uuid-3", "name": "스타벅스 C", "category": "카페", "district": "종로구"},
    ]

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=rows)

    os_client = MagicMock()
    os_client.mget = AsyncMock(
        return_value={
            "docs": [
                {"found": True, "_source": {"place_id": "uuid-1", "stars": 3.5}},
                {"found": True, "_source": {"place_id": "uuid-2", "stars": 4.2}},
                {"found": True, "_source": {"place_id": "uuid-3", "stars": 4.0}},
            ]
        }
    )

    result = await _fetch_places_pg(pool, ["스타벅스"], os_client)
    assert len(result) == 1
    assert result[0]["place_id"] == "uuid-2"


# ---------------------------------------------------------------------------
# _build_compare_blocks
# ---------------------------------------------------------------------------


async def test_build_compare_blocks_success() -> None:
    """정상 케이스 — text_stream/chart/analysis_sources 구조 검증."""
    from src.graph.review_compare_node import _build_compare_blocks  # pyright: ignore[reportMissingImports]

    places: list[dict[str, Any]] = [
        {"place_id": "uuid-1", "name": "스타벅스 홍대점", "category": "카페"},
        {"place_id": "uuid-2", "name": "블루보틀 삼청점", "category": "카페"},
    ]
    scores_map = {
        "uuid-1": {
            "satisfaction": 3.5,
            "accessibility": 4.0,
            "cleanliness": 3.0,
            "value": 2.5,
            "atmosphere": 4.5,
            "expertise": 4.0,
        },
        "uuid-2": {
            "satisfaction": 4.2,
            "accessibility": 3.0,
            "cleanliness": 4.5,
            "value": 3.0,
            "atmosphere": 4.8,
            "expertise": 4.2,
        },
    }

    review_count_map = {"uuid-1": 12, "uuid-2": 30}
    blocks = _build_compare_blocks("스타벅스 vs 블루보틀 비교", places, scores_map, review_count_map)

    assert len(blocks) == 3

    ts = blocks[0]
    assert ts["type"] == "text_stream"
    assert "system" in ts
    assert "prompt" in ts

    chart = blocks[1]
    assert chart["type"] == "chart"
    assert chart["chart_type"] == "radar"
    assert len(chart["places"]) == 2
    assert set(chart["places"][0]["scores"].keys()) == {
        "satisfaction",
        "accessibility",
        "cleanliness",
        "value",
        "atmosphere",
        "expertise",
    }
    assert set(chart["places"][1]["scores"].keys()) == set(chart["places"][0]["scores"].keys())

    src = blocks[2]
    assert src["type"] == "analysis_sources"
    assert src["review_count"] == 42


async def test_build_compare_blocks_no_scores() -> None:
    """OS 문서 없는 장소 → scores 빈 dict → 6 키 0.0 fill (#214)."""
    from src.graph.review_compare_node import _build_compare_blocks  # pyright: ignore[reportMissingImports]

    places: list[dict[str, Any]] = [
        {"place_id": "uuid-1", "name": "장소A", "category": "카페"},
        {"place_id": "uuid-2", "name": "장소B", "category": "카페"},
    ]

    blocks = _build_compare_blocks("장소A vs 장소B 비교", places, {}, {})

    chart = blocks[1]
    assert len(chart["places"]) == 2
    for place_chart in chart["places"]:
        assert set(place_chart["scores"].keys()) == {
            "satisfaction",
            "accessibility",
            "cleanliness",
            "value",
            "atmosphere",
            "expertise",
        }
        assert all(v == 0.0 for v in place_chart["scores"].values())
    assert blocks[2]["review_count"] == 0


# ---------------------------------------------------------------------------
# #214 회귀 — text_stream prompt에 raw 점수 leak 금지 + 비교 키워드 self-check
# ---------------------------------------------------------------------------
def test_build_compare_blocks_text_stream_does_not_leak_scores() -> None:
    """text_stream prompt에 점수 수치/지표명이 leak되면 안 됨 (#214 C1)."""
    from src.graph.review_compare_node import _build_compare_blocks  # pyright: ignore[reportMissingImports]

    places: list[dict[str, Any]] = [
        {"place_id": "p1", "name": "A", "category": "카페"},
        {"place_id": "p2", "name": "B", "category": "카페"},
    ]
    scores_map: dict[str, dict[str, float]] = {
        "p1": {"satisfaction": 4.9, "value": 2.5, "cleanliness": 4.9},
        "p2": {"satisfaction": 4.5, "value": 3.0, "cleanliness": 4.7},
    }

    blocks = _build_compare_blocks("A vs B 비교", places, scores_map, {"p1": 5, "p2": 5})

    prompt: str = blocks[0]["prompt"]
    forbidden_score_tokens = ("4.9", "2.5", "4.5", "3.0", "4.7", "5.0", "/5", "/5.0")
    for tok in forbidden_score_tokens:
        assert tok not in prompt, f"prompt에 점수 leak: {tok!r}"
    for key in ("satisfaction", "accessibility", "cleanliness", "value", "atmosphere", "expertise"):
        assert key not in prompt, f"prompt에 지표 키 leak: {key!r}"


def test_build_compare_blocks_chart_has_six_metric_keys() -> None:
    """chart.places[].scores는 항상 6 키 모두 보유 (#214 M2)."""
    from src.graph.review_compare_node import _build_compare_blocks  # pyright: ignore[reportMissingImports]

    places: list[dict[str, Any]] = [
        {"place_id": "p1", "name": "A", "category": "카페"},
        {"place_id": "p2", "name": "B", "category": "카페"},
    ]
    scores_map = {"p1": {"satisfaction": 4.0}, "p2": {}}

    blocks = _build_compare_blocks("A 비교 B", places, scores_map, {})

    expected_keys = {"satisfaction", "accessibility", "cleanliness", "value", "atmosphere", "expertise"}
    for place_chart in blocks[1]["places"]:
        assert set(place_chart["scores"].keys()) == expected_keys


async def test_review_compare_node_no_compare_keyword_returns_disambiguation() -> None:
    """비교 키워드 없는 쿼리("A랑 B 어디냐?")는 intent_router 오분류 backstop으로 안내로 빠짐 (#214 H3)."""
    from src.graph.review_compare_node import review_compare_node  # pyright: ignore[reportMissingImports]

    state: dict[str, Any] = {
        "query": "물포곤 청담점이랑 광화문점 어디냐?",
        "processed_query": {"keywords": ["물포곤 청담점", "광화문점"]},
    }

    result = await review_compare_node(state)
    blocks = result["response_blocks"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "disambiguation"
    msg = blocks[0]["message"]
    assert "비교" in msg or "리뷰 비교" in msg


def test_intent_router_review_compare_requires_trigger_keyword() -> None:
    """intent_router 분류 프롬프트 양쪽에 REVIEW_COMPARE trigger keyword 필수 명시 (#214 H2)."""
    from src.graph.intent_router_node import _CLASSIFY_MULTI_SYSTEM_PROMPT, _CLASSIFY_SYSTEM_PROMPT

    for prompt in (_CLASSIFY_SYSTEM_PROMPT, _CLASSIFY_MULTI_SYSTEM_PROMPT):
        assert "Trigger keywords REQUIRED" in prompt, "REVIEW_COMPARE trigger keyword 명시 누락"
        assert "NOT REVIEW_COMPARE" in prompt, "REVIEW_COMPARE 부정 예시 누락"


# ---------------------------------------------------------------------------
# review_compare_node — disambiguation
# ---------------------------------------------------------------------------


async def test_review_compare_node_disambiguation() -> None:
    """장소명 1개 입력 → disambiguation 블록 반환 (DB 호출 없음)."""
    from src.graph.review_compare_node import review_compare_node  # pyright: ignore[reportMissingImports]

    state: dict[str, Any] = {
        "query": "스타벅스 리뷰",
        "processed_query": {"keywords": ["스타벅스"]},
    }
    result = await review_compare_node(state)

    blocks = result["response_blocks"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "disambiguation"
    assert "어느 장소와 비교하시겠어요?" in blocks[0]["message"]
    assert blocks[0]["candidates"] == []


async def test_review_compare_node_one_place_not_found() -> None:
    """PG에서 한 장소 미매칭 → 1건 resolve → 찾은 장소 text_stream 소개."""
    from unittest.mock import patch

    from src.graph.review_compare_node import review_compare_node  # pyright: ignore[reportMissingImports]

    fetch_results = [
        [{"place_id": "uuid-1", "name": "맥도날드장안", "category": "음식점", "district": "동대문구"}],
        [],
    ]

    mock_pool = MagicMock()
    mock_pool.fetch = AsyncMock(side_effect=fetch_results)
    mock_os = MagicMock()

    with (
        patch("src.db.postgres.get_pool", return_value=mock_pool),
        patch("src.db.opensearch.get_os_client", return_value=mock_os),
    ):
        state: dict[str, Any] = {
            "query": "맥도날드장안 vs 없는장소 비교",
            "processed_query": {},
        }
        result = await review_compare_node(state)

    blocks = result["response_blocks"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text_stream"
    assert "없는장소" in blocks[0]["prompt"]
    assert "맥도날드장안" in blocks[0]["prompt"]


async def test_review_compare_node_no_places_found() -> None:
    """PG에서 두 장소 모두 미매칭 → disambiguation 블록 반환."""
    from unittest.mock import patch

    from src.graph.review_compare_node import review_compare_node  # pyright: ignore[reportMissingImports]

    mock_pool = MagicMock()
    mock_pool.fetch = AsyncMock(return_value=[])
    mock_os = MagicMock()

    with (
        patch("src.db.postgres.get_pool", return_value=mock_pool),
        patch("src.db.opensearch.get_os_client", return_value=mock_os),
    ):
        state: dict[str, Any] = {
            "query": "없는장소A vs 없는장소B 비교",
            "processed_query": {},
        }
        result = await review_compare_node(state)

    blocks = result["response_blocks"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "disambiguation"
    assert blocks[0]["candidates"] == []
