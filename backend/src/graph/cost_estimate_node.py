"""COST_ESTIMATE 노드 — 비용 견적 텍스트 스트리밍.

경로 A (place_name 있음):
  Naver Blog Search → 정규식 금액 추출 → Gemini text_stream (blog 데이터 컨텍스트)

경로 B (place_name 없음):
  Gemini text_stream 단독 (category + district + keywords 컨텍스트)

불변식 #8: asyncpg 파라미터 바인딩 (PG 쿼리 미사용, 향후 추가 시 준수)
불변식 #9: Optional[str]
불변식 #10: text_stream 블록 재사용 (신규 블록 타입 없음)
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

_NAVER_BLOG_URL = "https://openapi.naver.com/v1/search/blog.json"
_NAVER_TIMEOUT = 5.0
_NAVER_DISPLAY = 10

_PRICE_MIN = 1_000
_PRICE_MAX = 500_000

_SYSTEM_PROMPT = (
    "당신은 서울 로컬 라이프 AI 챗봇 'AnyWay'입니다. "
    "사용자가 특정 장소나 카테고리의 예상 비용을 물어보고 있습니다. "
    "모든 메뉴 가격을 나열하지 말고 '약 X~Y만원대' 형식의 가격 구간으로 친절하게 안내해주세요."
)


def _has_specific_place(processed_query: dict[str, Any]) -> bool:
    name = processed_query.get("place_name")
    return bool(name and str(name).strip())


async def _fetch_blog_prices(
    place_name: str,
    client_id: str,
    client_secret: str,
) -> list[int]:
    """Naver Blog Search → 정규식 금액 추출 → 원 단위 list 반환.

    API 한도: 일 25,000회. timeout 5s. 추출 실패 시 빈 list.
    """
    if not client_id or not client_secret:
        logger.warning("cost_estimate: naver api key 미설정 → blog 조회 생략")
        return []

    import httpx  # pyright: ignore[reportMissingImports]

    try:
        async with httpx.AsyncClient(timeout=_NAVER_TIMEOUT) as client:
            resp = await client.get(
                _NAVER_BLOG_URL,
                headers={
                    "X-Naver-Client-Id": client_id,
                    "X-Naver-Client-Secret": client_secret,
                },
                params={"query": f"{place_name} 가격", "display": _NAVER_DISPLAY, "sort": "sim"},
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
    except Exception:
        logger.exception("cost_estimate: naver blog search 실패")
        return []

    cutoff = date.today() - timedelta(days=365)
    prices: list[int] = []
    for item in items:
        postdate_str = item.get("postdate", "")
        try:
            post_date = date(int(postdate_str[:4]), int(postdate_str[4:6]), int(postdate_str[6:8]))
            if post_date < cutoff:
                continue
        except (ValueError, IndexError):
            continue

        raw = item.get("description", "")
        text = re.sub(r"<[^>]+>", "", raw)
        for amount_str, unit in re.findall(r"(\d{1,3}(?:,\d{3})*|\d+)\s*(만\s*원|천\s*원|원)", text):
            try:
                digits = int(amount_str.replace(",", ""))
                unit_clean = unit.replace(" ", "")
                if unit_clean == "만원":
                    value = digits * 10_000
                elif unit_clean == "천원":
                    value = digits * 1_000
                else:
                    value = digits
                if _PRICE_MIN <= value <= _PRICE_MAX:
                    prices.append(value)
            except ValueError:
                continue

    return prices


def _extract_party_size(query: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*인", query)
    return int(m.group(1)) if m else None


def _build_prompt(
    query: str,
    processed_query: dict[str, Any],
    place_name: Optional[str],
    blog_prices: list[int],
) -> str:
    party = _extract_party_size(query) or 1
    party_prefix = f"{party}인 방문 기준, "

    if place_name:
        if blog_prices:
            price_list = ", ".join(f"{p:,}원" for p in sorted(blog_prices))
            price_section = f"블로그 수집 메뉴 단가 (메뉴 1개당 가격): {price_list}"
        else:
            price_section = "블로그 가격 정보: 없음"
        lines = [
            f"{place_name} 방문 예상 비용을 알려주세요.",
            price_section,
            f"{party_prefix}위 단가를 참고해 인당 메뉴 1~2개 주문 시 총 예상 비용을 '약 X~Y만원대' 형식으로 알려주세요. 메뉴 목록은 나열하지 마세요.",
        ]
        return "\n".join(lines)

    district = processed_query.get("district") or processed_query.get("neighborhood") or "서울"
    category = processed_query.get("category") or "음식점"
    keywords = processed_query.get("keywords") or []
    keyword_str = ", ".join(keywords) if keywords else ""

    lines = [
        "다음 조건의 예상 비용을 알려주세요:",
        f"- 지역: {district}",
        f"- 종류: {category}" + (f" / {keyword_str}" if keyword_str else ""),
        f"- 쿼리: {query}",
        f"{party_prefix}'약 X~Y만원대' 형식의 가격 구간으로 알려주세요. 메뉴 목록은 나열하지 마세요.",
    ]
    return "\n".join(lines)


async def _handle_refinement(
    state: dict[str, Any],
    previous_blocks: list[dict[str, Any]],
    refinement: dict[str, Any],
) -> dict[str, Any]:
    """COST_ESTIMATE refinement — 조건 변경 시 전체 재실행."""
    if refinement.get("action") == "change_condition" and refinement.get("new_condition"):
        state = dict(state)
        state["query"] = refinement["new_condition"]
    state = dict(state)
    state["previous_blocks"] = None
    state["refinement"] = None
    return await cost_estimate_node(state)


async def cost_estimate_node(state: dict[str, Any]) -> dict[str, Any]:
    """COST_ESTIMATE 노드 — 비용 견적 text_stream 블록 반환.

    Args:
        state: AgentState dict.

    Returns:
        {"response_blocks": [text_stream]}.
    """
    previous_blocks = state.get("previous_blocks")
    refinement = state.get("refinement")
    if previous_blocks and refinement:
        return await _handle_refinement(state, previous_blocks, refinement)

    from src.config import get_settings  # pyright: ignore[reportMissingImports]

    query: str = state.get("query", "")
    processed_query: dict[str, Any] = state.get("processed_query") or {}

    place_name: Optional[str] = processed_query.get("place_name")
    blog_prices: list[int] = []

    if _has_specific_place(processed_query):
        settings = get_settings()
        blog_prices = await _fetch_blog_prices(
            str(place_name),
            settings.naver_client_id or "",
            settings.naver_client_secret or "",
        )
        logger.info("cost_estimate: path_a place=%s blog_prices=%d건", place_name, len(blog_prices))
    else:
        logger.info("cost_estimate: path_b (no place_name) → gemini 단독")

    prompt = _build_prompt(query, processed_query, place_name, blog_prices)

    return {
        "response_blocks": [
            {
                "type": "text_stream",
                "system": _SYSTEM_PROMPT,
                "prompt": prompt,
            }
        ]
    }
