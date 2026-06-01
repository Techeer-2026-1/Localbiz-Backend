"""course_plan_node 단위 테스트.

순수 함수 검증: _parse_categories / _greedy_nn_route / _build_blocks / _haversine_m.
DB/OS/Gemini 의존성은 mock.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# 테스트 픽스처 데이터
# ---------------------------------------------------------------------------
_PLACES: list[dict[str, Any]] = [
    {
        "place_id": "p-001",
        "name": "광장시장",
        "category": "맛집",
        "address": "서울 종로구 창경궁로 88",
        "district": "종로구",
        "lat": 37.5701,
        "lng": 126.9996,
    },
    {
        "place_id": "p-002",
        "name": "익선동 한옥마을",
        "category": "관광지",
        "address": "서울 종로구 익선동",
        "district": "종로구",
        "lat": 37.5743,
        "lng": 126.9912,
    },
    {
        "place_id": "p-003",
        "name": "경복궁",
        "category": "관광지",
        "address": "서울 종로구 사직로 161",
        "district": "종로구",
        "lat": 37.5796,
        "lng": 126.977,
    },
]


# ---------------------------------------------------------------------------
# _parse_categories 테스트
# ---------------------------------------------------------------------------
async def test_parse_categories_plus() -> None:
    """+ 구분자로 카테고리 추출."""
    from src.graph.course_plan_node import _parse_categories  # pyright: ignore[reportMissingImports]

    result = _parse_categories("홍대 카페+맛집 코스", None)
    assert "카페" in result
    assert "맛집" in result


async def test_parse_categories_single_pq_fallback() -> None:
    """명시적 카테고리·상황 키워드 모두 없으면 pq_category 단일 사용."""
    from src.graph.course_plan_node import _parse_categories  # pyright: ignore[reportMissingImports]

    result = _parse_categories("강남 코스", "카페")
    assert result == ["카페"]


async def test_parse_categories_fallback() -> None:
    """카테고리 없으면 맛집 기본값."""
    from src.graph.course_plan_node import _parse_categories  # pyright: ignore[reportMissingImports]

    result = _parse_categories("좋은 곳 추천", None)
    assert result == ["맛집"]


# ---------------------------------------------------------------------------
# #175 상황 키워드 → 카테고리 조합 매핑
# ---------------------------------------------------------------------------
async def test_parse_categories_situation_date() -> None:
    """'데이트코스' 상황 키워드 → 시간대별 시퀀스 (음식점·공원·카페·쇼핑·술집) #177."""
    from src.graph.course_plan_node import _parse_categories  # pyright: ignore[reportMissingImports]

    result = _parse_categories("이번주 토요일 홍대 데이트코스 짜줘", None)
    assert result == ["음식점", "공원", "카페", "쇼핑", "술집"]


async def test_parse_categories_situation_overrides_pq_category() -> None:
    """상황 키워드가 pq_category(단일)보다 우선 — 데이트 의도면 시퀀스 적용 (#175/#177)."""
    from src.graph.course_plan_node import _parse_categories  # pyright: ignore[reportMissingImports]

    result = _parse_categories("강남 데이트 코스", "카페")
    assert result == ["음식점", "공원", "카페", "쇼핑", "술집"]


async def test_parse_categories_explicit_category_wins_over_situation() -> None:
    """쿼리에 명시적 카테고리가 있으면 상황 키워드보다 우선 (단일 카테고리 의도)."""
    from src.graph.course_plan_node import _parse_categories  # pyright: ignore[reportMissingImports]

    # "데이트"가 있어도 명시적 "카페"가 있으면 단일 카테고리 사용
    result = _parse_categories("홍대 카페 데이트 코스", None)
    assert "카페" in result
    # 시퀀스로 확장되지 않고 명시 카테고리만 — 단일 케이스는 Greedy NN 경로
    assert result == ["카페"]


async def test_parse_categories_situation_travel() -> None:
    """'여행' 상황 키워드 → 관광지·맛집·카페·쇼핑 시퀀스 (#177)."""
    from src.graph.course_plan_node import _parse_categories  # pyright: ignore[reportMissingImports]

    result = _parse_categories("부산 여행 코스 추천", None)
    assert result == ["관광지", "맛집", "카페", "쇼핑"]


