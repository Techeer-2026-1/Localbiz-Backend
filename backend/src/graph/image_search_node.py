"""IMAGE_SEARCH 노드 — Gemini Vision으로 이미지에서 장소 식별.

흐름:
  1. httpx로 이미지 URL 다운로드 → base64 변환
  2. Gemini Vision (gemini-2.5-flash) JSON-mode 분석
     → place_candidates / main_candidate / scene_description / is_identifiable
  3. 분기:
     A. is_identifiable=false → "찾을 수 없어요" text_stream
     B. place_candidates 있음 → PG name ILIKE 검색
        - 1건 또는 main_candidate 있음: text_stream + place 블록
        - 복수 + main_candidate null: text_stream + disambiguation 블록
        - PG 미스 + wants_identify: Google Vision Web Detection → PG 재검색 → fallback
        - PG 미스 + 추천 의도: scene_description k-NN fallback
     C. place_candidates 없음 + wants_identify: Google Vision Web Detection → PG 검색 → fallback
        place_candidates 없음 + 추천 의도: scene_description 768d 임베딩 → k-NN → places + map_markers

기획서 §4.5 SSE 블록 순서:
  식별 성공: intent → text_stream → place → done
  유사 추천: intent → text_stream → places[] → map_markers → done
  간판 복수: intent → text_stream → disambiguation → done

불변식 #7: gemini-embedding-001 768d
불변식 #8: asyncpg $1,$2 파라미터 바인딩
불변식 #9: Optional[str] 사용 (str | None 금지)
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Optional

from src.graph._tracing import traced_node  # pyright: ignore[reportMissingImports]
from src.observability.metrics import langgraph_fallback_total  # pyright: ignore[reportMissingImports]

_URL_RE = re.compile(r"https?://\S+")
# "여기 어딘지" 류 — 특정 장소 식별 의도
_IDENTIFY_RE = re.compile(
    r"어디(야|인지|냐|에요|예요|임|\?|$)|어딘지|여기가 어디|이게 어디|어느 (곳|장소|가게|카페|식당)"
)
# 지점명 suffix — "홍대점", "1호점", "강남지점" 등 → 같은 브랜드의 지점으로 허용
_BRANCH_SUFFIX_RE = re.compile(r".+점$|.+지점$|.+분점$|.+호점$")

logger = logging.getLogger(__name__)

_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB
_OS_TOP_K = 5
_OS_MIN_SCORE = 0.5

_VISION_SYSTEM_PROMPT = """\
당신은 서울 로컬 장소 검색 시스템의 이미지 분석 AI입니다.
이미지를 분석하고 지정된 JSON 형식으로만 응답합니다. 다른 텍스트는 절대 포함하지 마세요.
"""

_VISION_USER_PROMPT = """\
이미지를 분석하고 아래 JSON만 반환하세요.

{
  "place_candidates": ["가게명1", "가게명2"],
  "main_candidate": "가게명1",
  "photo_subject": "장소",
  "place_type": "카페",
  "scene_description": "장소 특징 설명",
  "is_identifiable": true
}

각 필드 규칙:
- place_candidates: 이미지에서 읽힌 가게명/브랜드명/랜드마크명/건물명/역명. 없으면 []
  (간판, 로고, 영수증, 지도 텍스트, 역 표지판 포함)
- main_candidate: place_candidates 중 이미지 중심 또는 가장 크게 보이는 장소. 판단 불가 또는 후보 없으면 null
- photo_subject: 사진이 주로 담고 있는 것. 아래 중 하나만 선택.
  장소 — 카페/식당/건물/공원 등 장소의 내외부
  음식·음료 — 음식, 음료, 메뉴판이 주인공
  교통·인프라 — 지하철역, 버스정류장, 도로 표지판
  사물·기타 — 물건, 동물, 사람, 하늘, 화장실 등 장소와 무관한 피사체
- place_type: photo_subject가 "장소" 또는 "교통·인프라"일 때만 작성. 아래 중 하나. 나머지는 null.
  카페 / 음식점 / 술집 / 공원 / 박물관·미술관 / 쇼핑몰·상점 / 숙박 / 랜드마크·건물 / 교통·인프라 / 기타
- scene_description: 장소의 분위기·인테리어·조명·색감·건축 특징을 한국어로 서술. 항상 작성.
  음식 사진이면 음식의 특징(재료, 색감, 담음새)을 서술.
  장소 이름처럼 들릴 수 있는 단어(나무, 숲, 하늘 등)는 "나무가 보이는", "숲 느낌의"처럼 수식어로 표현.
  예) "따뜻한 조명의 아늑한 인테리어, 원목 테이블과 화분 장식, 유리창 너머 정원뷰"
      "한식 음식 사진, 비빔밥과 반찬류, 전통 도자기 그릇"
      "황금색 고층 빌딩 외관, 여의도 배경, 60층 이상 현대 건축물"
- is_identifiable: 장소·랜드마크·음식점을 검색으로 찾을 가능성이 있으면 true.
  사람 얼굴이 중심인 셀카, 하늘만 찍힌 사진, 동물·화장실·일반 사물 사진은 false.
