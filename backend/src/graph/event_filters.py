"""EVENT 검색·추천 결과에서 시설 정보 row 제외 필터 (#193).

events 테이블에 "서울시시설대관" source의 row가 함께 적재돼있어 (별도 ETL),
"강남 행사 추천"에 "서초구 ○○ 대강당" 같은 시설 정보가 행사 카드로 표시되던 회귀를 차단.

전략 — 양쪽 안전망:
  1. source 블랙리스트 — 정확 매칭 (정식 source 라벨이 알려진 경우)
  2. title suffix — 시설명 끝 패턴 (대강당·다목적실·컨퍼런스룸 등)
"""

from __future__ import annotations

from typing import Any

# source 컬럼 값이 다음에 해당하면 행사 추천에서 제외 (정확 매칭, strip 적용).
_FACILITY_SOURCES: frozenset[str] = frozenset(
    {
        "서울시시설대관",
    }
)

# title 끝 패턴이 다음과 일치하면 시설 정보로 간주 (substring 아닌 endswith — 오탐 회피).
# 예: "서초구 ○○ 대강당" → 제외. "○○ 대강당에서 열리는 페스티벌" → 통과.
_FACILITY_TITLE_SUFFIXES: tuple[str, ...] = (
    "대강당",
    "다목적실",
    "컨퍼런스룸",
    "회의실",
    "강의실",
    "세미나실",
    "연회실",
)


def is_real_event(event: dict[str, Any]) -> bool:
    """행사로 간주할 수 있는지 검사 — 시설 정보면 False (#193).

    PG/OS/Naver 검색 결과 모두에 적용. EVENT_SEARCH·EVENT_RECOMMEND 공통.
    """
    source = (event.get("source") or "").strip()
    if source in _FACILITY_SOURCES:
        return False

    title = (event.get("title") or "").strip()
    if title.endswith(_FACILITY_TITLE_SUFFIXES):
        return False

    return True
