"""쿼리에서 구조화 필드를 정규식으로 추출 (LLM 호출 없이 명확 매칭).

`query_preprocessor` 노드가 LLM 응답 후 이 결과로 district/category/date/time을
보완한다. 모호 케이스는 비워두고 LLM 응답을 그대로 유지한다 — 정확도 우선.

재사용: 향후 P1-2(course_plan 카테고리 정규식), P1-4(calendar 시간 정규식)에서
같은 모듈 import.
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, timedelta
from typing import Any, Optional

from src.utils.geo_mapping import NEIGHBORHOOD_TO_DISTRICT  # pyright: ignore[reportMissingImports]

# 서울 25개 자치구 — 직접 매칭 (예: "강남구 카페")
SEOUL_DISTRICTS: frozenset[str] = frozenset(
    {
        "강남구",
        "강동구",
        "강북구",
        "강서구",
        "관악구",
        "광진구",
        "구로구",
        "금천구",
        "노원구",
        "도봉구",
        "동대문구",
        "동작구",
        "마포구",
        "서대문구",
        "서초구",
        "성동구",
        "성북구",
        "송파구",
        "양천구",
        "영등포구",
        "용산구",
        "은평구",
        "종로구",
        "중구",
        "중랑구",
    }
)

_DISTRICT_PATTERN = re.compile("(" + "|".join(SEOUL_DISTRICTS) + ")")

# 카테고리 정규식 — 명확 키워드만. value는 정규화된 카테고리명.
_CATEGORY_KEYWORDS: dict[str, str] = {
    "카페": "카페",
    "커피": "카페",
    "디저트": "카페",
    "음식점": "음식점",
    "맛집": "음식점",
    "식당": "음식점",
    "레스토랑": "음식점",
    "전시": "전시회",
    "전시회": "전시회",
    "축제": "축제",
    "페스티벌": "축제",
    "호텔": "호텔",
    "모텔": "모텔",
    "숙박": "숙박",
    "게스트하우스": "숙박",
    "펜션": "숙박",
}

# 긴 키워드 우선 매칭 — "전시회"가 "전시"보다 먼저 매치되게
_CATEGORY_PATTERN = re.compile("(" + "|".join(sorted(_CATEGORY_KEYWORDS.keys(), key=len, reverse=True)) + ")")

# 날짜 — 명확 패턴만. "다음 주", "주말" 등 모호 표현은 LLM 위임.
_DATE_KEYWORDS_RELATIVE: dict[str, int] = {
    "오늘": 0,
    "내일": 1,
    "모레": 2,
    "글피": 3,
}

_DATE_ISO_PATTERN = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_MONTH_DAY_PATTERN = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_DATE_MONTH_ONLY_PATTERN = re.compile(r"(?<!\d)(\d{1,2})\s*월(?!\s*\d)")

_TIME_HH_PATTERN = re.compile(r"(오전|오후)?\s*(\d{1,2})\s*시")
_TIME_KEYWORD_PATTERN = re.compile(r"(새벽|아침|점심|저녁|밤)")


def extract_regex_fields(query: str, today: Optional[date] = None) -> dict[str, Any]:
    """쿼리에서 정규식으로 추출 가능한 필드만 반환.

    매치되지 않은 필드는 dict에 포함되지 않아 LLM 결과를 보존한다.

    Args:
        query: 사용자 원본 쿼리.
        today: 기준 날짜. None이면 ``date.today()``.

    Returns:
        매치된 필드만 포함하는 dict (district/neighborhood/category/date_*/time_reference).
    """
    if not query:
        return {}
    # 입력 길이 가드 — 동네명 substring 루프가 O(k×n)이므로 비정상 입력으로부터 보호
    query = query[:500]
    result: dict[str, Any] = {}
    today_resolved = today or date.today()

    district_match = _DISTRICT_PATTERN.search(query)
    if district_match:
        result["district"] = district_match.group(1)
    else:
        # 가장 긴 동네명 우선 매칭 (부분매칭 폭주 방지)
        for key in sorted(NEIGHBORHOOD_TO_DISTRICT.keys(), key=len, reverse=True):
            if key in query:
                result["neighborhood"] = key
                result["district"] = NEIGHBORHOOD_TO_DISTRICT[key]
                break

    category_match = _CATEGORY_PATTERN.search(query)
    if category_match:
        result["category"] = _CATEGORY_KEYWORDS[category_match.group(1)]

    date_start, date_end, date_ref = _extract_date(query, today_resolved)
    if date_ref:
        result["date_reference"] = date_ref
    if date_start:
        result["date_start_resolved"] = date_start.isoformat()
    if date_end:
        result["date_end_resolved"] = date_end.isoformat()

    time_ref = _extract_time(query)
    if time_ref:
        result["time_reference"] = time_ref

    return result


def _extract_date(query: str, today: date) -> tuple[Optional[date], Optional[date], Optional[str]]:
    """명확 날짜 패턴만 추출. 모호 표현은 None 반환 → LLM 위임."""
    iso_match = _DATE_ISO_PATTERN.search(query)
    if iso_match:
        try:
            d = date(
                int(iso_match.group(1)),
                int(iso_match.group(2)),
                int(iso_match.group(3)),
            )
            return d, d, f"{iso_match.group(1)}-{iso_match.group(2)}-{iso_match.group(3)}"
        except ValueError:
            pass

    md_match = _DATE_MONTH_DAY_PATTERN.search(query)
    if md_match:
        try:
            month = int(md_match.group(1))
            day = int(md_match.group(2))
            year = today.year
            d = date(year, month, day)
            if d < today:
                d = date(year + 1, month, day)
            return d, d, f"{month}월 {day}일"
        except ValueError:
            pass

    for keyword, offset in _DATE_KEYWORDS_RELATIVE.items():
        if keyword in query:
            d = today + timedelta(days=offset)
            return d, d, keyword

    m_only_match = _DATE_MONTH_ONLY_PATTERN.search(query)
    if m_only_match:
        try:
            month = int(m_only_match.group(1))
            if 1 <= month <= 12:
                year = today.year if month >= today.month else today.year + 1
                start = date(year, month, 1)
                end = date(year, month, monthrange(year, month)[1])
                return start, end, f"{month}월"
        except ValueError:
            pass

    return None, None, None


def _extract_time(query: str) -> Optional[str]:
    """시간 표현 추출."""
    hh_match = _TIME_HH_PATTERN.search(query)
    if hh_match:
        prefix = hh_match.group(1) or ""
        hour = hh_match.group(2)
        try:
            h = int(hour)
            if 0 <= h <= 24:
                return (prefix + f" {h}시").strip()
        except ValueError:
            pass

    kw_match = _TIME_KEYWORD_PATTERN.search(query)
    if kw_match:
        return kw_match.group(1)

    return None