"""

_IMAGE_SEARCH_SYSTEM_PROMPT = (
    "당신은 서울 로컬 라이프 AI 챗봇 'AnyWay'입니다. "
    "자기소개나 인사로 시작하지 말고 바로 본론으로 답변하세요. "
    "이미지 분석 결과를 바탕으로 장소 정보를 간결하게 안내해주세요. "
    "불필요한 감탄 표현이나 과도한 이모지 사용을 자제하고, 핵심 정보를 자연스럽게 전달하세요."
)


# ---------------------------------------------------------------------------
# 이미지 다운로드
# ---------------------------------------------------------------------------
async def _download_image(url: str) -> Optional[str]:
    """이미지 URL → base64 문자열. 실패 시 None."""
    import httpx  # pyright: ignore[reportMissingImports]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()
            if len(resp.content) > _MAX_IMAGE_BYTES:
                logger.warning("image_search: image too large (%d bytes)", len(resp.content))
                return None
            return base64.b64encode(resp.content).decode("utf-8")
    except Exception:
        logger.exception("image_search: download failed url=%s", url)
        return None


# ---------------------------------------------------------------------------
# Gemini Vision 분석
# ---------------------------------------------------------------------------
async def _analyze_vision(b64_image: str, api_key: str) -> dict[str, Any]:
    """Gemini Vision으로 이미지 분석 → JSON dict.

    실패 시 기본값 반환: place_candidates=[], is_identifiable=False.
    """
    from langchain_core.messages import HumanMessage, SystemMessage  # pyright: ignore[reportMissingImports]
    from langchain_google_genai import ChatGoogleGenerativeAI  # pyright: ignore[reportMissingImports]

    from src.utils.llm_parsing import parse_llm_json  # pyright: ignore[reportMissingImports]
    from src.utils.resilience import retry_call  # pyright: ignore[reportMissingImports]

    _default: dict[str, Any] = {
        "place_candidates": [],
        "main_candidate": None,
        "photo_subject": "장소",
        "place_type": None,
        "scene_description": "",
        "is_identifiable": False,
    }

    try:
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=api_key,
            temperature=0,
        )
        response = await retry_call(
            lambda: llm.ainvoke(
                [
                    SystemMessage(content=_VISION_SYSTEM_PROMPT),
                    HumanMessage(
                        content=[
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"},
                            },
                            {"type": "text", "text": _VISION_USER_PROMPT},
                        ]
                    ),
                ]
            ),
            attempts=3,
        )
        text = str(response.content).strip()

        result = parse_llm_json(text)
        return {
            "place_candidates": result.get("place_candidates") or [],
            "main_candidate": result.get("main_candidate"),
            "photo_subject": result.get("photo_subject") or "장소",
            "place_type": result.get("place_type"),
            "scene_description": result.get("scene_description") or "",
            "is_identifiable": bool(result.get("is_identifiable", True)),
        }
    except Exception:
        logger.exception("image_search: vision analysis failed")
        return _default


# ---------------------------------------------------------------------------
# DB 검색 헬퍼
# ---------------------------------------------------------------------------
async def _find_last_image_url_from_history(thread_id: str) -> Optional[str]:
    """messages 테이블에서 직전 user 메시지의 이미지 URL 추출. 불변식 #3: SELECT만."""
    try:
        from src.db.postgres import get_pool  # pyright: ignore[reportMissingImports]

        rows = await get_pool().fetch(
            "SELECT blocks FROM messages WHERE thread_id = $1 AND role = 'user' ORDER BY message_id DESC LIMIT 5",
            thread_id,
        )
        for row in rows:
            blocks = row["blocks"]
            if isinstance(blocks, str):
                blocks = json.loads(blocks)
            for block in blocks if isinstance(blocks, list) else []:
                if not isinstance(block, dict):
                    continue
                content = block.get("content", "")
                match = _URL_RE.search(content)
                if match:
                    return match.group(0)
    except Exception:
        logger.warning("image_search: history 이미지 URL 조회 실패")
    return None


async def _search_pg(pool: Any, name: str) -> list[dict[str, Any]]:
    """장소명으로 places 테이블 검색. 불변식 #8.

    name 컬럼 ILIKE 외에 raw_data->>'search_name' 도 함께 조회.
    영어 간판("SAESEOUL") → 한국어 DB명("새서울") 미스매치를 커버.
    """
    try:
        rows = await pool.fetch(
            "SELECT place_id, name, category, address, district, "
            "ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng "
            "FROM places WHERE is_deleted = false "
            "AND (name ILIKE $1 OR (raw_data IS NOT NULL AND raw_data->>'search_name' ILIKE $1)) "
            "LIMIT 5",
            f"%{name}%",
        )
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("image_search: _search_pg failed name=%s", name)
        return []


