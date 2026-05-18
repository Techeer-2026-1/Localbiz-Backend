"""event_search_node 단위 테스트.

내부 헬퍼 함수 _naver_to_event_dict / _build_blocks / _search_os_events /
_merge_candidates / _llm_rerank 를 직접 테스트.
노드 함수(event_search_node)는 DB/OS/Naver 의존성이 있으므로 머지 후 manual 검증.
place_recommend_node 테스트 양식과 일관.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# _naver_to_event_dict — Naver 응답 변환
# ---------------------------------------------------------------------------
async def test_naver_to_event_dict_html_clean() -> None:
    """Naver 검색 결과의 <b> 태그 강조 표시 → 평문 변환."""
    from src.graph.event_search_node import _naver_to_event_dict  # pyright: ignore[reportMissingImports]

    item = {
        "title": "<b>전시회</b> 후기 - 이번 주말 추천",
        "link": "https://blog.naver.com/example/123",
        "description": "<b>전시회</b>는 정말 좋았어요...",
        "bloggername": "test_blogger",
        "postdate": "20260501",
    }
    event = _naver_to_event_dict(item)

    assert event["title"] == "전시회 후기 - 이번 주말 추천"
    assert event["summary"] == "전시회는 정말 좋았어요..."
    assert event["detail_url"] == "https://blog.naver.com/example/123"
    assert event["source"] == "naver_blog"
    # DB 전용 필드는 None
    assert event["event_id"] is None
    assert event["category"] is None
    assert event["address"] is None


async def test_naver_to_event_dict_missing_fields() -> None:
    """Naver 응답에 일부 필드 누락 시 None으로 안전 처리."""
    from src.graph.event_search_node import _naver_to_event_dict  # pyright: ignore[reportMissingImports]

    item = {"title": "단순 제목"}  # link, description 등 없음
    event = _naver_to_event_dict(item)

    assert event["title"] == "단순 제목"
    assert event["summary"] == ""
    assert event["detail_url"] is None
    assert event["source"] == "naver_blog"


# ---------------------------------------------------------------------------
# _build_blocks — 결과 → 블록 변환
# ---------------------------------------------------------------------------
_DB_EVENTS: list[dict[str, Any]] = [
    {
        "event_id": "01234567-89ab-cdef-0123-456789abcdef",
        "title": "재즈 페스티벌",
        "category": "공연",
        "place_name": "올림픽공원",
        "address": "서울 송파구",
        "district": "송파구",
        "lat": 37.520,
        "lng": 127.121,
        "date_start": "2026-05-10",
        "date_end": "2026-05-12",
        "price": 50000,
        "poster_url": "https://example.com/poster.jpg",
        "detail_url": "https://example.com/event/1",
        "summary": "재즈 음악 축제",
        "source": "서울시문화행사",
    },
]

_NAVER_EVENTS: list[dict[str, Any]] = [
    {
        "event_id": None,
        "title": "전시회 추천 블로그",
        "category": None,
        "place_name": None,
        "address": None,
        "district": None,
        "lat": None,
        "lng": None,
        "date_start": None,
        "date_end": None,
        "price": None,
        "poster_url": None,
        "detail_url": "https://blog.naver.com/example",
        "summary": "이번 주말 갈만한 전시회 모음",
        "source": "naver_blog",
    },
]


async def test_build_blocks_with_db_events_only() -> None:
    """DB events만 있을 때: text_stream + events 블록 (references 없음)."""
    from src.graph.event_search_node import _build_blocks  # pyright: ignore[reportMissingImports]

    blocks = _build_blocks("이번 주말 송파구 행사", _DB_EVENTS, [])

    block_types = [b["type"] for b in blocks]
    assert "text_stream" in block_types
    assert "events" in block_types
    assert "references" not in block_types  # DB 결과만이라 references 없음

    # events 블록 내용 검증
    events_block = next(b for b in blocks if b["type"] == "events")
    assert events_block["total_count"] == 1
    item = events_block["items"][0]
    assert item["title"] == "재즈 페스티벌"
    assert item["category"] == "공연"
    assert item["district"] == "송파구"
    assert item["price"] == 50000


async def test_build_blocks_with_naver_fallback() -> None:
    """Naver fallback 결과 포함 시: references 블록 추가."""
    from src.graph.event_search_node import _build_blocks  # pyright: ignore[reportMissingImports]

    merged = _DB_EVENTS + _NAVER_EVENTS
    blocks = _build_blocks("주말 전시회", merged, [])

    block_types = [b["type"] for b in blocks]
    assert "text_stream" in block_types
    assert "events" in block_types
    assert "references" in block_types

    # references는 Naver 결과만 포함
    refs_block = next(b for b in blocks if b["type"] == "references")
    assert len(refs_block["items"]) == 1
    assert refs_block["items"][0]["url"] == "https://blog.naver.com/example"
    assert refs_block["items"][0]["source"] == "naver_blog"


async def test_build_blocks_empty_results() -> None:
    """검색 결과 0건: text_stream 블록만 생성 (events / references 없음)."""
    from src.graph.event_search_node import _build_blocks  # pyright: ignore[reportMissingImports]

    blocks = _build_blocks("결과 없는 쿼리", [], [])

    block_types = [b["type"] for b in blocks]
    assert "text_stream" in block_types
    assert "events" not in block_types
    assert "references" not in block_types

    # text_stream에 "검색 결과가 없습니다" 안내
    ts_block = next(b for b in blocks if b["type"] == "text_stream")
    assert "검색 결과가 없습니다" in ts_block["prompt"]


# ---------------------------------------------------------------------------
# _search_os_events — events_vector k-NN + date post-filter
# ---------------------------------------------------------------------------
async def test_search_os_events_post_filter() -> None:
    """date post-filter: 종료된 행사 / NULL date_end 제외, 미종료만 반환 (#110 G1b)."""
    from src.graph import event_search_node as mod  # pyright: ignore[reportMissingImports]

    mock_os = AsyncMock()
    mock_os.search.return_value = {
        "hits": {
            "hits": [
                {
                    "_id": "e-future",
                    "_score": 0.9,
                    "_source": {
                        "event_id": "e-future",
                        "title": "미래 행사",
                        "date_start": "2099-01-01",
                        "date_end": "2099-12-31",
                    },
                },
                {
                    "_id": "e-past",
                    "_score": 0.8,
                    "_source": {
                        "event_id": "e-past",
                        "title": "지난 행사",
                        "date_start": "1999-01-01",
                        "date_end": "2000-01-01",
                    },
                },
                {
                    "_id": "e-null",
                    "_score": 0.7,
                    "_source": {"event_id": "e-null", "title": "날짜 없는 행사", "date_end": None},
                },
            ]
        }
    }

    with patch.object(mod, "_embed_query_768d", AsyncMock(return_value=[0.1] * 768)):
        results = await mod._search_os_events(mock_os, "전시", "key", "2026-05-18", None, None)

    assert [r["event_id"] for r in results] == ["e-future"]


async def test_search_os_events_resolved_date_upper_bound() -> None:
    """resolved date 둘 다 있으면 date_start > date_end_resolved 행사 제외."""
    from src.graph import event_search_node as mod  # pyright: ignore[reportMissingImports]

    mock_os = AsyncMock()
    mock_os.search.return_value = {
        "hits": {
            "hits": [
                {
                    "_id": "in-range",
                    "_score": 0.9,
                    "_source": {
                        "event_id": "in-range",
                        "title": "범위 내",
                        "date_start": "2026-05-20",
                        "date_end": "2026-05-25",
                    },
                },
                {
                    "_id": "too-late",
                    "_score": 0.8,
                    "_source": {
                        "event_id": "too-late",
                        "title": "범위 밖",
                        "date_start": "2026-07-01",
                        "date_end": "2026-07-10",
                    },
                },
            ]
        }
    }

    with patch.object(mod, "_embed_query_768d", AsyncMock(return_value=[0.1] * 768)):
        results = await mod._search_os_events(mock_os, "q", "key", "2026-05-18", "2026-05-19", "2026-05-30")

    assert [r["event_id"] for r in results] == ["in-range"]


async def test_search_os_events_resolved_date_lower_bound() -> None:
    """resolved date 둘 다 있으면 date_end < date_start_resolved 행사 제외 (overlap 하한).

    CodeRabbit 지적: today_iso 이후 종료지만 요청 기간보다 먼저 끝난 행사를 거른다.
    """
    from src.graph import event_search_node as mod  # pyright: ignore[reportMissingImports]

    mock_os = AsyncMock()
    mock_os.search.return_value = {
        "hits": {
            "hits": [
                {
                    "_id": "in-range",
                    "_score": 0.9,
                    "_source": {
                        "event_id": "in-range",
                        "title": "범위 내",
                        "date_start": "2026-05-20",
                        "date_end": "2026-05-25",
                    },
                },
                {
                    "_id": "too-early",
                    "_score": 0.8,
                    "_source": {
                        "event_id": "too-early",
                        "title": "요청 기간 이전 종료 (today 이후지만 범위 밖)",
                        "date_start": "2026-05-01",
                        "date_end": "2026-05-05",
                    },
                },
            ]
        }
    }

    with patch.object(mod, "_embed_query_768d", AsyncMock(return_value=[0.1] * 768)):
        results = await mod._search_os_events(mock_os, "q", "key", "2026-04-30", "2026-05-19", "2026-05-30")

    assert [r["event_id"] for r in results] == ["in-range"]


async def test_search_os_events_zero_vector_skip() -> None:
    """임베딩이 zero-vector면 OS 검색 자체를 skip (빈 list, search 미호출)."""
    from src.graph import event_search_node as mod  # pyright: ignore[reportMissingImports]

    mock_os = AsyncMock()
    with patch.object(mod, "_embed_query_768d", AsyncMock(return_value=[0.0] * 768)):
        results = await mod._search_os_events(mock_os, "전시", "key", "2026-05-18", None, None)

    assert results == []
    mock_os.search.assert_not_called()


# ---------------------------------------------------------------------------
# _merge_candidates — PG 정형 + OS 의미 병합 + PG 2차 보강
# ---------------------------------------------------------------------------
def _pg_row(event_id: str, district: str) -> dict[str, Any]:
    """PG 2차 보강 조회가 반환하는 행 양식."""
    return {
        "event_id": event_id,
        "title": "행사",
        "category": "공연",
        "place_name": "홀",
        "address": "서울",
        "district": district,
        "lat": 37.5,
        "lng": 127.0,
        "date_start": "2026-06-01",
        "date_end": "2026-06-02",
        "price": 0,
        "poster_url": None,
        "detail_url": "https://example.com/e",
        "summary": "요약",
        "source": "서울시문화행사",
    }


async def test_merge_candidates_os_enrichment() -> None:
    """OS hit이 PG 2차 보강으로 표시 필드를 채운다 (#110 G1)."""
    from src.graph.event_search_node import _merge_candidates  # pyright: ignore[reportMissingImports]

    os_results = [{"event_id": "os-1", "title": "행사", "score": 0.9}]
    mock_pool = AsyncMock()
    mock_pool.fetch.return_value = [_pg_row("os-1", "중구")]

    merged = await _merge_candidates(mock_pool, [], os_results)

    assert len(merged) == 1
    assert merged[0]["district"] == "중구"  # PG 보강 필드


async def test_merge_candidates_discards_pg_absent() -> None:
    """PG에 없는 OS hit(하드 삭제 등)은 폐기된다."""
    from src.graph.event_search_node import _merge_candidates  # pyright: ignore[reportMissingImports]

    os_results = [{"event_id": "ghost", "title": "삭제된 행사", "score": 0.9}]
    mock_pool = AsyncMock()
    mock_pool.fetch.return_value = []  # PG 부재

    merged = await _merge_candidates(mock_pool, [], os_results)

    assert merged == []


async def test_merge_candidates_pg_exception_graceful() -> None:
    """PG 2차 조회 예외 시 PG 정형 결과로 graceful 진행 (OS 보강분 폐기)."""
    from src.graph.event_search_node import _merge_candidates  # pyright: ignore[reportMissingImports]

    pg_results = [{"event_id": "pg-1", "title": "정형 행사"}]
    os_results = [{"event_id": "os-1", "title": "의미 행사", "score": 0.9}]
    mock_pool = AsyncMock()
    mock_pool.fetch.side_effect = Exception("DB error")

    merged = await _merge_candidates(mock_pool, pg_results, os_results)

    assert [m["event_id"] for m in merged] == ["pg-1"]


async def test_merge_candidates_dedup_os_priority() -> None:
    """동일 event_id가 OS·PG 양쪽에 있으면 1건으로 제거, OS 보강분 우선."""
    from src.graph.event_search_node import _merge_candidates  # pyright: ignore[reportMissingImports]

    os_results = [{"event_id": "dup", "title": "행사", "score": 0.9}]
    pg_results = [{"event_id": "dup", "title": "행사", "district": "PG정형"}]
    mock_pool = AsyncMock()
    mock_pool.fetch.return_value = [_pg_row("dup", "OS보강")]

    merged = await _merge_candidates(mock_pool, pg_results, os_results)

    assert len(merged) == 1
    assert merged[0]["district"] == "OS보강"


# ---------------------------------------------------------------------------
# _llm_rerank — Gemini Flash 순위 재배치 + per-event 소개
# ---------------------------------------------------------------------------
async def test_llm_rerank_reorders_and_aligns_descriptions() -> None:
    """ranked_indices 순서로 재배치 + descriptions를 reranked 순서에 정렬 (#110 G2)."""
    from src.graph import event_search_node as mod  # pyright: ignore[reportMissingImports]

    candidates = [{"event_id": f"e-{i}", "title": f"행사{i}"} for i in range(4)]
    mock_settings = type("Settings", (), {"gemini_llm_api_key": "fake-key"})()

    with (
        patch("src.config.get_settings", return_value=mock_settings),
        patch("langchain_google_genai.ChatGoogleGenerativeAI") as mock_llm_cls,
    ):
        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = type(
            "R",
            (),
            {"content": '{"ranked_indices": [2, 0], "reasons": {"2": "이유2", "0": "이유0"}}'},
        )()
        mock_llm_cls.return_value = mock_llm
        reranked, descriptions = await mod._llm_rerank(candidates, "쿼리", ["키워드"])

    # ranked_indices 순서 반영
    assert reranked[0]["event_id"] == "e-2"
    assert reranked[1]["event_id"] == "e-0"
    # descriptions가 reranked 순서에 정렬
    assert descriptions[0] == "이유2"
    assert descriptions[1] == "이유0"
    # ranked_indices에 없던 후보도 원본 순서로 채움 + 길이 일치
    assert len(reranked) == 4
    assert len(descriptions) == len(reranked)


async def test_llm_rerank_fallback_on_error() -> None:
    """Gemini 실패 시 병합 순서 상위 5건 + 빈 descriptions (graceful degradation)."""
    from src.graph import event_search_node as mod  # pyright: ignore[reportMissingImports]

    candidates = [{"event_id": f"e-{i}", "title": f"행사{i}"} for i in range(8)]
    mock_settings = type("Settings", (), {"gemini_llm_api_key": "fake-key"})()

    with (
        patch("src.config.get_settings", return_value=mock_settings),
        patch("langchain_google_genai.ChatGoogleGenerativeAI") as mock_llm_cls,
    ):
        mock_llm = AsyncMock()
        mock_llm.ainvoke.side_effect = Exception("API error")
        mock_llm_cls.return_value = mock_llm
        reranked, descriptions = await mod._llm_rerank(candidates, "쿼리", [])

    assert len(reranked) == 5
    assert reranked[0]["event_id"] == "e-0"  # 원본 순서 유지
    assert descriptions == []


async def test_llm_rerank_no_api_key() -> None:
    """API 키 없을 때 병합 순서 상위 5건."""
    from src.graph.event_search_node import _llm_rerank  # pyright: ignore[reportMissingImports]

    candidates = [{"event_id": f"e-{i}", "title": f"행사{i}"} for i in range(8)]
    mock_settings = type("Settings", (), {"gemini_llm_api_key": ""})()

    with patch("src.config.get_settings", return_value=mock_settings):
        reranked, descriptions = await _llm_rerank(candidates, "test", [])

    assert len(reranked) == 5
    assert descriptions == []


async def test_llm_rerank_empty_candidates() -> None:
    """빈 후보 → 빈 결과."""
    from src.graph.event_search_node import _llm_rerank  # pyright: ignore[reportMissingImports]

    mock_settings = type("Settings", (), {"gemini_llm_api_key": "fake-key"})()

    with patch("src.config.get_settings", return_value=mock_settings):
        reranked, descriptions = await _llm_rerank([], "test", [])

    assert reranked == []
    assert descriptions == []
