"""CROWDEDNESS 노드 — population_stats 기반 혼잡도 분석. Phase: P1."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from src.graph._tracing import traced_node  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

_STALE_THRESHOLD_DAYS = 3
_LEVEL_MAP: dict[str, str] = {"한산": "low", "보통": "medium", "혼잡": "high"}


def _classify_level(ratio: float) -> str:
    """비율 기준 혼잡도 등급 반환. avg=0 케이스는 호출 전 처리."""
    if ratio < 0.7:
        return "한산"
    if ratio < 1.2:
        return "보통"
    return "혼잡"


async def _resolve_dong_codes(
    pool: Any,
    neighborhood: Optional[str],
    district: Optional[str],
) -> Optional[tuple[list[str], str]]:
    """통용 지명/자치구를 행정동 코드 리스트 + 표시명으로 해석한다.

    우선순위:
        1. neighborhood가 통용 지명 매핑(geo_mapping)에 있으면 패턴 ILIKE로 행정동 매칭
        2. neighborhood가 실제 행정동명 일부와 일치하면 ILIKE 매칭
        3. 위 두 가지 실패 시 district의 모든 행정동
        4. neighborhood 없고 district만 있을 때도 district의 모든 행정동

    Returns:
        ([adm_dong_code, ...], 표시명) 또는 None.
        표시명은 사용자가 물은 단위(neighborhood 또는 district)와 일치.
    """
    from src.utils.geo_mapping import resolve_dong_patterns  # pyright: ignore[reportMissingImports]

    if neighborhood:
        patterns = resolve_dong_patterns(neighborhood)
        if patterns:
            like_patterns = [f"%{p}%" for p in patterns]
            rows = await pool.fetch(
                """
                SELECT adm_dong_code
                FROM administrative_districts
                WHERE adm_dong_name ILIKE ANY($1::text[])
                  AND ($2::text IS NULL OR district = $2)
                """,
                like_patterns,
                district,
            )
            if rows:
                return [r["adm_dong_code"] for r in rows], neighborhood

        rows = await pool.fetch(
            """
            SELECT adm_dong_code
            FROM administrative_districts
            WHERE adm_dong_name ILIKE $1
              AND ($2::text IS NULL OR district = $2)
            """,
            f"%{neighborhood}%",
            district,
        )
        if rows:
            return [r["adm_dong_code"] for r in rows], neighborhood

        # Gemini 정규화로 "성수" → "성수동" 같은 변형 입력 방어 — 끝 "동" 떼고 재시도.
        # 실제 동명은 "성수1가1동"이라 "%성수동%"은 0건이지만 "%성수%"는 매치.
        if neighborhood.endswith("동") and len(neighborhood) > 1:
            trimmed = neighborhood[:-1]
            rows = await pool.fetch(
                """
                SELECT adm_dong_code
                FROM administrative_districts
                WHERE adm_dong_name ILIKE $1
                  AND ($2::text IS NULL OR district = $2)
                """,
                f"%{trimmed}%",
                district,
            )
            if rows:
                return [r["adm_dong_code"] for r in rows], neighborhood

    if district:
        rows = await pool.fetch(
            """
            SELECT adm_dong_code
            FROM administrative_districts
            WHERE district = $1
            """,
            district,
        )
        if rows:
            return [r["adm_dong_code"] for r in rows], district

    return None


async def _fetch_population(
    pool: Any,
    dong_codes: list[str],
    time_slot: int,
) -> Optional[dict[str, Any]]:
    """주어진 행정동 코드들의 현재 시간대 인구 SUM + 30일 동일 시간대 평균.

    base_date 는 population_stats 의 최신 일자.
    avg_pop 은 동일 dong 집합·동일 time_slot 의 일자별 SUM의 30일 평균.
    단, 최신일(base_date) 자체는 baseline 에서 제외해 현재 스냅샷 오염을 막는다.
    """
    if not dong_codes:
        return None

    row = await pool.fetchrow(
        """
        WITH latest AS (SELECT MAX(base_date) AS d FROM population_stats)
        SELECT
            COALESCE(SUM(cur.total_pop), 0) AS current_pop,
            l.d AS base_date,
            COALESCE(
                (SELECT AVG(daily_total)
                 FROM (
                     SELECT SUM(p2.total_pop) AS daily_total
                     FROM population_stats p2, latest
                     WHERE p2.adm_dong_code = ANY($1::text[])
                       AND p2.time_slot = $2
                       AND p2.base_date >= latest.d - INTERVAL '30 days'
                       AND p2.base_date < latest.d
                     GROUP BY p2.base_date
                 ) dt),
                0
            ) AS avg_pop
        FROM population_stats cur, latest l
        WHERE cur.adm_dong_code = ANY($1::text[])
          AND cur.time_slot = $2
          AND cur.base_date = l.d
        GROUP BY l.d
        """,
        dong_codes,
        time_slot,
    )
    if not row or row["base_date"] is None:
        return None
    return dict(row)


def _build_crowdedness_blocks(
    level: str,
    current_pop: int,
    avg_pop: float,
    area_name: str,
    base_date: Any,
) -> list[dict[str, Any]]:
    """text_stream 블록 생성. stale 데이터 경고 포함."""
    from datetime import date as _date

    stale_note = ""
    if isinstance(base_date, _date):
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        delta = (today - base_date).days
        if delta >= _STALE_THRESHOLD_DAYS:
            stale_note = f" (※ 기준일: {base_date}, {delta}일 전 데이터입니다)"

    avg_str = f"{avg_pop:,.0f}명" if avg_pop > 0 else "집계 불가"
    prompt = (
        f"{area_name}의 현재 혼잡도는 **{level}**입니다{stale_note}. "
        f"현재 생활인구 {current_pop:,}명 (30일 동일 시간대 평균 {avg_str} 기준)."
    )
    return [
        {
            "type": "text_stream",
            "system": "당신은 서울 로컬 라이프 AI 챗봇입니다. 자기소개나 인사로 시작하지 말고 바로 본론으로 답변하세요. 제공된 혼잡도 정보를 바탕으로 자연스럽고 친근하게 설명해주세요.",
            "prompt": prompt,
        }
    ]


async def fetch_congestion_by_district(
    pool: Any,
    district: str,
) -> Optional[dict[str, Any]]:
    """district 단위 혼잡도 조회. places 블록 congestion 필드용 (area_proxy).

    Returns:
        {"level": "low"|"medium"|"high", "updated_at": ISO date str, "source": "area_proxy"}
        or None when population data is unavailable.
    """
    time_slot: int = datetime.now(ZoneInfo("Asia/Seoul")).hour
    resolution = await _resolve_dong_codes(pool, None, district)
    if resolution is None:
        return None
    dong_codes, _ = resolution
    pop = await _fetch_population(pool, dong_codes, time_slot)
    if pop is None:
        return None
    current_pop = int(pop.get("current_pop") or 0)
    avg_pop = float(pop.get("avg_pop") or 0)
    # baseline 없으면 places 카드에 misleading 등급 붙이지 않음 — congestion 필드 자체 생략.
    if avg_pop == 0:
        return None
    level_ko = _classify_level(current_pop / avg_pop)
    base_date = pop.get("base_date")
    updated_at = base_date.isoformat() if base_date is not None else ""
    return {
        "level": _LEVEL_MAP.get(level_ko, "medium"),
        "updated_at": updated_at,
        "source": "area_proxy",
    }


@traced_node("crowdedness")
async def crowdedness_node(state: dict[str, Any]) -> dict[str, Any]:
    """CROWDEDNESS intent 처리 노드. Phase: P1."""
    from src.db.postgres import get_pool  # pyright: ignore[reportMissingImports]

    processed: dict[str, Any] = state.get("processed_query") or {}
    neighborhood: Optional[str] = processed.get("neighborhood")
    district: Optional[str] = processed.get("district")

    def _no_location() -> dict[str, Any]:
        return {
            "response_blocks": [
                {
                    "type": "text_stream",
                    "system": "당신은 서울 로컬 라이프 AI 챗봇입니다. 자기소개나 인사로 시작하지 말고 바로 본론으로 답변하세요.",
                    "prompt": "질문하신 지역을 인식하지 못했습니다. 지역명을 포함해 다시 질문해 주세요.",
                }
            ]
        }

    if not neighborhood and not district:
        return _no_location()

    pool = get_pool()
    time_slot: int = datetime.now(ZoneInfo("Asia/Seoul")).hour

    resolution = await _resolve_dong_codes(pool, neighborhood, district)
    if resolution is None:
        return _no_location()
    dong_codes, area_name = resolution

    pop = await _fetch_population(pool, dong_codes, time_slot)

    if pop is None:
        return {
            "response_blocks": [
                {
                    "type": "text_stream",
                    "system": "당신은 서울 로컬 라이프 AI 챗봇입니다. 자기소개나 인사로 시작하지 말고 바로 본론으로 답변하세요.",
                    "prompt": "해당 지역의 생활인구 데이터가 없습니다.",
                }
            ]
        }

    current_pop = int(pop.get("current_pop") or 0)
    avg_pop = float(pop.get("avg_pop") or 0)
    base_date = pop.get("base_date")

    # 30일 동일 시간대 baseline이 없으면 비율 비교 불가 — "보통" 폴백 대신 정직하게 미산정 안내.
    if avg_pop == 0:
        return {
            "response_blocks": [
                {
                    "type": "text_stream",
                    "system": "당신은 서울 로컬 라이프 AI 챗봇입니다. 자기소개나 인사로 시작하지 말고 바로 본론으로 답변하세요.",
                    "prompt": (
                        f"{area_name} 현재 생활인구는 {current_pop:,}명입니다. "
                        f"다만 동일 시간대 30일 비교 데이터가 부족해 혼잡도 등급은 산정하지 못했습니다."
                    ),
                }
            ]
        }

    level = _classify_level(current_pop / avg_pop)
    return {"response_blocks": _build_crowdedness_blocks(level, current_pop, avg_pop, area_name, base_date)}
