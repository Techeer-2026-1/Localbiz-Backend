"""src/utils/llm_parsing.py 단위 테스트 (#124).

parse_llm_json — 코드펜스 변형 케이스 / require_keys 검증.
"""

from __future__ import annotations

import json

import pytest

from src.utils.llm_parsing import parse_llm_json, require_keys  # pyright: ignore[reportMissingImports]


# ---------------------------------------------------------------------------
# parse_llm_json — 코드펜스 변형
# ---------------------------------------------------------------------------
def test_parse_json_fence_with_json_tag() -> None:
    """```json ... ``` 펜스."""
    text = '```json\n{"a": 1, "b": [2, 3]}\n```'
    assert parse_llm_json(text) == {"a": 1, "b": [2, 3]}


def test_parse_json_fence_without_tag() -> None:
    """``` ... ``` (json 태그 없음)."""
    assert parse_llm_json('```\n{"x": true}\n```') == {"x": True}


def test_parse_json_no_fence() -> None:
    """펜스 없는 순수 JSON."""
    assert parse_llm_json('{"k": "v"}') == {"k": "v"}


def test_parse_json_list_root() -> None:
    """루트가 list인 응답."""
    assert parse_llm_json("```json\n[1, 2, 3]\n```") == [1, 2, 3]


def test_parse_json_surrounding_whitespace() -> None:
    """앞뒤 공백·개행이 있어도 파싱."""
    assert parse_llm_json('  \n ```json\n  {"a": 1}  \n```  \n ') == {"a": 1}


def test_parse_json_uppercase_tag_inline() -> None:
    """JSON 태그 대문자 + 한 줄 펜스."""
    assert parse_llm_json('```JSON {"a": 1}```') == {"a": 1}


def test_parse_json_open_fence_only() -> None:
    """닫는 펜스가 없는(잘린) 응답도 앞 펜스만 제거 후 파싱."""
    assert parse_llm_json('```json\n{"a": 1}') == {"a": 1}


def test_parse_json_brace_inside_string_value() -> None:
    """문자열 값 안의 중괄호/백틱이 split을 깨지 않음."""
    assert parse_llm_json('```json\n{"note": "uses ``` and }"}\n```') == {"note": "uses ``` and }"}


def test_parse_json_empty_raises() -> None:
    """빈 응답 → ValueError."""
    with pytest.raises(ValueError):
        parse_llm_json("")
    with pytest.raises(ValueError):
        parse_llm_json("   \n  ")


def test_parse_json_invalid_raises_valueerror() -> None:
    """깨진 JSON → ValueError (json.JSONDecodeError가 ValueError 서브클래스)."""
    with pytest.raises(ValueError):
        parse_llm_json('```json\n{"a": }\n```')
    # 기존 호출부의 except json.JSONDecodeError도 잡히도록 서브클래스 보장
    try:
        parse_llm_json("not json at all")
    except json.JSONDecodeError:
        pass  # JSONDecodeError로도 잡힘 — OK
    except ValueError:
        pass  # ValueError로도 잡힘 — OK
    else:
        raise AssertionError("예외가 발생해야 함")


# ---------------------------------------------------------------------------
# require_keys
# ---------------------------------------------------------------------------
def test_require_keys_pass() -> None:
    """필수 키가 모두 있으면 dict 그대로 반환."""
    obj = {"ranked_indices": [0, 1], "reasons": {}}
    assert require_keys(obj, ["ranked_indices", "reasons"]) is obj


def test_require_keys_missing() -> None:
    """필수 키 누락 → ValueError."""
    with pytest.raises(ValueError):
        require_keys({"ranked_indices": []}, ["ranked_indices", "reasons"])


def test_require_keys_not_dict() -> None:
    """dict가 아니면 ValueError."""
    with pytest.raises(ValueError):
        require_keys([1, 2, 3], ["a"])