# ---------------------------------------------------------------------------
# #175 _is_excluded_place_name — 교통결절점 제외 (오탐 회피 포함)
# ---------------------------------------------------------------------------
async def test_excluded_place_name_station_suffix() -> None:
    """'홍대역' 같은 역 끝 이름은 코스 stop으로 제외 (#175)."""
    from src.graph.course_plan_node import _is_excluded_place_name  # pyright: ignore[reportMissingImports]

    assert _is_excluded_place_name("홍대역") is True
    assert _is_excluded_place_name("강남역") is True
    assert _is_excluded_place_name("압구정로데오역") is True


async def test_excluded_place_name_substring_terminal() -> None:
    """터미널·정류장·공항은 substring 매칭으로 제외."""
    from src.graph.course_plan_node import _is_excluded_place_name  # pyright: ignore[reportMissingImports]

    assert _is_excluded_place_name("강남고속버스터미널") is True
    assert _is_excluded_place_name("홍대 버스정류장") is True
    assert _is_excluded_place_name("김포공항 라운지") is True


async def test_excluded_place_name_no_false_positive_yeoksam() -> None:
    """'역삼동'·'역사' 같은 단어가 끼어도 substring 오탐 없어야 함 (#175)."""
    from src.graph.course_plan_node import _is_excluded_place_name  # pyright: ignore[reportMissingImports]

    # endswith("역") 체크라 "역삼" 끝남 → False
    assert _is_excluded_place_name("역삼맛집") is False
    # "역사관" — 끝이 "관"이라 False
    assert _is_excluded_place_name("서울역사박물관") is False
    # 일반 음식점·카페는 그대로
    assert _is_excluded_place_name("홍대 감성카페") is False
    assert _is_excluded_place_name("") is False


async def test_excluded_place_name_normalizes_whitespace() -> None:
    """trailing/leading whitespace로 endswith 우회 방지 — CodeRabbit fix (#175)."""
    from src.graph.course_plan_node import _is_excluded_place_name  # pyright: ignore[reportMissingImports]

    assert _is_excluded_place_name("홍대역 ") is True  # trailing space
    assert _is_excluded_place_name(" 홍대역") is True  # leading space
    assert _is_excluded_place_name("  강남역  ") is True  # 양쪽
    assert _is_excluded_place_name("   ") is False  # 공백만


async def test_excluded_place_name_intersections() -> None:
    """교차로·사거리·로터리 — 코스 stop 부적합 (#177 운영 회귀)."""
    from src.graph.course_plan_node import _is_excluded_place_name  # pyright: ignore[reportMissingImports]

    assert _is_excluded_place_name("홍대삼거리") is True
    assert _is_excluded_place_name("강남사거리") is True
    assert _is_excluded_place_name("홍대입구역사거리R") is True  # substring "사거리"
    assert _is_excluded_place_name("종로 교차로") is True
    assert _is_excluded_place_name("광화문 로터리") is True
    assert _is_excluded_place_name("청량리오거리") is True


# ---------------------------------------------------------------------------
# #177 _pick_by_sequence — 시간대별 카테고리 시퀀스
# ---------------------------------------------------------------------------
async def test_pick_by_sequence_orders_by_categories() -> None:
    """시퀀스 순서대로 각 카테고리 1개씩 picked — 데이트 흐름 (#177)."""
    from src.graph.course_plan_node import _pick_by_sequence  # pyright: ignore[reportMissingImports]

    candidates = [
        {"place_id": "p1", "name": "카페A", "category": "카페"},
        {"place_id": "p2", "name": "맛집A", "category": "음식점"},
        {"place_id": "p3", "name": "술집A", "category": "술집"},
        {"place_id": "p4", "name": "공원A", "category": "공원"},
        {"place_id": "p5", "name": "쇼핑A", "category": "쇼핑"},
        {"place_id": "p6", "name": "카페B", "category": "카페"},
    ]
    sequence = ["음식점", "공원", "카페", "쇼핑", "술집"]
    result = _pick_by_sequence(candidates, sequence)

    names = [r["name"] for r in result]
    # 시퀀스 순서대로 각 카테고리 첫 매칭
    assert names == ["맛집A", "공원A", "카페A", "쇼핑A", "술집A"]


