"""query_regex 단위 테스트.

LLM 호출 없이 정규식 단독 동작을 검증.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.utils.query_regex import extract_regex_fields  # pyright: ignore[reportMissingImports]

_TODAY = date(2026, 5, 14)  # 목요일


@pytest.mark.parametrize(
    "query, expected_district",
    [
        ("강남구 분위기 좋은 카페", "강남구"),
        ("마포구 전시회", "마포구"),
        ("종로구 한식 맛집", "종로구"),
        ("서초구 호텔", "서초구"),
        ("중구 명동 추천", "중구"),
    ],
)
def test_district_direct_match(query: str, expected_district: str) -> None:
    result = extract_regex_fields(query, today=_TODAY)
    assert result.get("district") == expected_district


@pytest.mark.parametrize(
    "query, expected_district, expected_neighborhood",
    [
        ("홍대 카페 추천", "마포구", "홍대"),
        ("이태원 맛집", "용산구", "이태원"),
        ("성수동 전시", "성동구", "성수동"),
        ("강남역 근처", "강남구", "강남역"),
    ],
)
def test_neighborhood_to_district(query: str, expected_district: str, expected_neighborhood: str) -> None:
    result = extract_regex_fields(query, today=_TODAY)
    assert result.get("district") == expected_district
    assert result.get("neighborhood") == expected_neighborhood


@pytest.mark.parametrize(
    "query, expected_category",
    [
        ("강남 카페", "카페"),
        ("홍대 디저트", "카페"),
        ("종로 맛집", "음식점"),
        ("이태원 레스토랑", "음식점"),
        ("DDP 전시회", "전시회"),
        ("한강 축제", "축제"),
        ("강남 호텔", "호텔"),
        ("종로 게스트하우스", "숙박"),
    ],
)
def test_category(query: str, expected_category: str) -> None:
    result = extract_regex_fields(query, today=_TODAY)
    assert result.get("category") == expected_category


def test_category_long_first() -> None:
    """'전시회'가 '전시'보다 우선 매치되어야 한다."""
    result = extract_regex_fields("이번 전시회", today=_TODAY)
    assert result.get("category") == "전시회"


def test_date_iso() -> None:
    result = extract_regex_fields("2026-12-25 행사", today=_TODAY)
    assert result.get("date_start_resolved") == "2026-12-25"
    assert result.get("date_end_resolved") == "2026-12-25"


def test_date_month_day_future() -> None:
    """미래 N월 N일 — 같은 해."""
    result = extract_regex_fields("8월 15일 행사", today=_TODAY)
    assert result.get("date_start_resolved") == "2026-08-15"


def test_date_month_day_past_rolls_to_next_year() -> None:
    """과거 N월 N일은 내년으로 보정."""
    result = extract_regex_fields("3월 1일 일정", today=_TODAY)
    assert result.get("date_start_resolved") == "2027-03-01"


@pytest.mark.parametrize(
    "query, expected_offset, expected_ref",
    [
        ("오늘 카페", 0, "오늘"),
        ("내일 전시", 1, "내일"),
        ("모레 약속", 2, "모레"),
    ],
)
def test_date_relative(query: str, expected_offset: int, expected_ref: str) -> None:
    result = extract_regex_fields(query, today=_TODAY)
    expected = (_TODAY + timedelta(days=expected_offset)).isoformat()
    assert result.get("date_reference") == expected_ref
    assert result.get("date_start_resolved") == expected


def test_date_month_only() -> None:
    """'3월' 단독 — 해당 월 전체."""
    result = extract_regex_fields("3월 축제", today=_TODAY)
    assert result.get("date_start_resolved") == "2027-03-01"
    assert result.get("date_end_resolved") == "2027-03-31"


def test_date_ambiguous_returns_nothing() -> None:
    """'다음 주' '주말' 같은 모호 표현은 정규식이 채우지 않음 (LLM 위임)."""
    result = extract_regex_fields("다음 주 카페", today=_TODAY)
    assert "date_start_resolved" not in result
    assert "date_reference" not in result


@pytest.mark.parametrize(
    "query, expected_time",
    [
        ("오후 2시 약속", "오후 2시"),
        ("오전 9시 회의", "오전 9시"),
        ("3시에 만나자", "3시"),
        ("저녁에 보자", "저녁"),
        ("아침 일찍", "아침"),
    ],
)
def test_time(query: str, expected_time: str) -> None:
    result = extract_regex_fields(query, today=_TODAY)
    assert result.get("time_reference") == expected_time


def test_combined_fields() -> None:
    result = extract_regex_fields("내일 오후 2시 홍대 카페", today=_TODAY)
    assert result.get("district") == "마포구"
    assert result.get("neighborhood") == "홍대"
    assert result.get("category") == "카페"
    assert result.get("date_reference") == "내일"
    assert result.get("date_start_resolved") == "2026-05-15"
    assert result.get("time_reference") == "오후 2시"


def test_empty_query() -> None:
    assert extract_regex_fields("", today=_TODAY) == {}


def test_no_match_returns_empty() -> None:
    """매치되지 않는 쿼리는 빈 dict — LLM 응답을 전부 보존."""
    result = extract_regex_fields("그냥 추천해줘", today=_TODAY)
    assert result == {}


def test_invalid_month_day() -> None:
    """잘못된 월일은 매치되지 않음 (예외 처리)."""
    result = extract_regex_fields("13월 32일", today=_TODAY)
    assert "date_start_resolved" not in result


def test_explicit_date_wins_over_relative_keyword() -> None:
    """'오늘 8월 15일' 같은 충돌 케이스 — 명시 N월 N일이 우선."""
    result = extract_regex_fields("오늘 8월 15일 축제", today=_TODAY)
    assert result.get("date_reference") == "8월 15일"
    assert result.get("date_start_resolved") == "2026-08-15"


def test_iso_date_wins_over_relative_keyword() -> None:
    """ISO 날짜가 상대 키워드보다 우선."""
    result = extract_regex_fields("내일 2026-08-15 행사", today=_TODAY)
    assert result.get("date_start_resolved") == "2026-08-15"


def test_long_input_truncated_safely() -> None:
    """500자 초과 입력에서도 정상 동작."""
    long_query = "강남 카페 " + "x" * 1000
    result = extract_regex_fields(long_query, today=_TODAY)
    assert result.get("district") == "강남구"
    assert result.get("category") == "카페"
