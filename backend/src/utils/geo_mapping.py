"""동네명 → 자치구 매핑 유틸.

query_preprocessor, place_search, place_recommend, course_plan 등
여러 노드에서 공유하는 neighborhood→district 추론 테이블.
"""

from __future__ import annotations

from typing import Optional

# 서울 주요 동네 → 자치구 매핑
NEIGHBORHOOD_TO_DISTRICT: dict[str, str] = {
    # 마포구
    "홍대": "마포구",
    "홍대입구": "마포구",
    "합정": "마포구",
    "상수": "마포구",
    "망원": "마포구",
    "연남": "마포구",
    "연남동": "마포구",
    "서교동": "마포구",
    "연트럴파크": "마포구",
    # 강남구
    "강남": "강남구",
    "강남역": "강남구",
    "신사": "강남구",
    "신사동": "강남구",
    "압구정": "강남구",
    "청담": "강남구",
    "청담동": "강남구",
    "가로수길": "강남구",
    "삼성동": "강남구",
    "역삼": "강남구",
    "역삼동": "강남구",
    "논현": "강남구",
    "논현동": "강남구",
    # 용산구
    "이태원": "용산구",
    "한남": "용산구",
    "한남동": "용산구",
    "경리단길": "용산구",
    "해방촌": "용산구",
    "용산": "용산구",
    # 성동구
    "성수": "성동구",
    "성수동": "성동구",
    "왕십리": "성동구",
    "서울숲": "성동구",
    # 광진구
    "건대": "광진구",
    "건대입구": "광진구",
    "자양동": "광진구",
    # 송파구
    "잠실": "송파구",
    "석촌": "송파구",
    "방이동": "송파구",
    # 종로구
    "북촌": "종로구",
    "삼청동": "종로구",
    "익선동": "종로구",
    "종로": "종로구",
    "광화문": "종로구",
    "인사동": "종로구",
    "경복궁": "종로구",
    "서촌": "종로구",
    # 중구
    "을지로": "중구",
    "명동": "중구",
    "남산": "중구",
    "충무로": "중구",
    "동대문": "중구",
    # 서대문구
    "신촌": "서대문구",
    "이대": "서대문구",
    "연세대": "서대문구",
    # 영등포구
    "여의도": "영등포구",
    "영등포": "영등포구",
    # 서초구
    "서초": "서초구",
    "방배": "서초구",
    "반포": "서초구",
    "잠원": "서초구",
    # 강동구
    "천호": "강동구",
    "길동": "강동구",
    # 동작구
    "노량진": "동작구",
    "사당": "동작구",
    # 관악구
    "신림": "관악구",
    "서울대입구": "관악구",
    # 강서구
    "마곡": "강서구",
    # 노원구
    "노원": "노원구",
}


def resolve_district(neighborhood: Optional[str]) -> Optional[str]:
    """동네명에서 자치구를 추론한다.

    Args:
        neighborhood: 동네/지역명 (예: "홍대", "강남역")

    Returns:
        매핑된 자치구명 또는 None.
    """
    if not neighborhood:
        return None
    neighborhood = neighborhood.strip()
    if not neighborhood:
        return None
    # 정확 매칭 우선
    if neighborhood in NEIGHBORHOOD_TO_DISTRICT:
        return NEIGHBORHOOD_TO_DISTRICT[neighborhood]
    # 부분 매칭 — key가 입력에 포함된 경우만 (예: "홍대앞" → "홍대" 매칭)
    # 역방향(neighborhood in key)은 과도한 매칭 위험으로 제거
    for key, district in NEIGHBORHOOD_TO_DISTRICT.items():
        if key in neighborhood:
            return district
    return None
