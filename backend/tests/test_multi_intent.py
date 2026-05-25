"""multi-intent 단위 테스트 — 7개 케이스.

classify_intents + intent_router_node 주입 가드.
DB 실연결 없음 (AsyncMock).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


def _mock_settings() -> MagicMock:
    """get_settings() mock — gemini_llm_api_key 설정된 상태."""
    settings = MagicMock()
    settings.gemini_llm_api_key = "fake-key-for-test"
    return settings


# ---------------------------------------------------------------------------
# classify_intents — 단일 intent
# ---------------------------------------------------------------------------


async def test_classify_intents_single() -> None:
    """단일 intent 쿼리 → 1개 리스트, sub_query = 원본."""
    from src.graph.intent_router_node import IntentType, classify_intents  # pyright: ignore[reportMissingImports]

    mock_response = MagicMock()
    mock_response.content = json.dumps(
        {"intents": [{"intent": "PLACE_RECOMMEND", "confidence": 0.95, "sub_query": "홍대 카페 추천해줘"}]}
    )

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=mock_response)

    with (
        patch("langchain_google_genai.ChatGoogleGenerativeAI", return_value=mock_llm),
        patch("src.config.get_settings", return_value=_mock_settings()),
    ):
        result = await classify_intents("홍대 카페 추천해줘")

    assert len(result) == 1
    assert result[0][0] == IntentType.PLACE_RECOMMEND
    assert result[0][2] == "홍대 카페 추천해줘"


# ---------------------------------------------------------------------------
# classify_intents — 복수 intent
# ---------------------------------------------------------------------------


async def test_classify_intents_multi() -> None:
    """복수 intent 쿼리 → 2개 리스트 + sub_query 분리."""
    from src.graph.intent_router_node import IntentType, classify_intents  # pyright: ignore[reportMissingImports]

    mock_response = MagicMock()
    mock_response.content = json.dumps(
        {
            "intents": [
                {"intent": "PLACE_RECOMMEND", "confidence": 0.9, "sub_query": "카페 추천해줘"},
                {"intent": "EVENT_SEARCH", "confidence": 0.85, "sub_query": "전시회 알려줘"},
            ]
        }
    )

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=mock_response)

    with (
        patch("langchain_google_genai.ChatGoogleGenerativeAI", return_value=mock_llm),
        patch("src.config.get_settings", return_value=_mock_settings()),
    ):
        result = await classify_intents("카페 추천해주고 전시회 알려줘")

    assert len(result) == 2
    assert result[0][0] == IntentType.PLACE_RECOMMEND
    assert result[0][2] == "카페 추천해줘"
    assert result[1][0] == IntentType.EVENT_SEARCH
    assert result[1][2] == "전시회 알려줘"


# ---------------------------------------------------------------------------
# classify_intents — 최대 3개 제한
# ---------------------------------------------------------------------------


async def test_classify_intents_max_3() -> None:
    """4개 이상 → 앞 3개만 채택."""
    from src.graph.intent_router_node import classify_intents  # pyright: ignore[reportMissingImports]

    mock_response = MagicMock()
    mock_response.content = json.dumps(
        {
            "intents": [
                {"intent": "PLACE_RECOMMEND", "confidence": 0.9, "sub_query": "a"},
                {"intent": "EVENT_SEARCH", "confidence": 0.8, "sub_query": "b"},
                {"intent": "ANALYSIS", "confidence": 0.7, "sub_query": "c"},
                {"intent": "GENERAL", "confidence": 0.5, "sub_query": "d"},
            ]
        }
    )

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=mock_response)

    with (
        patch("langchain_google_genai.ChatGoogleGenerativeAI", return_value=mock_llm),
        patch("src.config.get_settings", return_value=_mock_settings()),
    ):
        result = await classify_intents("a b c d")

    assert len(result) == 3


# ---------------------------------------------------------------------------
# classify_intents — 라우팅 불가 intent → GENERAL fallback
# ---------------------------------------------------------------------------


async def test_classify_intents_phase2_filter() -> None:
    """라우팅 불가 intent(FAVORITE) → GENERAL fallback."""
    from src.graph.intent_router_node import IntentType, classify_intents  # pyright: ignore[reportMissingImports]

    mock_response = MagicMock()
    mock_response.content = json.dumps(
        {"intents": [{"intent": "FAVORITE", "confidence": 0.9, "sub_query": "이 식당 저장해줘"}]}
    )

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=mock_response)

    with (
        patch("langchain_google_genai.ChatGoogleGenerativeAI", return_value=mock_llm),
        patch("src.config.get_settings", return_value=_mock_settings()),
    ):
        result = await classify_intents("이 식당 저장해줘")

    assert len(result) == 1
    assert result[0][0] == IntentType.GENERAL


# ---------------------------------------------------------------------------
# classify_intents — Gemini 실패 fallback
# ---------------------------------------------------------------------------


async def test_classify_intents_fallback() -> None:
    """Gemini 실패 → [("GENERAL", 0.0, query)]."""
    from src.graph.intent_router_node import IntentType, classify_intents  # pyright: ignore[reportMissingImports]

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=Exception("API error"))

    with (
        patch("langchain_google_genai.ChatGoogleGenerativeAI", return_value=mock_llm),
        patch("src.config.get_settings", return_value=_mock_settings()),
    ):
        result = await classify_intents("테스트 쿼리")

    assert len(result) == 1
    assert result[0][0] == IntentType.GENERAL
    assert result[0][2] == "테스트 쿼리"


# ---------------------------------------------------------------------------
# intent_router_node — 주입 가드
# ---------------------------------------------------------------------------


async def test_intent_router_node_injected() -> None:
    """intent 주입 시 classify 스킵, intent 블록만 반환."""
    from src.graph.intent_router_node import intent_router_node  # pyright: ignore[reportMissingImports]

    state: dict[str, Any] = {
        "query": "카페 추천해줘",
        "intent": "PLACE_RECOMMEND",
    }
    result = await intent_router_node(state)

    blocks = result["response_blocks"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "intent"
    assert blocks[0]["intent"] == "PLACE_RECOMMEND"
    assert blocks[0]["confidence"] == 1.0
    # intent 키가 반환에 없음 (state의 주입값 유지)
    assert "intent" not in result


async def test_intent_router_node_no_injection() -> None:
    """intent 미주입 시 기존 classify_intent 호출 (regression 확인)."""
    from src.graph.intent_router_node import IntentType, intent_router_node  # pyright: ignore[reportMissingImports]

    mock_classify = AsyncMock(return_value=(IntentType.GENERAL, 0.5))

    with patch("src.graph.intent_router_node.classify_intent", mock_classify):
        state: dict[str, Any] = {
            "query": "안녕",
        }
        result = await intent_router_node(state)

    mock_classify.assert_called_once_with("안녕", None)
    assert result["intent"] == "GENERAL"
    assert result["response_blocks"][0]["intent"] == "GENERAL"


# ---------------------------------------------------------------------------
# 프롬프트 가드 — SEARCH vs RECOMMEND 판별 규칙 존재 확인
# ---------------------------------------------------------------------------


async def test_classify_prompts_have_search_recommend_guidance() -> None:
    """'찾아줘/알려줘'(SEARCH)를 RECOMMEND로 오분류하던 문제 교정 규칙 보존.

    라이브 검증(2026-05-25)에서 EVENT_SEARCH/PLACE_SEARCH 쿼리가 RECOMMEND로
    오분류되어 프롬프트에 판별 규칙을 추가했다. 실수로 제거되지 않도록 가드한다.
    """
    from src.graph import intent_router_node as mod  # pyright: ignore[reportMissingImports]

    for prompt in (mod._CLASSIFY_MULTI_SYSTEM_PROMPT, mod._CLASSIFY_SYSTEM_PROMPT):
        assert "SEARCH vs RECOMMEND" in prompt
        assert "prefer SEARCH" in prompt