async def _embed_768d(text: str, api_key: str) -> list[float]:
    """Gemini embedding-001 768d 임베딩. 불변식 #7."""
    from src.utils.resilience import request_json  # pyright: ignore[reportMissingImports]

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"
    body = {
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": text[:2000]}]},
        "outputDimensionality": 768,
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    data = await request_json("POST", url, json=body, headers=headers, timeout=10)
    return data.get("embedding", {}).get("values", [0.0] * 768)


async def _search_knn(os_client: Any, vector: list[float]) -> list[str]:
    """places_vector k-NN HNSW 검색 → place_id 목록."""
    from src.utils.resilience import retry_call  # pyright: ignore[reportMissingImports]

    try:
        body: dict[str, Any] = {
            "size": _OS_TOP_K,
            "query": {"knn": {"embedding": {"vector": vector, "k": _OS_TOP_K}}},
            "min_score": _OS_MIN_SCORE,
        }
        result = await retry_call(lambda: os_client.search(index="places_vector", body=body), attempts=3)
        return [h.get("_id", "") for h in result.get("hits", {}).get("hits", [])]
    except Exception:
        logger.exception("image_search: k-NN search failed")
        return []


async def _fetch_places_by_ids(pool: Any, place_ids: list[str]) -> list[dict[str, Any]]:
    """place_id 목록 → places 상세 조회. 불변식 #8."""
    if not place_ids:
        return []
    try:
        rows = await pool.fetch(
            "SELECT place_id, name, category, address, district, "
            "ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng "
            "FROM places WHERE place_id = ANY($1::text[]) AND is_deleted = false",
            place_ids,
        )
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("image_search: _fetch_places_by_ids failed")
        return []


async def _web_detect(b64_image: str, api_key: str) -> dict[str, Any]:
    """Google Vision Web Detection REST API → 웹 엔티티/페이지 제목/레이블 반환.

    base64 이미지를 직접 전송하므로 localhost URL도 동작.

    Returns:
        {"entities": [str], "best_guess": str, "page_titles": [str]}
        실패 또는 API 키 미설정 시 빈 값 반환
    """
    from src.utils.resilience import request_json  # pyright: ignore[reportMissingImports]

    _empty: dict[str, Any] = {"entities": [], "best_guess": "", "page_titles": []}
    if not api_key or not b64_image:
        return _empty
    try:
        endpoint = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
        body = {
            "requests": [
                {
                    "image": {"content": b64_image},
                    "features": [{"type": "WEB_DETECTION", "maxResults": 20}],
                }
            ]
        }
        data = await request_json("POST", endpoint, json=body, timeout=15)

        responses = data.get("responses", [])
        web = responses[0].get("webDetection", {}) if responses else {}

        # 스코어 임계값 낮춤 (0.5 → 0.3) — 장소명이 낮은 스코어로 나올 수 있음
        entities = [
            e["description"] for e in web.get("webEntities", []) if e.get("score", 0) > 0.3 and e.get("description")
        ]
        guesses = [lb["label"] for lb in web.get("bestGuessLabels", []) if lb.get("label")]

        # pagesWithMatchingImages 페이지 제목 — 카페명/식당명이 포함된 경우가 많음
        page_titles = [p["pageTitle"] for p in web.get("pagesWithMatchingImages", []) if p.get("pageTitle")][:5]

        logger.info(
            "image_search: web_detect entities=%s best_guess=%s pages=%s",
            entities[:3],
            guesses[:1],
            page_titles[:2],
        )
        return {
            "entities": entities[:10],
            "best_guess": guesses[0] if guesses else "",
            "page_titles": page_titles,
        }
    except Exception:
        logger.exception("image_search: web detection failed")
        return _empty


_GP_TYPE_MAP: dict[str, str] = {
    "cafe": "카페",
    "restaurant": "음식점",
    "bar": "술집",
    "park": "공원",
    "museum": "박물관·미술관",
    "art_gallery": "박물관·미술관",
    "shopping_mall": "쇼핑몰·상점",
    "store": "쇼핑몰·상점",
    "lodging": "숙박",
    "hotel": "숙박",
}


async def _save_gp_place_to_db(
    pool: Any,
    os_client: Optional[Any],
    gp_place: dict[str, Any],
    scene_description: str,
    gemini_api_key: str,
    search_name: str = "",
) -> Optional[str]:
    """Google Places 결과를 places(PG) + places_vector(OS)에 저장.

    중복 방지: 같은 name+address가 이미 있으면 기존 place_id 반환.
    OS 인덱싱은 best-effort — 실패해도 PG INSERT는 유지.
    불변식 #8: asyncpg 파라미터 바인딩.
    """
    import uuid as _uuid_lib

    lat = gp_place.get("lat")
    lng = gp_place.get("lng")
    name: str = gp_place.get("name", "")
    address: str = gp_place.get("address", "")
    if not name or lat is None or lng is None:
        return None

    try:
        # 중복 체크: 같은 이름+주소 이미 존재하면 INSERT 스킵
        existing = await pool.fetch(
            "SELECT place_id FROM places WHERE name = $1 AND address = $2 AND source = $3 AND is_deleted = false LIMIT 1",
            name,
            address,
            "google_places",
        )
        if existing:
            existing_id: str = existing[0]["place_id"]
            logger.info("image_search: GP place '%s' already in DB as %s", name, existing_id)
            # 기존 레코드에 search_name 없으면 raw_data에 추가 (영어↔한국어 재발견용)
            if search_name and search_name != name:
                try:
                    await pool.execute(
                        "UPDATE places SET raw_data = COALESCE(raw_data, '{}'::jsonb) || $1::jsonb "
                        "WHERE place_id = $2 AND (raw_data->>'search_name' IS NULL OR raw_data->>'search_name' != $3)",
                        json.dumps({"search_name": search_name}, ensure_ascii=False),
                        existing_id,
                        search_name,
                    )
                except Exception:
                    logger.warning("image_search: failed to patch search_name for %s", existing_id)
            return existing_id

        # 주소에서 구(district) 추출 — "서울 마포구 홍익로 10" → "마포구"
        district_m = re.search(r"([가-힣]+구)\b", address)
        district: Optional[str] = district_m.group(1) if district_m else None

        new_id = str(_uuid_lib.uuid4())
        raw_payload = {k: v for k, v in gp_place.items() if k not in ("lat", "lng", "place_id")}
        # 원본 검색명(영어 간판 등)을 저장 → _search_pg의 raw_data->>'search_name' 조회로 재발견 가능
        if search_name and search_name != name:
            raw_payload["search_name"] = search_name
        raw_data = json.dumps(raw_payload, ensure_ascii=False)
        await pool.execute(
            """
            INSERT INTO places (
                place_id, name, category, sub_category, address, district,
                geom, phone, raw_data, source, is_deleted
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                ST_SetSRID(ST_MakePoint($7, $8), 4326),
                $9, $10::jsonb, $11, false
            )
            """,
            new_id,
            name,
            gp_place.get("category") or "",
            None,  # sub_category
            address,
            district,
            float(lng),  # ST_MakePoint(경도, 위도) — longitude first
            float(lat),
            gp_place.get("phone"),
            raw_data,
            "google_places",
        )
        logger.info("image_search: saved GP place '%s' to PG as %s", name, new_id)

        # OS 인덱싱 (best-effort) — scene_description 768d 임베딩
        if os_client and scene_description:
            try:
                type_prefix = f"{gp_place.get('category', '')} " if gp_place.get("category") else ""
                embed_text = f"{type_prefix}분위기 특징: {scene_description}"
                vector = await _embed_768d(embed_text, gemini_api_key)
                await os_client.index(
                    index="places_vector",
                    id=new_id,
                    body={"embedding": vector},
                )
                logger.info("image_search: indexed '%s' to OpenSearch", name)
            except Exception:
                logger.warning("image_search: OS indexing failed for '%s' (PG saved OK)", name)

        return new_id
    except Exception:
        logger.exception("image_search: failed to save GP place '%s' to DB", name)
        return None


async def _get_google_place_details(raw_place_id: str, api_key: str) -> dict[str, Any]:
    """Google Places Details API → 영업시간·전화·평점 등 상세 정보. 실패 시 빈 dict."""
    from src.utils.resilience import request_json  # pyright: ignore[reportMissingImports]

    if not api_key or not raw_place_id:
        return {}
    try:
        params = {
            "place_id": raw_place_id,
            "language": "ko",
            "fields": "rating,user_ratings_total,opening_hours,formatted_phone_number,price_level,website",
            "key": api_key,
        }
        data = await request_json(
            "GET",
            "https://maps.googleapis.com/maps/api/place/details/json",
            params=params,
            timeout=8,
        )
        return data.get("result", {})
    except Exception:
        logger.warning("image_search: google place details failed id=%s", raw_place_id)
        return {}


async def _search_google_places(name: str, api_key: str) -> Optional[dict[str, Any]]:
    """Google Places Text Search + Details → 장소 dict. 없으면 None.

    반환 dict: place_id(gp_ 접두어), name, category, address, lat, lng,
              rating, user_ratings_total, phone, opening_hours, price_level
    """
    from src.utils.resilience import request_json  # pyright: ignore[reportMissingImports]

    if not api_key:
        return None
    try:
        params = {
            "query": f"{name} 서울",
            "language": "ko",
            "key": api_key,
        }
        data = await request_json(
            "GET",
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params=params,
            timeout=10,
        )

        results = data.get("results", [])
        if not results:
            logger.info("image_search: google places no results for name=%s", name)
            return None

        r = results[0]
        raw_place_id: str = r.get("place_id", "")
        loc = r.get("geometry", {}).get("location", {})
        types: list[str] = r.get("types", [])
        category = next((v for t in types for k, v in _GP_TYPE_MAP.items() if k in t), "")

        # Details API로 상세 정보 병렬 조회
        details = await _get_google_place_details(raw_place_id, api_key)

        # 영업시간 — 요일별 전체 텍스트를 Gemini가 자연어로 요약하도록 전달
        hours_info: Optional[str] = None
        oh = details.get("opening_hours", {})
        if oh:
            weekday = oh.get("weekday_text", [])
            if weekday:
                hours_info = "\n".join(weekday)

        place: dict[str, Any] = {
            "place_id": f"gp_{raw_place_id}",
            "name": r.get("name", name),
            "category": category,
            "address": r.get("formatted_address", ""),
            "lat": loc.get("lat"),
            "lng": loc.get("lng"),
        }
        if details.get("rating"):
            place["rating"] = details["rating"]
        if details.get("user_ratings_total"):
            place["user_ratings_total"] = details["user_ratings_total"]
        if hours_info:
            place["opening_hours"] = hours_info
        if details.get("price_level") is not None:
            place["price_level"] = details["price_level"]

        logger.info(
            "image_search: google places hit name=%s → %s (rating=%s, reviews=%s)",
            name,
            place.get("name"),
            place.get("rating"),
            place.get("user_ratings_total"),
        )
        return place
    except Exception:
        logger.exception("image_search: google places search failed name=%s", name)
        return None


async def _extract_place_hint(
    entities: list[str],
    best_guess: str,
    page_titles: list[str],
    api_key: str,
) -> str:
    """Web Detection 결과 전체를 Gemini로 분석해 장소명 추출.

    웹 엔티티, 베스트 추측, 페이지 제목을 종합해 실제 장소(고유명사)를 찾아냄.
    일반 사물명(chair, table 등)은 걸러내고 장소명만 반환.

    Returns:
        장소명 문자열. 없으면 빈 문자열.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI  # pyright: ignore[reportMissingImports]

    from src.utils.llm_parsing import parse_llm_json  # pyright: ignore[reportMissingImports]
    from src.utils.resilience import retry_call  # pyright: ignore[reportMissingImports]

    if not api_key or (not entities and not best_guess and not page_titles):
        return ""

    parts: list[str] = []
    if entities:
        parts.append(f"웹 엔티티: {', '.join(entities[:10])}")
    if best_guess:
        parts.append(f"최적 추측: {best_guess}")
    if page_titles:
        parts.append(f"매칭 페이지 제목: {' | '.join(page_titles)}")

    try:
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=api_key,
            temperature=0,
        )
        response = await retry_call(
            lambda: llm.ainvoke(
                [
                    (
                        "system",
                        "구글 역방향 이미지 검색 결과입니다. "
                        "아래 텍스트에 명시적으로 등장하는 카페·식당·건물·공원 등 실제 장소의 고유명사(상호명/장소명)가 있으면 하나만 추출하세요. "
                        "절대로 추론하거나 추측하지 마세요. 텍스트에 글자 그대로 나온 장소명만 허용합니다. "
                        "Interior design, decoration, table, chair 같은 일반 사물/형용사, "
                        "개인 이름(YouTuber, 블로거 등), 홈데코 관련 키워드는 장소명이 아닙니다. "
                        "장소명이 없으면 반드시 빈 문자열을 반환하세요. "
                        'JSON으로만 응답: {"place_name": "장소명"} 또는 {"place_name": ""}',
                    ),
                    ("human", "\n".join(parts)),
                ]
            ),
            attempts=3,
        )
        text = str(response.content).strip()
        result = parse_llm_json(text)
        hint = result.get("place_name", "").strip()

        # 할루시네이션 방지: 원본 텍스트에 hint가 실제로 등장하는지 검증
        if hint:
            source_text = " ".join(entities + [best_guess] + page_titles).lower()
            if hint.lower() not in source_text:
                logger.info("image_search: hint '%s' not found in source text → discarded (hallucination)", hint)
                return ""

        logger.info("image_search: extracted place hint=%s", hint)
        return hint
    except Exception:
        logger.exception("image_search: place hint extraction failed")
        return ""


# ---------------------------------------------------------------------------
# 블록 생성 헬퍼
# ---------------------------------------------------------------------------
def _text_stream_block(prompt: str) -> dict[str, Any]:
    return {"type": "text_stream", "system": _IMAGE_SEARCH_SYSTEM_PROMPT, "prompt": prompt}


def _place_to_block(place: dict[str, Any]) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "place",
        "place_id": place.get("place_id", ""),
        "name": place.get("name", ""),
    }
    for key in ("category", "address", "district"):
        if place.get(key):
            block[key] = place[key]
    if place.get("lat") is not None:
        block["lat"] = place["lat"]
    if place.get("lng") is not None:
        block["lng"] = place["lng"]
    from src.models.blocks import attach_map_urls  # pyright: ignore[reportMissingImports]

    attach_map_urls(block)
    return block


# ---------------------------------------------------------------------------
# k-NN 흐름 (케이스 B/C 공통)
# ---------------------------------------------------------------------------
async def _fallback_knn_with_message(
    msg_prompt: str,
    scene_description: str,
    api_key: str,
    pool: Any,
    os_client: Optional[Any],
    place_type: Optional[str],
) -> list[dict[str, Any]]:
    """못 찾은 경우 커스텀 메시지 + k-NN 결과 반환. scene_description 없으면 메시지만."""
    langgraph_fallback_total.labels(path="image.knn_scene_fallback").inc()
    if not scene_description:
        return [_text_stream_block(msg_prompt)]
    knn_blocks = await _run_knn(scene_description, api_key, pool, os_client, place_type=place_type)
    non_text = [b for b in knn_blocks if b.get("type") != "text_stream"]
    if not non_text:
        return [_text_stream_block(msg_prompt)]
    return [_text_stream_block(msg_prompt), *non_text]


async def _run_knn(
    query_text: str,
    api_key: str,
    pool: Any,
    os_client: Optional[Any],
    place_type: Optional[str] = None,
) -> list[dict[str, Any]]:
    """scene_description → 임베딩 → k-NN → places + map_markers 블록.

    place_type이 있으면 PG category 필터 후 k-NN 결과와 교집합.
    임베딩 쿼리에 장소 유형 + "분위기 특징:" 접두어를 붙여 키워드 오염 차단.
    """
    if os_client is None:
        return [
            _text_stream_block("비슷한 장소를 찾으려 했지만 검색 서비스를 이용할 수 없어요. 잠시 후 다시 시도해주세요.")
        ]

    # 임베딩 쿼리: 장소 유형 + 분위기 특징 접두어 → 키워드 오염 완화
    type_prefix = f"{place_type} " if place_type else ""
    embedding_query = f"{type_prefix}분위기 특징: {query_text}"
    logger.info("image_search: knn embedding_query=%s", embedding_query[:80])

    try:
        vector = await _embed_768d(embedding_query, api_key)
    except Exception:
        logger.exception("image_search: embedding failed")
        return [_text_stream_block("이미지 분석은 완료됐지만 유사 장소 검색 중 오류가 발생했어요. 다시 시도해주세요.")]

    place_ids = await _search_knn(os_client, vector)

    # place_type이 있으면 PG category 필터 적용 (soft filter — 결과 없으면 필터 해제)
    if place_type and place_ids:
        try:
            rows = await pool.fetch(
                "SELECT place_id, name, category, address, district, "
                "ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng "
                "FROM places WHERE place_id = ANY($1::text[]) AND is_deleted = false "
                "AND category ILIKE $2",
                place_ids,
                f"%{place_type}%",
            )
            places = [dict(r) for r in rows]
            if not places:
                logger.info("image_search: category filter '%s' returned 0, falling back to unfiltered", place_type)
                places = await _fetch_places_by_ids(pool, place_ids)
        except Exception:
            logger.exception("image_search: category-filtered fetch failed")
            places = await _fetch_places_by_ids(pool, place_ids)
    else:
        places = await _fetch_places_by_ids(pool, place_ids)

    # k-NN 결과 순서 보존 (place_ids 순서 = 유사도 내림차순)
    id_order = {pid: i for i, pid in enumerate(place_ids)}
    places.sort(key=lambda p: id_order.get(p.get("place_id", ""), 999))

    if not places:
        return [
            _text_stream_block(
                "이미지와 비슷한 분위기의 장소를 찾지 못했어요. "
                "장소명을 직접 알려주시면 더 잘 찾아드릴 수 있다고 안내해주세요."
            )
        ]

    result_summary = "\n".join(
        f"- {p.get('name', '')} ({p.get('category', '')}, {p.get('district', '')})" for p in places
    )
    blocks: list[dict[str, Any]] = [
        _text_stream_block(
            f"이미지 분위기: {query_text}\n\n"
            f"비슷한 분위기의 장소:\n{result_summary}\n\n"
            "사진과 유사한 분위기의 장소를 소개해주세요. "
            "각 장소별로 이미지와 비슷한 특징(분위기, 인테리어, 위치)을 구체적으로 설명하세요."
        ),
        {
            "type": "places",
            "items": [_place_to_block(p) for p in places],
            "total_count": len(places),
        },
    ]

    markers = [
        {
            "place_id": p.get("place_id", ""),
            "lat": p["lat"],
            "lng": p["lng"],
            "label": p.get("name", ""),
        }
        for p in places
        if p.get("lat") is not None and p.get("lng") is not None
    ]
    if markers:
        blocks.append({"type": "map_markers", "markers": markers})

    return blocks


# ---------------------------------------------------------------------------
# place_candidates 처리 (케이스 B)
# ---------------------------------------------------------------------------
async def _handle_candidates(
    pool: Any,
    candidates: list[str],
    main_candidate: Optional[str],
    scene_description: str,
    api_key: str,
    os_client: Optional[Any],
    wants_identify: bool = False,
    wants_recommend: bool = False,
    b64_image: str = "",
    vision_api_key: str = "",
    google_places_api_key: str = "",
    place_type: Optional[str] = None,
) -> list[dict[str, Any]]:
    """place_candidates 있을 때 PG 검색 → place / disambiguation / k-NN / Web Detection fallback."""
    # main_candidate 우선, 나머지 순서로 PG 검색
    search_order = []
    if main_candidate:
        search_order.append(main_candidate)
    for c in candidates:
        if c != main_candidate:
            search_order.append(c)

    pg_results: list[dict[str, Any]] = []
    matched_name = ""
    for name in search_order:
        rows = await _search_pg(pool, name)
        if rows:
            pg_results = rows
            matched_name = name
            break

    # 서브 업종 필터: candidate가 결과 이름의 prefix이고 suffix가 지점명이 아니면 서브 업종 → 제거
    # 예) "63빌딩" → "63빌딩 뷔페"(뷔페≠지점) → 제외 / "스타벅스" → "스타벅스 홍대점" → 유지
    if pg_results and matched_name:
        cand_lower = matched_name.lower()

        def _is_acceptable(place_name: str) -> bool:
            name_lower = place_name.lower()
            # 비율 기준 통과 (60% 이상)
            if len(cand_lower) / max(len(name_lower), 1) >= 0.6:
                return True
            # 지점명 suffix 허용: prefix가 candidate이고 suffix가 지점 패턴
            if name_lower.startswith(cand_lower):
                suffix = name_lower[len(cand_lower) :].strip()
                if suffix and _BRANCH_SUFFIX_RE.match(suffix):
                    return True
            return False

        good = [p for p in pg_results if _is_acceptable(p.get("name", ""))]
        if not good:
            logger.info(
                "image_search: PG sub-venue filter removed all %d results for '%s' (names=%s)",
                len(pg_results),
                matched_name,
                [p.get("name") for p in pg_results],
            )
            pg_results = []
        else:
            logger.info(
                "image_search: PG found %d result(s) for '%s': %s",
                len(good),
                matched_name,
                [p.get("name") for p in good],
            )
            pg_results = good

    # PG 미스
    if not pg_results:
        logger.info("image_search: PG miss for candidates=%s", candidates)
        if wants_identify:
            # 1) Google Places Text Search — 간판명으로 직접 검색
            search_name = main_candidate or candidates[0]
            gp_place = await _search_google_places(search_name, google_places_api_key)
            if gp_place:
                # DB에 저장 (PG + OS best-effort) → 성공 시 새 UUID로 place_id 교체
                saved_id = await _save_gp_place_to_db(
                    pool, os_client, gp_place, scene_description, api_key, search_name=search_name
                )
                if saved_id:
                    gp_place["place_id"] = saved_id

                detail_parts = [
                    f"장소명: {gp_place.get('name', '')}",
                    f"카테고리: {gp_place.get('category', '') or '정보 없음'}",
                    f"주소: {gp_place.get('address', '')}",
                ]
                if gp_place.get("rating"):
                    detail_parts.append(
                        f"평점: {gp_place['rating']}점 ({gp_place.get('user_ratings_total', 0)}개 리뷰)"
                    )
                if gp_place.get("opening_hours"):
                    detail_parts.append(f"영업시간:\n{gp_place['opening_hours']}")
                if gp_place.get("price_level") is not None:
                    price_labels = ["무료", "저렴", "보통", "비쌈", "매우 비쌈"]
                    lvl = int(gp_place["price_level"])
                    detail_parts.append(f"가격대: {price_labels[lvl] if lvl < len(price_labels) else ''}")

                result_blocks: list[dict[str, Any]] = [
                    _text_stream_block(
                        f"이미지에서 '{search_name}' 간판을 확인했고, "
                        f"Google 검색으로 '{gp_place.get('name', search_name)}'을(를) 찾았습니다.\n\n"
                        + "\n".join(detail_parts)
                        + "\n\n장소명, 카테고리, 위치, 평점 등 알게 된 정보를 자연스럽게 소개해주세요."
                        + (
                            f"\n\n그 다음에 '{gp_place.get('name', search_name)}'와(과) 비슷한 분위기의 장소도 함께 추천해주세요."
                            if wants_recommend
                            else ""
                        )
                    ),
                    _place_to_block(gp_place),
                ]
                if wants_recommend and place_type not in ("랜드마크·건물", "교통·인프라"):
                    knn_blocks = await _run_knn(scene_description, api_key, pool, os_client, place_type=place_type)
                    result_blocks.extend(b for b in knn_blocks if b.get("type") != "text_stream")
                elif wants_recommend:
                    result_blocks.append(
                        _text_stream_block(
                            f"장소 소개 후, 주변 맛집이나 카페가 궁금하다면 "
                            f"'{gp_place.get('name', search_name)} 주변 카페 추천해줘'처럼 말씀해 주시면 찾아드릴 수 있다고 안내해주세요."
                        )
                    )
                return result_blocks

            # 2) Google Vision Web Detection → Gemini로 장소명 추출 → PG 재검색
            web = await _web_detect(b64_image, vision_api_key)
            hint = await _extract_place_hint(
                web.get("entities", []),
                web.get("best_guess", ""),
                web.get("page_titles", []),
                api_key,
            )
            if hint:
                rows = await _search_pg(pool, hint)
                if rows:
                    place = rows[0]
                    return [
                        _text_stream_block(
                            f"이미지에서 '{', '.join(candidates)}' 간판을 확인했고, "
                            f"Google 역방향 이미지 검색으로 '{place.get('name', hint)}'일 수 있습니다. "
                            f"장소 정보: {place.get('name', '')}, {place.get('category', '')}, {place.get('address', '')}. "
                            "확실하지 않을 수 있음을 먼저 언급하고, '혹시 이곳인가요?' 형태로 물어보며 장소를 소개해주세요."
                        ),
                        _place_to_block(place),
                    ]

        # 정확한 장소 특정 불가 → "정확히는 모르지만" + k-NN 비슷한 분위기 추천
        if wants_identify:
            return await _fallback_knn_with_message(
                "정확한 장소를 찾지 못했지만, 비슷한 분위기의 장소를 찾아드릴게요.\n\n"
                "정확히 찾은 장소는 아님을 먼저 알리고, 비슷한 분위기의 장소들을 자연스럽게 소개해주세요.",
                scene_description,
                api_key,
                pool,
                os_client,
                place_type=place_type,
            )

        # 추천 의도 → scene_description k-NN fallback
        knn_query = scene_description or (main_candidate or (candidates[0] if candidates else ""))
        if knn_query:
            return await _run_knn(knn_query, api_key, pool, os_client, place_type=place_type)

        return [
            _text_stream_block(
                "이미지에서 장소를 특정하기 어렵습니다. "
                "가게 간판이나 특징적인 장소가 잘 보이는 사진을 보내주시면 더 잘 찾아드릴 수 있다고 안내해주세요."
            )
        ]

    # 1건 또는 main_candidate 있음 → place 블록
    if len(pg_results) == 1 or main_candidate:
        place = pg_results[0]
        pg_result_blocks: list[dict[str, Any]] = [
            _text_stream_block(
                f"이미지에서 '{place.get('name', matched_name)}'을(를) 찾았습니다. "
                f"장소 정보: {place.get('name', '')}, {place.get('category', '')}, {place.get('address', '')}. "
                "장소명, 카테고리, 위치를 포함해 간결하게 소개해주세요."
                + (
                    f"\n\n그 다음에 '{place.get('name', matched_name)}'와(과) 비슷한 분위기의 장소도 함께 추천해주세요."
                    if wants_recommend
                    else ""
                )
            ),
            _place_to_block(place),
        ]
        if wants_recommend and place_type not in ("랜드마크·건물", "교통·인프라"):
            knn_blocks = await _run_knn(scene_description, api_key, pool, os_client, place_type=place_type)
            pg_result_blocks.extend(b for b in knn_blocks if b.get("type") != "text_stream")
        elif wants_recommend:
            pg_result_blocks.append(
                _text_stream_block(
                    f"장소 소개 후, 주변 맛집이나 카페가 궁금하다면 "
                    f"'{place.get('name', matched_name)} 주변 카페 추천해줘'처럼 말씀해 주시면 찾아드릴 수 있다고 안내해주세요."
                )
            )
        return pg_result_blocks

    # 복수 + main_candidate null → disambiguation
    return [
        _text_stream_block(
            f"이미지에서 여러 장소 후보가 확인됩니다: {', '.join(r['name'] for r in pg_results)}. "
            "어떤 장소를 찾으시는지 간단히 물어봐주세요."
        ),
        {
            "type": "disambiguation",
            "message": "사진에서 여러 장소가 보여요. 어떤 곳이 궁금하신가요?",
            "candidates": [
                {
                    "place_id": r.get("place_id"),
                    "name": r.get("name", ""),
                    "address": r.get("address"),
                    "category": r.get("category"),
                }
                for r in pg_results
            ],
        },
    ]


# ---------------------------------------------------------------------------
# LangGraph 노드
# ---------------------------------------------------------------------------
@traced_node("image_search")
async def image_search_node(state: dict[str, Any]) -> dict[str, Any]:
    """IMAGE_SEARCH 노드 — 이미지 URL에서 장소 식별.

    Args:
        state: AgentState dict.

    Returns:
        {"response_blocks": [...]}.
    """
    from src.config import get_settings  # pyright: ignore[reportMissingImports]
    from src.db.opensearch import get_os_client  # pyright: ignore[reportMissingImports]
    from src.db.postgres import get_pool  # pyright: ignore[reportMissingImports]

    query = state.get("query", "")
    settings = get_settings()
    pool = get_pool()

    # 텍스트 + URL 혼합 쿼리에서 URL 추출 ("이 가게 어디야? https://...")
    url_match = _URL_RE.search(query)
    image_url = url_match.group(0) if url_match else None
    query_text_only = _URL_RE.sub("", query).strip()

    # URL 없으면 ("아까 그 사진") → history에서 직전 이미지 URL 찾기
    if not image_url:
        thread_id: Optional[str] = state.get("thread_id")
        if thread_id:
            image_url = await _find_last_image_url_from_history(thread_id)
        if not image_url:
            return {
                "response_blocks": [_text_stream_block("이미지를 찾을 수 없습니다. 검색할 이미지를 다시 보내주세요.")]
            }

    # 특정 장소 식별 의도 여부 ("여기 어딘지" vs "비슷한 곳 추천")
    wants_identify = bool(_IDENTIFY_RE.search(query_text_only))
    # 비슷한 장소 추천 의도 — 식별 의도와 동시에 올 수 있음 ("여기 어딘지 알아? 비슷한 곳도 추천해줘")
    wants_recommend = bool(re.search(r"추천|비슷한|비슷|유사|같은 분위기|같은 느낌", query_text_only))

    os_client: Optional[Any] = None
    try:
        os_client = get_os_client()
    except RuntimeError:
        logger.warning("image_search: OpenSearch client not initialized")

    # 1. 이미지 다운로드
    b64 = await _download_image(image_url)
    if not b64:
        return {"response_blocks": [_text_stream_block("이미지를 불러오지 못했어요. 다시 시도해주세요.")]}

    # 2. Gemini Vision 분석
    vision = await _analyze_vision(b64, settings.gemini_llm_api_key)
    candidates: list[str] = vision.get("place_candidates") or []
    main_candidate: Optional[str] = vision.get("main_candidate")
    photo_subject: str = vision.get("photo_subject") or "장소"
    scene_description: str = vision.get("scene_description") or ""
    is_identifiable: bool = bool(vision.get("is_identifiable", True))
    place_type: Optional[str] = vision.get("place_type") or None

    logger.info(
        "image_search: candidates=%s main=%s photo_subject=%s place_type=%s identifiable=%s",
        candidates,
        main_candidate,
        photo_subject,
        place_type,
        is_identifiable,
    )

    vision_debug_block: dict[str, Any] = {
        "type": "vision_debug",
        "place_candidates": candidates,
        "main_candidate": main_candidate,
        "photo_subject": photo_subject,
        "scene_description": scene_description,
        "is_identifiable": is_identifiable,
    }

    # 케이스 A: 장소 찾기 불가 이미지 (셀카, 동물, 화장실, 하늘 등)
    if not is_identifiable:
        return {
            "response_blocks": [
                vision_debug_block,
                _text_stream_block(
                    "이 사진에서는 장소를 특정하기 어렵습니다. "
                    "가게 간판, 건물 외관, 음식 사진 등을 보내주시면 더 잘 찾아드릴 수 있다고 안내해주세요."
                ),
            ]
        }

    # 케이스 A-2: 사물·기타 사진 (간판 없음) — 검색 불가 안내
    if photo_subject == "사물·기타" and not candidates:
        return {
            "response_blocks": [
                vision_debug_block,
                _text_stream_block(
                    "이 사진에서는 장소 정보를 찾기 어려워요. "
                    "카페·식당 간판, 건물 외관, 지하철 표지판, 음식 사진 등을 보내주시면 더 잘 찾아드릴 수 있다고 안내해주세요."
                ),
            ]
        }

    # 케이스 B: place_candidates 있음 → PG 검색 (장소, 랜드마크, 역명 모두 포함)
    if candidates:
        blocks = await _handle_candidates(
            pool,
            candidates,
            main_candidate,
            scene_description,
            settings.gemini_llm_api_key,
            os_client,
            wants_identify=wants_identify,
            wants_recommend=wants_recommend,
            b64_image=b64 or "",
            vision_api_key=settings.google_vision_api_key,
            google_places_api_key=settings.google_places_api_key,
            place_type=place_type,
        )
        return {"response_blocks": [vision_debug_block, *blocks]}

    # 케이스 C: place_candidates 없음
    # C-1. 음식·음료 사진 → 비슷한 음식점 k-NN 추천
    if photo_subject == "음식·음료":
        food_prompt = (
            f"음식 사진이네요. {scene_description}\n\n"
            "이 음식/음료를 파는 음식점 또는 카페를 소개해주세요. "
            "음식의 특징(장르, 재료, 스타일)을 간단히 언급하고 비슷한 메뉴를 즐길 수 있는 장소를 추천해주세요."
        )
        knn_type = place_type or "음식점"
        knn_blocks = await _run_knn(
            scene_description or "음식점", settings.gemini_llm_api_key, pool, os_client, place_type=knn_type
        )
        non_text = [b for b in knn_blocks if b.get("type") != "text_stream"]
        food_blocks: list[dict[str, Any]] = (
            [_text_stream_block(food_prompt), *non_text] if non_text else [_text_stream_block(food_prompt)]
        )
        return {"response_blocks": [vision_debug_block, *food_blocks]}

    # C-2. 교통·인프라 / 랜드마크·건물 — 간판 없이 장소 특정 시도
    if wants_identify or place_type in ("교통·인프라", "랜드마크·건물"):
        # Web Detection → Gemini 장소명 추출 → PG 검색
        web = await _web_detect(b64 or "", settings.google_vision_api_key)
        hint = await _extract_place_hint(
            web.get("entities", []),
            web.get("best_guess", ""),
            web.get("page_titles", []),
            settings.gemini_llm_api_key,
        )
        if hint:
            rows = await _search_pg(pool, hint)
            if rows:
                place = rows[0]
                return {
                    "response_blocks": [
                        vision_debug_block,
                        _text_stream_block(
                            f"간판이 보이지 않아 Google 역방향 이미지 검색으로 추정한 결과 "
                            f"'{place.get('name', hint)}'일 수 있습니다. "
                            f"장소 정보: {place.get('name', '')}, {place.get('category', '')}, {place.get('address', '')}. "
                            "확실하지 않을 수 있음을 먼저 언급하고, '혹시 이곳인가요?' 형태로 물어보며 장소를 소개해주세요."
                        ),
                        _place_to_block(place),
                    ]
                }

        # 랜드마크·건물은 k-NN이 의미없음 → 안내 메시지만
        if place_type in ("랜드마크·건물", "교통·인프라"):
            return {
                "response_blocks": [
                    vision_debug_block,
                    _text_stream_block(
                        "사진에서 장소를 특정하기 어렵습니다. "
                        "건물 이름이나 역 이름을 직접 알려주시면 더 잘 찾아드릴 수 있다고 안내해주세요."
                    ),
                ]
            }

        # 일반 장소 — "정확히는 모르지만" + k-NN
        knn_blocks = await _fallback_knn_with_message(
            "정확한 장소를 찾지 못했지만, 비슷한 분위기의 장소를 찾아드릴게요.\n\n"
            "정확히 찾은 장소는 아님을 먼저 알리고, 비슷한 분위기의 장소들을 자연스럽게 소개해주세요.",
            scene_description,
            settings.gemini_llm_api_key,
            pool,
            os_client,
            place_type=place_type,
        )
        return {"response_blocks": [vision_debug_block, *knn_blocks]}

    # C-3. 추천 의도 + 랜드마크·건물 → k-NN 의미없음, 안내
    if place_type in ("랜드마크·건물", "교통·인프라"):
        return {
            "response_blocks": [
                vision_debug_block,
                _text_stream_block(
                    "건물이나 역 사진이네요. 장소 이름을 알려주시면 주변 맛집이나 카페를 찾아드릴 수 있어요."
                ),
            ]
        }

    if not scene_description:
        return {
            "response_blocks": [
                vision_debug_block,
                _text_stream_block(
                    "이미지에서 장소를 찾을 수 없어요. 가게 간판이나 특징적인 장소가 보이는 사진을 보내주세요."
                ),
            ]
        }

    # C-4. 추천 의도 + 일반 장소 → k-NN
    blocks = await _run_knn(scene_description, settings.gemini_llm_api_key, pool, os_client, place_type=place_type)
    return {"response_blocks": [vision_debug_block, *blocks]}
