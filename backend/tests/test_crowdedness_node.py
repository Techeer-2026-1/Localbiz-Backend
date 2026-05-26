"""crowdedness_node 단위 테스트.

순수 함수 직접 호출 + AsyncMock으로 pool.fetch/fetchrow mock.
DB 실연결 없음.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# _classify_level
# ---------------------------------------------------------------------------


async def test_classify_level_한산() -> None:
    from src.graph.crowdedness_node import _classify_level  # pyright: ignore[reportMissingImports]

    assert _classify_level(0.5) == "한산"


async def test_classify_level_보통() -> None:
    from src.graph.crowdedness_node import _classify_level  # pyright: ignore[reportMissingImports]

    assert _classify_level(1.0) == "보통"


async def test_classify_level_혼잡() -> None:
    from src.graph.crowdedness_node import _classify_level  # pyright: ignore[reportMissingImports]

    assert _classify_level(1.3) == "혼잡"


async def test_classify_level_혼잡_high_ratio() -> None:
    from src.graph.crowdedness_node import _classify_level  # pyright: ignore[reportMissingImports]

    assert _classify_level(1.6) == "혼잡"


async def test_classify_level_zero_avg() -> None:
    """avg_pop=0 이면 노드가 _classify_level 을 호출하지 않고 '보통' 반환."""
    from src.graph.crowdedness_node import crowdedness_node  # pyright: ignore[reportMissingImports]

    pop_row: dict[str, Any] = {
        "current_pop": 1000,
        "base_date": date(2026, 5, 5),
        "avg_pop": 0,
    }
    pool = MagicMock()

    with (
        patch("src.db.postgres.get_pool", return_value=pool),
        patch(
            "src.graph.crowdedness_node._resolve_dong_codes",
            AsyncMock(return_value=(["1162010100"], "홍대")),
        ),
        patch(
            "src.graph.crowdedness_node._fetch_population",
            AsyncMock(return_value=pop_row),
        ),
    ):
        result = await crowdedness_node({"processed_query": {"neighborhood": "홍대", "district": None}})

    blocks = result["response_blocks"]
    assert len(blocks) == 1
    assert "보통" in blocks[0]["prompt"]
    assert "홍대" in blocks[0]["prompt"]


# ---------------------------------------------------------------------------
# _resolve_dong_codes
# ---------------------------------------------------------------------------


async def test_resolve_dong_codes_neighborhood_pattern_match() -> None:
    """통용명 매핑(geo_mapping)으로 행정동 다건 매칭. area_name=neighborhood."""
    from src.graph.crowdedness_node import _resolve_dong_codes  # pyright: ignore[reportMissingImports]

    pool = MagicMock()
    pool.fetch = AsyncMock(
        return_value=[
            {"adm_dong_code": "1144051000"},
            {"adm_dong_code": "1144052000"},
            {"adm_dong_code": "1144064000"},
        ]
    )

    result = await _resolve_dong_codes(pool, "홍대", "마포구")
    assert result is not None
    codes, area_name = result
    assert area_name == "홍대"
    assert len(codes) == 3
    # 첫 호출이 NEIGHBORHOOD_TO_DONG_PATTERNS 경로 — 단 한 번에 매칭
    assert pool.fetch.call_count == 1


async def test_resolve_dong_codes_neighborhood_ilike_match() -> None:
    """통용명 매핑에 없는 입력 → ILIKE 직접 매칭으로 행정동명 부분일치."""
    from src.graph.crowdedness_node import _resolve_dong_codes  # pyright: ignore[reportMissingImports]

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[{"adm_dong_code": "1117060500"}, {"adm_dong_code": "1117060600"}])

    # "이태원"은 NEIGHBORHOOD_TO_DONG_PATTERNS에 없어 패턴 fetch 자체가 스킵됨
    # 직접 ILIKE 만 호출됨 → 단 한 번
    result = await _resolve_dong_codes(pool, "이태원", "용산구")
    assert result is not None
    codes, area_name = result
    assert area_name == "이태원"
    assert codes == ["1117060500", "1117060600"]
    assert pool.fetch.call_count == 1


async def test_resolve_dong_codes_dong_suffix_trim() -> None:
    """Gemini가 "성수" → "성수동"으로 정규화한 입력 — "동" 떼고 재시도해서 매칭.

    실제 동명("성수1가1동")은 "성수동" ILIKE에 매치 안 되지만 "성수" trim 후 매치.
    """
    from src.graph.crowdedness_node import _resolve_dong_codes  # pyright: ignore[reportMissingImports]

    pool = MagicMock()
    pool.fetch = AsyncMock(
        side_effect=[
            [],  # 1차: ILIKE '%성수동%' → 0건
            [
                {"adm_dong_code": "1120069000"},
                {"adm_dong_code": "1120070000"},
            ],  # 2차: ILIKE '%성수%' → 매치
        ]
    )

    result = await _resolve_dong_codes(pool, "성수동", None)
    assert result is not None
    codes, area_name = result
    # area_name은 사용자가 입력한 원본 보존 (Gemini 정규화 결과 그대로)
    assert area_name == "성수동"
    assert codes == ["1120069000", "1120070000"]
    assert pool.fetch.call_count == 2


async def test_resolve_dong_codes_district_fallback() -> None:
    """neighborhood 매칭 모두 실패 → district의 모든 동. area_name=district."""
    from src.graph.crowdedness_node import _resolve_dong_codes  # pyright: ignore[reportMissingImports]

    pool = MagicMock()
    pool.fetch = AsyncMock(
        side_effect=[
            [],  # ILIKE 직접 매칭 (neighborhood가 매핑에 없는 경우)
            [
                {"adm_dong_code": "1144051000"},
                {"adm_dong_code": "1144052000"},
            ],
        ]
    )

    result = await _resolve_dong_codes(pool, "우주정거장", "마포구")
    assert result is not None
    codes, area_name = result
    assert area_name == "마포구"
    assert len(codes) == 2


async def test_resolve_dong_codes_district_only() -> None:
    """neighborhood 없이 district만 → district의 모든 동."""
    from src.graph.crowdedness_node import _resolve_dong_codes  # pyright: ignore[reportMissingImports]

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[{"adm_dong_code": "1144051000"}])

    result = await _resolve_dong_codes(pool, None, "마포구")
    assert result is not None
    codes, area_name = result
    assert area_name == "마포구"
    assert codes == ["1144051000"]


async def test_resolve_dong_codes_none() -> None:
    """모든 매칭 실패 → None.

    "우주정거장"은 패턴 매핑에 없으므로 패턴 fetch는 스킵.
    직접 ILIKE 1회 + district fallback 1회 = 총 2회.
    """
    from src.graph.crowdedness_node import _resolve_dong_codes  # pyright: ignore[reportMissingImports]

    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=[[], []])

    result = await _resolve_dong_codes(pool, "우주정거장", "화성구")
    assert result is None
    assert pool.fetch.call_count == 2


# ---------------------------------------------------------------------------
# _fetch_population
# ---------------------------------------------------------------------------


async def test_fetch_population_empty_codes() -> None:
    """빈 dong_codes → None (DB 미호출)."""
    from src.graph.crowdedness_node import _fetch_population  # pyright: ignore[reportMissingImports]

    pool = MagicMock()
    pool.fetchrow = AsyncMock()

    result = await _fetch_population(pool, [], 14)
    assert result is None
    pool.fetchrow.assert_not_called()


async def test_fetch_population_sum() -> None:
    """다건 dong → SUM 결과 반환."""
    from src.graph.crowdedness_node import _fetch_population  # pyright: ignore[reportMissingImports]

    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        return_value={
            "current_pop": 50000,
            "base_date": date(2026, 5, 5),
            "avg_pop": 47000.0,
        }
    )

    result = await _fetch_population(pool, ["1144051000", "1144052000"], 14)
    assert result is not None
    assert result["current_pop"] == 50000
    assert result["avg_pop"] == 47000.0


async def test_fetch_population_no_data() -> None:
    """일치 데이터 없음 → None."""
    from src.graph.crowdedness_node import _fetch_population  # pyright: ignore[reportMissingImports]

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)

    result = await _fetch_population(pool, ["1144051000"], 14)
    assert result is None


# ---------------------------------------------------------------------------
# _build_crowdedness_blocks
# ---------------------------------------------------------------------------


async def test_build_blocks_match() -> None:
    """정상 케이스 — text_stream 블록 1개, 등급/인구 포함."""
    from src.graph.crowdedness_node import _build_crowdedness_blocks  # pyright: ignore[reportMissingImports]

    blocks = _build_crowdedness_blocks("혼잡", 5000, 3800.0, "홍대", date(2026, 5, 5))
    assert len(blocks) == 1
    b = blocks[0]
    assert b["type"] == "text_stream"
    assert "혼잡" in b["prompt"]
    assert "홍대" in b["prompt"]
    assert "5,000" in b["prompt"]


async def test_build_blocks_stale() -> None:
    """base_date 4일 전 — stale 경고 포함."""
    from src.graph.crowdedness_node import _build_crowdedness_blocks  # pyright: ignore[reportMissingImports]

    stale_date = date(2026, 5, 1)
    with patch("src.graph.crowdedness_node.datetime") as mock_dt:
        mock_dt.now.return_value.date.return_value = date(2026, 5, 5)
        blocks = _build_crowdedness_blocks("보통", 2000, 2100.0, "이태원", stale_date)

    assert "기준일" in blocks[0]["prompt"]
    assert "4일 전" in blocks[0]["prompt"]


async def test_build_blocks_no_match() -> None:
    """avg_pop=0 — 평균 '집계 불가' 표시."""
    from src.graph.crowdedness_node import _build_crowdedness_blocks  # pyright: ignore[reportMissingImports]

    blocks = _build_crowdedness_blocks("보통", 0, 0.0, "마포구", date(2026, 5, 5))
    assert "집계 불가" in blocks[0]["prompt"]


# ---------------------------------------------------------------------------
# crowdedness_node 진입점
# ---------------------------------------------------------------------------


async def test_node_skips_db_when_no_location() -> None:
    """neighborhood/district 모두 None → DB 미호출, 지역 미인식 텍스트."""
    from src.graph.crowdedness_node import crowdedness_node  # pyright: ignore[reportMissingImports]

    pool = MagicMock()
    pool.fetch = AsyncMock()
    pool.fetchrow = AsyncMock()

    with patch("src.db.postgres.get_pool", return_value=pool):
        result = await crowdedness_node({"processed_query": {}})

    pool.fetch.assert_not_called()
    pool.fetchrow.assert_not_called()
    assert "지역을 인식하지 못했습니다" in result["response_blocks"][0]["prompt"]


async def test_node_uses_kst_hour() -> None:
    """datetime monkeypatch — time_slot이 KST hour로 전달됨."""
    from src.graph.crowdedness_node import crowdedness_node  # pyright: ignore[reportMissingImports]

    pool = MagicMock()

    class FrozenDatetime:
        @staticmethod
        def now(tz: Any = None) -> Any:
            class _DT:
                hour = 14

                def date(self) -> date:
                    return date(2026, 5, 5)

            return _DT()

    captured_time_slot: list[int] = []

    async def mock_fetch_pop(p: Any, dong_codes: list[str], time_slot: int) -> dict[str, Any]:
        captured_time_slot.append(time_slot)
        return {
            "current_pop": 3000,
            "base_date": date(2026, 5, 5),
            "avg_pop": 2500.0,
        }

    with (
        patch("src.graph.crowdedness_node.datetime", FrozenDatetime),
        patch(
            "src.graph.crowdedness_node._resolve_dong_codes",
            AsyncMock(return_value=(["1144051000"], "홍대")),
        ),
        patch("src.graph.crowdedness_node._fetch_population", mock_fetch_pop),
        patch("src.db.postgres.get_pool", return_value=pool),
    ):
        await crowdedness_node({"processed_query": {"neighborhood": "홍대", "district": None}})

    assert captured_time_slot == [14]


async def test_node_neighborhood_preserves_label() -> None:
    """통용명("홍대") 입력 시 응답 area_name이 자치구로 광역화되지 않음."""
    from src.graph.crowdedness_node import crowdedness_node  # pyright: ignore[reportMissingImports]

    pool = MagicMock()
    pop_row: dict[str, Any] = {
        "current_pop": 60000,
        "base_date": date(2026, 5, 5),
        "avg_pop": 55000.0,
    }

    with (
        patch(
            "src.graph.crowdedness_node._resolve_dong_codes",
            AsyncMock(return_value=(["1144051000", "1144052000", "1144064000"], "홍대")),
        ),
        patch("src.graph.crowdedness_node._fetch_population", AsyncMock(return_value=pop_row)),
        patch("src.db.postgres.get_pool", return_value=pool),
    ):
        result = await crowdedness_node({"processed_query": {"neighborhood": "홍대", "district": "마포구"}})

    prompt = result["response_blocks"][0]["prompt"]
    assert "홍대의 현재 혼잡도" in prompt
    assert "마포구의 현재 혼잡도" not in prompt
    assert "60,000" in prompt


async def test_node_no_data_message() -> None:
    """resolution 성공 + 인구 데이터 없음 → '생활인구 데이터가 없습니다' 메시지."""
    from src.graph.crowdedness_node import crowdedness_node  # pyright: ignore[reportMissingImports]

    pool = MagicMock()

    with (
        patch(
            "src.graph.crowdedness_node._resolve_dong_codes",
            AsyncMock(return_value=(["1144051000"], "홍대")),
        ),
        patch("src.graph.crowdedness_node._fetch_population", AsyncMock(return_value=None)),
        patch("src.db.postgres.get_pool", return_value=pool),
    ):
        result = await crowdedness_node({"processed_query": {"neighborhood": "홍대", "district": None}})

    assert "생활인구 데이터가 없습니다" in result["response_blocks"][0]["prompt"]
