"""LLM 응답(JSON) 파싱 유틸 (#124).

Gemini가 ```json ... ``` 코드펜스로 감싸 반환하는 JSON을 견고하게 파싱한다.
노드별로 중복돼 있던 취약한 `text.startswith("```")` split 로직을 대체.

`parse_llm_json`은 파싱 실패 시 `ValueError`를 raise — `json.JSONDecodeError`가
`ValueError`의 서브클래스이므로, 호출부의 기존 `except Exception`/`except ValueError`
graceful fallback이 그대로 동작한다.

Phase: Infra (공통 인프라 하드닝).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

# 앞쪽 코드펜스(```json / ```)와 뒤쪽 코드펜스(```)를 각각 독립적으로 제거.
# split 방식과 달리 본문 중간에 ```가 들어가도(문자열 값 내부 등) 안전.
_OPEN_FENCE_RE = re.compile(r"^```[ \t]*(?:json)?[ \t]*\r?\n?", re.IGNORECASE)
_CLOSE_FENCE_RE = re.compile(r"\r?\n?[ \t]*```$")


def parse_llm_json(text: str) -> Any:
    """LLM 텍스트 응답에서 JSON을 추출·파싱한다.

    처리:
      1. 앞뒤 공백 제거.
      2. 앞쪽 ```json / ``` 펜스, 뒤쪽 ``` 펜스를 각각 제거(있을 때만).
      3. `json.loads`.

    Args:
        text: LLM 원본 응답 문자열.

    Returns:
        파싱된 값 (dict / list 등).

    Raises:
        ValueError: 빈 응답 또는 JSON 파싱 실패 시 (`json.JSONDecodeError` 포함).
    """
    if not text or not text.strip():
        raise ValueError("빈 LLM 응답")

    s = text.strip()
    s = _OPEN_FENCE_RE.sub("", s)
    s = _CLOSE_FENCE_RE.sub("", s)
    s = s.strip()

    if not s:
        raise ValueError("코드펜스 제거 후 빈 내용")

    return json.loads(s)


def require_keys(obj: Any, keys: Iterable[str]) -> dict[str, Any]:
    """`obj`가 dict이고 `keys`를 모두 포함하는지 검증.

    Args:
        obj: 검증 대상 (보통 `parse_llm_json` 결과).
        keys: 필수 키 목록.

    Returns:
        검증을 통과한 dict (그대로 반환).

    Raises:
        ValueError: dict가 아니거나 필수 키가 누락된 경우.
    """
    if not isinstance(obj, dict):
        raise ValueError(f"dict가 아님: {type(obj).__name__}")
    missing = [k for k in keys if k not in obj]
    if missing:
        raise ValueError(f"필수 키 누락: {missing}")
    return obj
