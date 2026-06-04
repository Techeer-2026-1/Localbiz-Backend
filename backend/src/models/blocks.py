"""SSE 응답 블록 16종 — Pydantic 모델 (기획서 section 4.5 권위).

16종 콘텐츠 블록 (messages.blocks JSON에 저장):
  intent / text / text_stream / place / places / events / course /
  map_markers / map_route / chart / calendar / references /
  analysis_sources / disambiguation / done / error

SSE 제어 이벤트 (런타임 한정, messages 미저장):
  - status: 노드 전환 진행 표시 (StatusFrame으로 별도 정의)
  - done_partial: multi-intent 구분자

변경 시 PM 합의 + 기획서 동기화 필수.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 1. intent — 분류 결과 통보
# ---------------------------------------------------------------------------
class IntentBlock(BaseModel):
    """사용자 쿼리의 intent 분류 결과."""

    type: str = "intent"
    intent: str
    confidence: Optional[float] = None


# ---------------------------------------------------------------------------
# 2. text — 완성된 텍스트 응답 (비스트리밍)
# ---------------------------------------------------------------------------
class TextBlock(BaseModel):
    """단일 텍스트 응답. 스트리밍 완료 후 최종본 저장 용도로도 사용."""

    type: str = "text"
    content: str = ""


# ---------------------------------------------------------------------------
# 3. text_stream — 토큰 단위 스트리밍
# ---------------------------------------------------------------------------
class TextStreamBlock(BaseModel):
    """Gemini 스트리밍 토큰. delta를 순차 전송."""

    type: str = "text_stream"
    delta: str = ""


# ---------------------------------------------------------------------------
# 4. place — 단일 장소 카드
# ---------------------------------------------------------------------------
class CongestionInfo(BaseModel):
    """동네 단위 혼잡도 (population_stats district fallback, area_proxy)."""

    level: str  # "low" | "medium" | "high"
    updated_at: str  # ISO date string of population_stats base_date
    source: Optional[str] = None  # "area_proxy"


class PlaceBlock(BaseModel):
    """단일 장소 상세 정보."""

    type: str = "place"
    place_id: str  # UUID (VARCHAR(36)), 불변식 #1
    name: str = ""
    category: Optional[str] = None
    address: Optional[str] = None
    district: Optional[str] = None  # 의도적 비정규화, 불변식 #5
    lat: Optional[float] = None
    lng: Optional[float] = None
    rating: Optional[float] = None
    image_url: Optional[str] = None
    summary: Optional[str] = None  # LLM 요약 (선택)
    naver_map_url: Optional[str] = None  # 런타임 생성 (네이버 지도 검색)
    kakao_map_url: Optional[str] = None  # 런타임 생성 (카카오맵 검색)
    congestion: Optional[CongestionInfo] = None


# ---------------------------------------------------------------------------
# 5. places — 장소 리스트
# ---------------------------------------------------------------------------
class PlacesBlock(BaseModel):
    """장소 목록 (검색/추천 결과)."""

    type: str = "places"
    items: list[PlaceBlock] = Field(default_factory=list)
    total_count: Optional[int] = None


# ---------------------------------------------------------------------------
# 6. events — 행사 리스트
# ---------------------------------------------------------------------------
class EventItem(BaseModel):
    """단일 행사 정보."""

    event_id: str  # UUID (VARCHAR(36)), 불변식 #1
    title: str = ""
    description: Optional[str] = None  # per-event LLM 소개 (SSE 응답 개선)
    district: Optional[str] = None  # 의도적 비정규화
    place_name: Optional[str] = None  # 의도적 비정규화
    address: Optional[str] = None  # 의도적 비정규화
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    category: Optional[str] = None
    image_url: Optional[str] = None
    homepage_url: Optional[str] = None


class EventsBlock(BaseModel):
    """행사 목록."""

    type: str = "events"
    items: list[EventItem] = Field(default_factory=list)
    total_count: Optional[int] = None


# ---------------------------------------------------------------------------
# 7. course — 코스 타임라인 (api-course-quick-spec.md 준수, flat 패턴)
# ---------------------------------------------------------------------------
class CoursePlaceInfo(BaseModel):
    """코스 stop 내 장소 정보 — 자체 완결 (FE 추가 조회 불필요)."""

    place_id: str
    name: str = ""
    category: Optional[str] = None
    category_label: Optional[str] = None
    address: Optional[str] = None
    district: Optional[str] = None
    location: Optional[dict[str, float]] = None  # {"lat": ..., "lng": ...}
    rating: Optional[float] = None
    summary: Optional[str] = None
    photo_url: Optional[str] = None  # Phase 1 생략
    photo_attribution: Optional[str] = None  # Phase 1 생략
    business_hours_today: Optional[str] = None  # Phase 1 생략
    is_open_now: Optional[bool] = None  # Phase 1 생략
    booking_url: Optional[str] = None  # Phase 1 생략
    congestion: Optional[CongestionInfo] = None  # 동네 단위 area_proxy (불변식 #5)


class CourseTransit(BaseModel):
    """경유지 간 이동 정보."""

    mode: str = "walk"  # walk | subway | bus | taxi
    mode_ko: str = "도보"
    distance_m: int = 0
    duration_min: int = 0


class CourseStop(BaseModel):
    """코스 내 단일 정거장 — 1-indexed order."""

    order: int
    arrival_time: Optional[str] = None  # "HH:mm"
    duration_min: Optional[int] = None
    place: CoursePlaceInfo
    transit_to_next: Optional[CourseTransit] = None  # 마지막 stop은 None
    recommendation_reason: Optional[str] = None


class CourseBlock(BaseModel):
    """코스 타임라인 (COURSE_PLAN intent). flat 패턴 — content wrapper 미적용."""

    type: str = "course"
    course_id: Optional[str] = None  # UUID4
    title: Optional[str] = None
    description: Optional[str] = None
    total_distance_m: Optional[int] = None
    total_duration_min: Optional[int] = None
    total_stay_min: Optional[int] = None
    total_transit_min: Optional[int] = None
    stops: list[CourseStop] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 8. map_markers — Leaflet 마커 (places 용)
# ---------------------------------------------------------------------------
class MarkerItem(BaseModel):
    """지도 마커 하나."""

    place_id: str
    lat: float
    lng: float
    label: Optional[str] = None


class MapMarkersBlock(BaseModel):
    """Leaflet 마커 목록."""

    type: str = "map_markers"
    markers: list[MarkerItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 9. map_route — 코스 경로 시각화 (api-course-quick-spec.md 준수)
# ---------------------------------------------------------------------------
class RouteMarker(BaseModel):
    """경로 마커 — course.stops[].order와 매칭."""

    order: int
    position: dict[str, float]  # {"lat": ..., "lng": ...}
    label: str = ""
    category: Optional[str] = None


class RouteSegment(BaseModel):
    """경로 구간 — straight(Phase 1) / road(Phase 2)."""

    from_order: int
    to_order: int
    mode: str = "walk"
    coordinates: list[list[float]] = Field(default_factory=list)  # [[lng, lat], ...] GeoJSON
    distance_m: Optional[int] = None
    duration_min: Optional[int] = None


class MapRouteBlock(BaseModel):
    """코스 경로 시각화. course_id로 CourseBlock과 연결."""

    type: str = "map_route"
    course_id: Optional[str] = None
    bounds: Optional[dict[str, dict[str, float]]] = None  # {"sw": {lat,lng}, "ne": {lat,lng}}
    center: Optional[dict[str, float]] = None  # {"lat": ..., "lng": ...}
    suggested_zoom: Optional[int] = None
    markers: list[RouteMarker] = Field(default_factory=list)
    polyline: Optional[dict[str, Any]] = None  # {"type": "straight", "segments": [...]}


# ---------------------------------------------------------------------------
# 10. chart — 레이더 차트 (REVIEW_COMPARE / ANALYSIS)
# ---------------------------------------------------------------------------
class ChartPlaceScore(BaseModel):
    """레이더 차트 장소 1개 — 기획서 v2 SSE L157."""

    name: str
    scores: dict[str, float] = Field(default_factory=dict)  # 6지표 키 그대로 (불변식 #6)


class ChartBlock(BaseModel):
    """레이더 차트 (6 지표 비교 — REVIEW_COMPARE)."""

    type: str = "chart"
    chart_type: str = "radar"
    places: list[ChartPlaceScore] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 11. calendar — Google Calendar event 카드
# ---------------------------------------------------------------------------
class CalendarBlock(BaseModel):
    """Google Calendar 이벤트 생성 결과."""

    type: str = "calendar"
    event_title: Optional[str] = None  # API 명세서 필드명 일치 (title → event_title)
    start_time: Optional[str] = None  # ISO 8601 + KST 오프셋
    end_time: Optional[str] = None
    location: Optional[str] = None
    calendar_link: Optional[str] = None
    status: Optional[str] = None  # "created" | "failed"


# ---------------------------------------------------------------------------
# 12. references — 추천 사유 인용 (PLACE_RECOMMEND)
# ---------------------------------------------------------------------------
class ReferenceItem(BaseModel):
    """인용 출처 하나."""

    source_type: str = ""  # "review" | "blog" | "official"
    source_id: Optional[str] = None
    snippet: str = ""
    url: Optional[str] = None


class ReferencesBlock(BaseModel):
    """추천 사유 인용 목록."""

    type: str = "references"
    items: list[ReferenceItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 13. analysis_sources — 분석 근거 출처 (ANALYSIS / REVIEW_COMPARE)
# ---------------------------------------------------------------------------
class AnalysisSourcesBlock(BaseModel):
    """분석에 사용된 데이터 출처."""

    type: str = "analysis_sources"
    review_count: Optional[int] = None
    blog_count: Optional[int] = None
    official_count: Optional[int] = None
    sources: list[ReferenceItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 14. disambiguation — 동명/다중 후보 선택 UI
# ---------------------------------------------------------------------------
class DisambiguationCandidate(BaseModel):
    """후보 하나."""

    place_id: Optional[str] = None
    event_id: Optional[str] = None
    name: str = ""
    address: Optional[str] = None
    category: Optional[str] = None


class DisambiguationBlock(BaseModel):
    """동음이의/다중 후보 선택."""

    type: str = "disambiguation"
    message: Optional[str] = None
    candidates: list[DisambiguationCandidate] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 15. done — 응답 종료 마커
# ---------------------------------------------------------------------------
class DoneBlock(BaseModel):
    """응답 종료. status로 done/error/cancelled 구분.

    error 발생 시 status="error" + error_message에 상세 내용.
    기획서 기준 done 블록이 error 역할도 수행.
    message_id: 방금 저장된 assistant 메시지 DB id (FE 북마크용).
    """

    type: str = "done"
    status: str = "done"  # "done" | "error" | "cancelled"
    error_message: Optional[str] = None
    message_id: Optional[int] = None


# ---------------------------------------------------------------------------
# 16. error — 에러 상세 (done status="error" 시 보조)
# ---------------------------------------------------------------------------
class ErrorBlock(BaseModel):
    """에러 상세 정보. done 블록의 보조 블록."""

    type: str = "error"
    code: Optional[str] = None
    message: str = ""
    recoverable: bool = True


# ---------------------------------------------------------------------------
# SSE 제어 이벤트 (messages 미저장, 16종 콘텐츠 블록이 아님)
# ---------------------------------------------------------------------------
class StatusFrame(BaseModel):
    """SSE 제어 이벤트 — 노드 전환 진행 표시.

    16종 콘텐츠 블록이 아님 (messages.blocks에 저장하지 않음).
    런타임 SSE 전송 전용.
    """

    type: str = "status"
    message: str = ""
    node: Optional[str] = None  # 현재 실행 중인 노드 이름


class DonePartialFrame(BaseModel):
    """SSE 제어 이벤트 — multi-intent 구분자.

    16종 콘텐츠 블록이 아님 (messages.blocks에 저장하지 않음).
    """

    type: str = "done_partial"
    completed_intent: Optional[str] = None


# ---------------------------------------------------------------------------
# Block type mapping (직렬화/역직렬화 헬퍼)
# ---------------------------------------------------------------------------
CONTENT_BLOCK_TYPES: dict[str, type[BaseModel]] = {
    "intent": IntentBlock,
    "text": TextBlock,
    "text_stream": TextStreamBlock,
    "place": PlaceBlock,
    "places": PlacesBlock,
    "events": EventsBlock,
    "course": CourseBlock,
    "map_markers": MapMarkersBlock,
    "map_route": MapRouteBlock,
    "chart": ChartBlock,
    "calendar": CalendarBlock,
    "references": ReferencesBlock,
    "analysis_sources": AnalysisSourcesBlock,
    "disambiguation": DisambiguationBlock,
    "done": DoneBlock,
    "error": ErrorBlock,
}


def serialize_block(block: BaseModel) -> dict[str, Any]:
    """블록 → JSON-serializable dict."""
    return block.model_dump(exclude_none=True)


def deserialize_block(data: dict[str, Any]) -> BaseModel:
    """dict → 적절한 블록 인스턴스. 알 수 없는 type이면 ValueError."""
    block_type = data.get("type", "")
    cls = CONTENT_BLOCK_TYPES.get(block_type)
    if cls is None:
        raise ValueError(f"알 수 없는 블록 type: {block_type!r}")
    return cls.model_validate(data)


# ---------------------------------------------------------------------------
# 유틸: place 블록에 지도 검색 URL 부착
# ---------------------------------------------------------------------------
def attach_map_urls(place_dict: dict[str, Any]) -> dict[str, Any]:
    """place 블록 dict에 naver_map_url, kakao_map_url을 런타임 생성하여 추가.

    DB 변경 없이 장소명 기반 검색 URL 패턴 사용.
    이미 URL이 있으면 덮어쓰지 않음.
    """
    name = place_dict.get("name", "")
    if not name:
        return place_dict
    encoded = quote(name)
    if not place_dict.get("naver_map_url"):
        place_dict["naver_map_url"] = f"https://map.naver.com/v5/search/{encoded}"
    if not place_dict.get("kakao_map_url"):
        place_dict["kakao_map_url"] = f"https://map.kakao.com/?q={encoded}"
    return place_dict
