"""query_preprocessor_node 단위 테스트.

Gemini 호출을 mock하여 공통 전처리 로직을 검증.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_general_intent_returns_empty() -> None:
    """GENERAL intent → 빈 dict 반환 (Gemini 호출 생략)."""
    from src.graph.query_preprocessor_node import query_preprocessor_node  # pyright: ignore[reportMissingImports]

    state: dict[str, Any] = {
        "query": "안녕하세요",
        "intent": "GENERAL",
    }
    result = await query_preprocessor_node(state)
    assert result["processed_query"] == {}


# P0-1 회귀 테스트: 확장된 _SKIP_INTENTS가 LLM 호출 없이 빈 dict 반환하는지 검증
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skip_intent",
    ["IMAGE_SEARCH", "CALENDAR", "REFINE", "BOOKING", "COST_ESTIMATE", "CROWDEDNESS"],
)
async def test_skip_intents_return_empty_without_llm(skip_intent: str) -> None:
    """확장된 _SKIP_INTENTS 6종 → 빈 dict + LLM 분기 미진입.

    skip 경로는 LLM 코드에 도달하기 전에 return 되어야 한다.
    `date.today()`는 LLM 분기 진입 직후 호출되므로 그것이 호출되지 않음을 검증해
    skip 경로 단축 회로를 간접적으로 입증한다.
    """
    from src.graph.query_preprocessor_node import query_preprocessor_node  # pyright: ignore[reportMissingImports]

    with patch("src.graph.query_preprocessor_node.date") as mock_date_mod:
        mock_date_mod.today.side_effect = AssertionError("skip 경로가 LLM 분기로 진입함")

        state: dict[str, Any] = {
            "query": "임의 쿼리",
            "intent": skip_intent,
        }
        result = await query_preprocessor_node(state)

        assert result["processed_query"] == {}
        mock_date_mod.today.assert_not_called()


@pytest.mark.asyncio
async def test_normal_query_extracts_fields() -> None:
    """검색 쿼리 → Gemini mock 응답에서 8필드 추출."""
    mock_response = AsyncMock()
    mock_response.content = (
        '{"original_query": "홍대 분위기 좋은 카페",'
        ' "expanded_query": "홍대 분위기 좋은 카페",'
        ' "district": "마포구",'
        ' "neighborhood": "홍대",'
        ' "category": "카페",'
        ' "keywords": ["분위기 좋은"],'
        ' "date_reference": null,'
        ' "time_reference": null}'
    )

    mock_settings = AsyncMock()
    mock_settings.gemini_llm_api_key = "fake-key-for-test"

    with (
        patch("src.config.get_settings", return_value=mock_settings),
        patch("langchain_google_genai.ChatGoogleGenerativeAI") as mock_llm_cls,
    ):
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm_cls.return_value = mock_llm

        from src.graph.query_preprocessor_node import query_preprocessor_node  # pyright: ignore[reportMissingImports]

        state: dict[str, Any] = {
            "query": "홍대 분위기 좋은 카페",
            "intent": "PLACE_SEARCH",
        }
        result = await query_preprocessor_node(state)

        pq = result["processed_query"]
        assert pq["district"] == "마포구"
        assert pq["neighborhood"] == "홍대"
        assert pq["category"] == "카페"
        assert pq["keywords"] == ["분위기 좋은"]
        assert pq["original_query"] == "홍대 분위기 좋은 카페"


@pytest.mark.asyncio
async def test_gemini_failure_returns_empty() -> None:
    """Gemini 실패 → 빈 dict fallback."""
    mock_settings = AsyncMock()
    mock_settings.gemini_llm_api_key = "fake-key-for-test"

    with (
        patch("src.config.get_settings", return_value=mock_settings),
        patch("langchain_google_genai.ChatGoogleGenerativeAI") as mock_llm_cls,
    ):
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=Exception("API error"))
        mock_llm_cls.return_value = mock_llm

        from src.graph.query_preprocessor_node import query_preprocessor_node  # pyright: ignore[reportMissingImports]

        state: dict[str, Any] = {
            "query": "강남 맛집",
            "intent": "PLACE_SEARCH",
        }
        result = await query_preprocessor_node(state)
        assert result["processed_query"] == {}