async def test_pick_by_sequence_skips_missing_categories_and_fills() -> None:
    """후보 없는 카테고리는 skip · max_stops까지 잔여 후보로 안전망 (#177)."""
    from src.graph.course_plan_node import _pick_by_sequence  # pyright: ignore[reportMissingImports]

    candidates = [
        {"place_id": "p1", "name": "맛집A", "category": "음식점"},
        {"place_id": "p2", "name": "맛집B", "category": "음식점"},
        {"place_id": "p3", "name": "카페A", "category": "카페"},
    ]
    sequence = ["음식점", "공원", "카페", "쇼핑", "술집"]
    result = _pick_by_sequence(candidates, sequence)

    names = [r["name"] for r in result]
    # 공원·쇼핑·술집은 후보 없음 → 시퀀스에서 음식점·카페만 picked
    # 그 후 잔여(맛집B)로 안전망 채움 — 총 3개
    assert names == ["맛집A", "카페A", "맛집B"]


async def test_pick_by_sequence_partial_category_match() -> None:
    """카테고리 substring 매칭 — '음식점' 시퀀스가 '한식음식점' 같은 세분 카테고리도 매칭."""
    from src.graph.course_plan_node import _pick_by_sequence  # pyright: ignore[reportMissingImports]

    candidates = [
        {"place_id": "p1", "name": "한식집", "category": "한식음식점"},
        {"place_id": "p2", "name": "분위기카페", "category": "디저트카페"},
    ]
    sequence = ["음식점", "카페"]
    result = _pick_by_sequence(candidates, sequence)

    names = [r["name"] for r in result]
    assert names == ["한식집", "분위기카페"]


async def test_pick_by_sequence_deduplicates_by_place_id() -> None:
    """같은 place_id는 시퀀스에서 한 번만 picked — 중복 제거 안전망."""
    from src.graph.course_plan_node import _pick_by_sequence  # pyright: ignore[reportMissingImports]

    # 같은 p1이 음식점과 카페 둘 다 매칭 가능한 케이스
    candidates = [
        {"place_id": "p1", "name": "복합공간", "category": "음식점/카페"},
        {"place_id": "p2", "name": "공원A", "category": "공원"},
        {"place_id": "p3", "name": "카페A", "category": "카페"},
    ]
    sequence = ["음식점", "공원", "카페"]
    result = _pick_by_sequence(candidates, sequence)

    names = [r["name"] for r in result]
    # p1이 음식점에 picked → 카페에는 다시 안 잡혀서 p3가 picked
    assert names == ["복합공간", "공원A", "카페A"]


# ---------------------------------------------------------------------------
# _haversine_m 테스트
# ---------------------------------------------------------------------------
async def test_haversine_distance() -> None:
    """광장시장 → 익선동 거리 계산 (약 800m)."""
    from src.graph.course_plan_node import _haversine_m  # pyright: ignore[reportMissingImports]

    dist = _haversine_m(37.5701, 126.9996, 37.5743, 126.9912)
    assert 500 < dist < 1500


# ---------------------------------------------------------------------------
# _greedy_nn_route 테스트
# ---------------------------------------------------------------------------
async def test_greedy_nn_route_order() -> None:
    """Greedy NN — 최근접 이웃 순서로 재배치."""
    from src.graph.course_plan_node import _greedy_nn_route  # pyright: ignore[reportMissingImports]

    route = _greedy_nn_route(_PLACES)
    assert len(route) == 3
    # 첫 번째는 그대로, 나머지는 거리 기반
    assert route[0]["place_id"] == "p-001"


async def test_greedy_nn_max_stops() -> None:
    """최대 5건으로 제한."""
    from src.graph.course_plan_node import _greedy_nn_route  # pyright: ignore[reportMissingImports]

    many = [
        {"place_id": f"p-{i}", "name": f"장소{i}", "lat": 37.5 + i * 0.01, "lng": 127.0 + i * 0.01} for i in range(10)
    ]
    route = _greedy_nn_route(many)
    assert len(route) == 5


async def test_greedy_nn_no_coords() -> None:
    """좌표 없는 후보만 있을 때."""
    from src.graph.course_plan_node import _greedy_nn_route  # pyright: ignore[reportMissingImports]

    no_coords = [{"place_id": f"p-{i}", "name": f"장소{i}", "lat": None, "lng": None} for i in range(3)]
    route = _greedy_nn_route(no_coords)
    assert len(route) == 3


