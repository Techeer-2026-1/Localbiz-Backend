"""REFINE 공통 헬퍼 — 노드별 _handle_refinement에서 공유.

리스트 항목 조작 (replace/remove/add) + 검색 쿼리 생성 유틸 + 전용 system prompt.
"""

from __future__ import annotations

import copy
from typing import Any, Optional

REFINE_SYSTEM_PROMPT = (
    "당신은 서울 로컬 라이프 AI 챗봇 'AnyWay'입니다. "
    "사용자가 이전 응답의 수정을 요청했고, 수정이 완료되었습니다.\n\n"
    "## 절대 규칙\n"
    "- 수정된 내용을 1-2문장으로 간결하게 안내하세요.\n"
    "- 전체 테마나 매력을 개괄하지 마세요.\n"
    "- 변경된 항목이 무엇인지 명확히 언급하세요.\n"
    "- 핵심 키워드는 **굵게** 강조하세요."
)


def apply_remove(items: list[dict[str, Any]], target_index: int) -> list[dict[str, Any]]:
    """1-indexed target_index 항목 제거. 범위 초과 시 원본 반환."""
    idx = target_index - 1
    if idx < 0 or idx >= len(items):
        return items
    result = copy.deepcopy(items)
    result.pop(idx)
    return result


def apply_replace(
    items: list[dict[str, Any]],
    target_index: int,
    new_item: dict[str, Any],
) -> list[dict[str, Any]]:
    """1-indexed target_index 항목을 new_item으로 교체. 범위 초과 시 원본 반환."""
    idx = target_index - 1
    if idx < 0 or idx >= len(items):
        return items
    result = copy.deepcopy(items)
    result[idx] = new_item
    return result


def apply_add(
    items: list[dict[str, Any]],
    new_item: dict[str, Any],
) -> list[dict[str, Any]]:
    """new_item을 목록 끝에 추가."""
    result = copy.deepcopy(items)
    result.append(new_item)
    return result


def get_refinement_search_query(
    refinement: dict[str, Any],
    processed_query: Optional[dict[str, Any]],
    original_query: str,
) -> str:
    """REFINE 수정 요청에서 검색 쿼리를 생성.

    new_condition이 있으면 우선, 없으면 processed_query의 expanded_query,
    최종 fallback은 original_query.
    """
    new_condition = refinement.get("new_condition")
    if new_condition:
        return new_condition

    if processed_query:
        expanded = processed_query.get("expanded_query")
        if expanded:
            return expanded

    return original_query


def extract_items_from_blocks(
    blocks: list[dict[str, Any]],
    block_type: str,
) -> list[dict[str, Any]]:
    """이전 응답 블록에서 특정 타입의 items 추출.

    places → items, events → items, course → stops.
    """
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") != block_type:
            continue
        if block_type == "course":
            return block.get("stops", [])
        return block.get("items", [])
    return []


def extract_excluded_ids(
    items: list[dict[str, Any]],
    id_field: str = "place_id",
    target_index: Optional[int] = None,
) -> set[str]:
    """기존 항목들의 ID 집합 반환 (중복 검색 방지용).

    target_index가 주어지면 해당 항목은 제외 (교체 대상이므로).
    """
    ids: set[str] = set()
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        if target_index is not None and i == target_index:
            continue
        item_id = item.get(id_field, "")
        if item_id:
            ids.add(item_id)
    return ids
