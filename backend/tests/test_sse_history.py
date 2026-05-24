"""sse.py `_load_recent_history` 단위 테스트 (#151).

빈 결과 EVENT 응답이 assistant `text` 블록으로 저장될 때 다음 턴 컨텍스트에서
정상 수집되는지 검증 (#151 G6).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.asyncio


async def test_load_recent_history_collects_assistant_text_block() -> None:
    """assistant role의 text 블록(빈 결과 정적 안내)이 history에 포함되는지 확인. #151."""
    from src.api.sse import _load_recent_history  # pyright: ignore[reportMissingImports]

    mock_pool = AsyncMock()
    # ORDER BY message_id DESC — 최신 row 먼저 (assistant turn이 user turn 다음에 발생)
    mock_pool.fetch.return_value = [
        {
            "role": "assistant",
            "blocks": [
                {
                    "type": "text",
                    "content": "조건에 맞는 행사를 찾지 못했어요.\n\n적용된 조건:\n• 자치구: 강남구",
                }
            ],
        },
        {
            "role": "user",
            "blocks": [{"type": "text", "content": "강남구 전시회 추천"}],
        },
    ]

    history = await _load_recent_history(mock_pool, "thread-1")

    # user + assistant 2개 turn 모두 수집되어야 함
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert "강남구 전시회 추천" in history[0]["content"]

    assert history[1]["role"] == "assistant"
    # 빈 결과 정적 안내(text 블록)가 컨텍스트에 포함됨
    assert "찾지 못했어요" in history[1]["content"]
    assert "강남구" in history[1]["content"]


async def test_load_recent_history_assistant_text_stream_still_works() -> None:
    """기존 assistant text_stream 블록 수집도 회귀 없음."""
    from src.api.sse import _load_recent_history  # pyright: ignore[reportMissingImports]

    mock_pool = AsyncMock()
    mock_pool.fetch.return_value = [
        {
            "role": "assistant",
            "blocks": [{"type": "text_stream", "content": "강남구 전시회 5건을 찾았어요."}],
        },
    ]

    history = await _load_recent_history(mock_pool, "thread-2")

    assert len(history) == 1
    assert history[0]["role"] == "assistant"
    assert "5건" in history[0]["content"]


async def test_load_recent_history_db_failure_returns_empty() -> None:
    """DB 조회 실패 시 빈 list 반환 (기존 graceful)."""
    from src.api.sse import _load_recent_history  # pyright: ignore[reportMissingImports]

    mock_pool = AsyncMock()
    mock_pool.fetch.side_effect = Exception("DB down")

    history = await _load_recent_history(mock_pool, "thread-3")

    assert history == []


async def test_load_recent_history_blocks_as_json_string() -> None:
    """blocks가 JSON 문자열로 저장된 경우 파싱 후 수집 (기존 동작)."""
    import json

    from src.api.sse import _load_recent_history  # pyright: ignore[reportMissingImports]

    mock_pool = AsyncMock()
    mock_pool.fetch.return_value = [
        {
            "role": "assistant",
            "blocks": json.dumps([{"type": "text", "content": "찾지 못했어요. 다른 조건으로…"}]),
        },
    ]

    history: list[dict[str, Any]] = await _load_recent_history(mock_pool, "thread-4")

    assert len(history) == 1
    assert "찾지 못했어요" in history[0]["content"]