# ---------------------------------------------------------------------------
# _build_blocks 테스트
# ---------------------------------------------------------------------------
async def test_build_blocks_normal() -> None:
    """정상 3-stop 코스 → text_stream + course + map_route 블록."""
    from src.graph.course_plan_node import _build_blocks  # pyright: ignore[reportMissingImports]

    stop_details = [
        {
            "order": 1,
            "arrival_time": "11:00",
            "duration_min": 60,
            "recommendation_reason": "전통시장",
            "transit_mode": "walk",
        },
        {
            "order": 2,
            "arrival_time": "12:12",
            "duration_min": 90,
            "recommendation_reason": "한옥마을",
            "transit_mode": "walk",
        },
        {
            "order": 3,
            "arrival_time": "14:00",
            "duration_min": 60,
            "recommendation_reason": "궁궐",
            "transit_mode": None,
        },
    ]

    blocks = await _build_blocks("종로 코스", _PLACES, "도심 코스", "3곳 코스", stop_details, "test-uuid")
    types = [b["type"] for b in blocks]

    assert "text_stream" in types
    assert "course" in types
    assert "map_route" in types

    # course 블록 검증
    course = next(b for b in blocks if b["type"] == "course")
    assert course["course_id"] == "test-uuid"
    assert len(course["stops"]) == 3
    assert course["stops"][0]["order"] == 1
    assert course["stops"][2]["transit_to_next"] is None  # 마지막 stop
    assert course["total_duration_min"] == course["total_stay_min"] + course["total_transit_min"]

    # map_route 블록 검증
    map_route = next(b for b in blocks if b["type"] == "map_route")
    assert map_route["course_id"] == "test-uuid"
    assert len(map_route["markers"]) == 3
    assert map_route["markers"][0]["order"] == 1
    assert len(map_route["polyline"]["segments"]) == 2  # 3 stops → 2 segments
    # GeoJSON [lng, lat] 순서
    assert map_route["polyline"]["segments"][0]["coordinates"][0][0] == _PLACES[0]["lng"]


async def test_build_blocks_empty() -> None:
    """빈 결과 → text_stream만 + 빈 course 블록."""
    from src.graph.course_plan_node import _build_blocks  # pyright: ignore[reportMissingImports]

    blocks = await _build_blocks("없는 코스", [], None, None, [], "test-uuid")
    types = [b["type"] for b in blocks]
    assert "text_stream" in types
    assert "course" in types


async def test_build_blocks_single_stop() -> None:
    """단일 stop → transit_to_next null, polyline segments 없음."""
    from src.graph.course_plan_node import _build_blocks  # pyright: ignore[reportMissingImports]

    single = [_PLACES[0]]
    details = [
        {"order": 1, "arrival_time": "11:00", "duration_min": 60, "recommendation_reason": "시장", "transit_mode": None}
    ]

    blocks = await _build_blocks("시장 코스", single, "시장 코스", "1곳", details, "test-uuid")
    course = next(b for b in blocks if b["type"] == "course")
    assert len(course["stops"]) == 1
    assert course["stops"][0]["transit_to_next"] is None

    map_route = next(b for b in blocks if b["type"] == "map_route")
    assert len(map_route["polyline"]["segments"]) == 0


# ---------------------------------------------------------------------------
# _llm_course_compose fallback 테스트
# ---------------------------------------------------------------------------
async def test_llm_compose_fallback() -> None:
    """Gemini 실패 시 None 반환 → _apply_fallback_times 적용."""
    from src.graph.course_plan_node import _apply_fallback_times  # pyright: ignore[reportMissingImports]

    details = _apply_fallback_times(_PLACES)
    assert len(details) == 3
    assert details[0]["arrival_time"] == "11:00"
    assert details[0]["duration_min"] == 60
    assert details[2]["transit_mode"] is None  # 마지막


async def test_llm_compose_api_error() -> None:
    """Gemini API 에러 → (None, None, []) 반환."""
    from src.graph.course_plan_node import _llm_course_compose  # pyright: ignore[reportMissingImports]

    mock_settings = type("Settings", (), {"gemini_llm_api_key": "fake-key"})()

    with (
        patch("src.config.get_settings", return_value=mock_settings),
        patch("langchain_google_genai.ChatGoogleGenerativeAI") as mock_llm_cls,
    ):
        mock_llm = AsyncMock()
        mock_llm.ainvoke.side_effect = Exception("API error")
        mock_llm_cls.return_value = mock_llm

        title, desc, details = await _llm_course_compose(_PLACES, "테스트")

    assert title is None
    assert desc is None
    assert details == []
