"""place_recommend_node 단위 테스트.

순수 함수 검증: _merge_candidates / _build_blocks / _llm_rerank fallback.
DB/OS/Gemini 의존성은 mock.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# 테스트 픽스처 데이터
# ---------------------------------------------------------------------------
_PG_RESULTS: list[dict[str, Any]] = [
    {
        "place_id": "pg-001",
        "name": "스터디카페A",
        "category": "카페",
        "address": "서울 마포구 홍대입구",
        "district": "마포구",
        "lat": 37.556,
        "lng": 126.923,
    },
]

_OS_PLACE_RESULTS: list[dict[str, Any]] = [
    {
        "place_id": "os-001",
        "name": "카페B",
        "category": "카페",
        "address": "서울 마포구",
        "district": "마포구",
        "lat": 37.557,
        "lng": 126.924,
        "score": 0.85,
    },
]

_OS_REVIEW_RESULTS: list[dict[str, Any]] = [
    {
        "place_id": "rv-001",
        "place_name": "조용한카페C",
        "keywords": ["조용한", "콘센트", "넓은 테이블"],
        "summary_text": "조용하고 콘센트가 많아 카공하기 좋은 카페입니다.",
        "score": 0.78,
    },
]


# ---------------------------------------------------------------------------
# _merge_candidates 테스트
# ---------------------------------------------------------------------------
async def test_merge_candidates_3channel() -> None:
    """3채널 결과 병합 → 중복 제거 + PG 2차 보강."""
    from src.graph.place_recommend_node import _merge_candidates  # pyright: ignore[reportMissingImports]

    # PG 2차 조회 mock
    mock_pool = AsyncMock()
    mock_pool.fetch.return_value = [
        {
            "place_id": "rv-001",
            "name": "조용한카페C",
            "category": "카페",
            "address": "서울 성동구",
            "district": "성동구",
            "lat": 37.544,
            "lng": 127.056,
        }
    ]

    merged, review_map = await _merge_candidates(mock_pool, _PG_RESULTS, _OS_PLACE_RESULTS, _OS_REVIEW_RESULTS)

    # 3채널 모두 포함 (중복 없음)
    place_ids = [m["place_id"] for m in merged]
    assert "os-001" in place_ids  # OS places_vector
    assert "pg-001" in place_ids  # PG
    assert "rv-001" in place_ids  # OS place_reviews → PG 2차 보강

    # review_data_map 구성 확인
    assert "rv-001" in review_map
    assert "조용한" in review_map["rv-001"]["keywords"]


async def test_merge_candidates_dedup() -> None:
    """동일 place_id 중복 제거."""
    from src.graph.place_recommend_node import _merge_candidates  # pyright: ignore[reportMissingImports]

    mock_pool = AsyncMock()
    mock_pool.fetch.return_value = []

    # PG와 OS에 같은 place_id
    same_pg = [{"place_id": "same-001", "name": "카페", "category": "카페"}]
    same_os = [
        {
            "place_id": "same-001",
            "name": "카페",
            "category": "카페",
            "score": 0.9,
        }
    ]

    merged, _ = await _merge_candidates(mock_pool, same_pg, same_os, [])
    assert len(merged) == 1


# ---------------------------------------------------------------------------
# _build_blocks 테스트
# ---------------------------------------------------------------------------
async def test_build_blocks_with_results() -> None:
    """결과 있을 때 → text_stream + places + map_markers + references 블록."""
    from src.graph.place_recommend_node import _build_blocks  # pyright: ignore[reportMissingImports]

    results = _PG_RESULTS + _OS_PLACE_RESULTS
    reasons = {"pg-001": "콘센트가 많아 카공에 적합"}
    review_map = {
        "pg-001": {
            "keywords": ["콘센트", "넓은"],
            "summary_text": "콘센트가 많고 넓은 테이블이 있습니다.",
        }
    }

    blocks = _build_blocks("홍대 카공 카페", results, reasons, review_map)
    types = [b["type"] for b in blocks]

    assert "text_stream" in types
    assert "places" in types
    assert "map_markers" in types
    assert "references" in types

    # places 블록 개수
    places_block = next(b for b in blocks if b["type"] == "places")
    assert places_block["total_count"] == 2

    # references 블록 — pg-001만 리뷰 데이터 있음
    ref_block = next(b for b in blocks if b["type"] == "references")
    assert len(ref_block["items"]) == 1
    assert ref_block["items"][0]["source_id"] == "pg-001"


async def test_build_blocks_empty() -> None:
    """결과 없을 때 → text_stream + 빈 places 블록."""
    from src.graph.place_recommend_node import _build_blocks  # pyright: ignore[reportMissingImports]

    blocks = _build_blocks("없는 조건", [], {}, {})
    assert len(blocks) == 2
    assert blocks[0]["type"] == "places"
    assert blocks[0]["total_count"] == 0
    assert blocks[1]["type"] == "text_stream"
    assert "찾지 못했습니다" in blocks[1]["prompt"]


async def test_build_blocks_no_coordinates() -> None:
    """좌표 없는 결과 → map_markers 블록 미생성."""
    from src.graph.place_recommend_node import _build_blocks  # pyright: ignore[reportMissingImports]

    no_coord = [{"place_id": "x", "name": "테스트", "category": "카페", "lat": None, "lng": None}]
    blocks = _build_blocks("테스트", no_coord, {}, {})
    types = [b["type"] for b in blocks]
    assert "map_markers" not in types


async def test_build_blocks_no_reviews() -> None:
    """리뷰 데이터 없는 경우 → references 블록 미생성."""
    from src.graph.place_recommend_node import _build_blocks  # pyright: ignore[reportMissingImports]

    blocks = _build_blocks("카페", _PG_RESULTS, {}, {})
    types = [b["type"] for b in blocks]
    assert "references" not in types


# ---------------------------------------------------------------------------
# _llm_rerank fallback 테스트
# ---------------------------------------------------------------------------
async def test_llm_rerank_fallback_on_error() -> None:
    """Gemini 실패 시 원본 순서 상위 5건 반환 (graceful degradation)."""
    from src.graph.place_recommend_node import _llm_rerank  # pyright: ignore[reportMissingImports]

    candidates = [
        {"place_id": f"p-{i}", "name": f"장소{i}", "category": "카페", "district": "마포구"} for i in range(10)
    ]

    mock_settings = type("Settings", (), {"gemini_llm_api_key": "fake-key"})()

    with (
        patch("src.config.get_settings", return_value=mock_settings),
        patch("langchain_google_genai.ChatGoogleGenerativeAI") as mock_llm_cls,
    ):
        mock_llm = AsyncMock()
        mock_llm.ainvoke.side_effect = Exception("API error")
        mock_llm_cls.return_value = mock_llm

        reranked, reasons = await _llm_rerank(candidates, "카공 카페", ["카공"], {})

    assert len(reranked) == 5
    assert reranked[0]["place_id"] == "p-0"  # 원본 순서 유지
    assert reasons == {}


async def test_llm_rerank_no_api_key() -> None:
    """API 키 없을 때 원본 상위 5건."""
    from src.graph.place_recommend_node import _llm_rerank  # pyright: ignore[reportMissingImports]

    candidates = [{"place_id": f"p-{i}", "name": f"장소{i}"} for i in range(10)]

    mock_settings = type("Settings", (), {"gemini_llm_api_key": ""})()

    with patch("src.config.get_settings", return_value=mock_settings):
        reranked, reasons = await _llm_rerank(candidates, "test", [], {})

    assert len(reranked) == 5
    assert reasons == {}


async def test_llm_rerank_empty_candidates() -> None:
    """빈 후보 → 빈 결과."""
    from src.graph.place_recommend_node import _llm_rerank  # pyright: ignore[reportMissingImports]

    mock_settings = type("Settings", (), {"gemini_llm_api_key": "fake-key"})()

    with patch("src.config.get_settings", return_value=mock_settings):
        reranked, reasons = await _llm_rerank([], "test", [], {})

    assert reranked == []
    assert reasons == {}


# P1-1 회귀: candidates ≤ _MAX_RESULTS면 LLM ainvoke 미호출
async def test_llm_rerank_skip_when_candidates_below_threshold() -> None:
    """후보 3건 → LLM rerank skip + OS 순서 그대로."""
    from src.graph.place_recommend_node import _llm_rerank  # pyright: ignore[reportMissingImports]

    candidates = [{"place_id": f"p-{i}", "name": f"장소{i}", "category": "카페"} for i in range(3)]

    mock_settings = type("Settings", (), {"gemini_llm_api_key": "fake-key"})()

    with (
        patch("src.config.get_settings", return_value=mock_settings),
        patch("langchain_google_genai.ChatGoogleGenerativeAI") as mock_llm_cls,
    ):
        mock_llm = AsyncMock()
        mock_llm_cls.return_value = mock_llm

        reranked, reasons = await _llm_rerank(candidates, "카페", [], {})

        mock_llm.ainvoke.assert_not_called()

    assert len(reranked) == 3
    assert reranked[0]["place_id"] == "p-0"
    assert reasons == {}
