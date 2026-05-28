"""event_filters.is_real_event 단위 테스트 (#193).

events 테이블에 적재된 "서울시시설대관" source / "○○ 대강당" 같은 시설 row가
EVENT 검색·추천 결과에 행사로 표시되던 회귀를 차단하는지 검증.
"""

from __future__ import annotations


def test_is_real_event_normal_event_passes() -> None:
    """정상 행사 row는 통과."""
    from src.graph.event_filters import is_real_event  # pyright: ignore[reportMissingImports]

    event = {
        "title": "강남페스티벌",
        "source": "서울시문화축제",
    }
    assert is_real_event(event) is True


def test_is_real_event_facility_source_blocked() -> None:
    """source가 '서울시시설대관'이면 행사로 간주하지 않음 — #193 핵심."""
    from src.graph.event_filters import is_real_event  # pyright: ignore[reportMissingImports]

    event = {
        "title": "서초구 내곡열린문화센터 4층 대강당",
        "source": "서울시시설대관",
    }
    assert is_real_event(event) is False


def test_is_real_event_facility_source_with_whitespace_blocked() -> None:
    """source 앞뒤 공백도 정확 매칭으로 차단."""
    from src.graph.event_filters import is_real_event  # pyright: ignore[reportMissingImports]

    event = {
        "title": "어느 시설",
        "source": "  서울시시설대관  ",
    }
    assert is_real_event(event) is False


def test_is_real_event_title_suffix_blocked() -> None:
    """title이 시설명 끝 패턴이면 차단 — source 모름에도 방어."""
    from src.graph.event_filters import is_real_event  # pyright: ignore[reportMissingImports]

    for title in [
        "서초구 서초2동 대강당",
        "서초구 내곡열린문화센터 지하2층 다목적실",
        "서초문화예술회관 4층 북카페 컨퍼런스룸1",
        "○○ 회의실",
        "강남구청 5층 강의실",
        "△△ 세미나실",
        "□□ 연회실",
    ]:
        # 끝 글자가 _FACILITY_TITLE_SUFFIXES 중 하나로 끝나는지 단순 endswith
        # → 위 7개 중 substring "컨퍼런스룸1"은 끝이 "1"이라 통과. 의도된 동작.
        # 단순 endswith만 — substring 매칭은 오탐 크니까 의도적으로 좁게.
        event = {"title": title, "source": "unknown"}
        result = is_real_event(event)
        if title.endswith(("대강당", "다목적실", "컨퍼런스룸", "회의실", "강의실", "세미나실", "연회실")):
            assert result is False, f"기대: 차단, 실제: 통과 — {title!r}"
        else:
            # 끝 글자가 매칭되지 않는 경우(예: 컨퍼런스룸1) — 통과
            assert result is True, f"기대: 통과, 실제: 차단 — {title!r}"


def test_is_real_event_real_event_with_facility_in_middle_passes() -> None:
    """'○○ 대강당에서 열리는 콘서트' 같은 진짜 행사 title은 통과 (오탐 회피)."""
    from src.graph.event_filters import is_real_event  # pyright: ignore[reportMissingImports]

    event = {
        "title": "서울시청 대강당에서 열리는 K-POP 콘서트",
        "source": "서울시문화행사",
    }
    # title이 "콘서트"로 끝나니까 endswith 매칭 안 되고 통과
    assert is_real_event(event) is True


def test_is_real_event_empty_dict_passes() -> None:
    """빈 dict는 통과 (None 처리 안전망)."""
    from src.graph.event_filters import is_real_event  # pyright: ignore[reportMissingImports]

    assert is_real_event({}) is True


def test_is_real_event_none_fields_passes() -> None:
    """source/title이 None인 row도 안전 처리."""
    from src.graph.event_filters import is_real_event  # pyright: ignore[reportMissingImports]

    event = {"title": None, "source": None}
    assert is_real_event(event) is True


def test_is_real_event_naver_source_passes() -> None:
    """Naver fallback의 source는 차단되지 않음."""
    from src.graph.event_filters import is_real_event  # pyright: ignore[reportMissingImports]

    event = {
        "title": "주말에 가볼 만한 강남 행사",
        "source": "naver_blog",
    }
    assert is_real_event(event) is True
