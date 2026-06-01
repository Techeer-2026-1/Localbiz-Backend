"""LangChain `BaseCallbackHandler` — ChatGoogleGenerativeAI 호출에 메트릭 후크 1군데서 통합.

각 ChatGoogleGenerativeAI 인스턴스 생성 시 `callbacks=[LLM_METRICS_CALLBACK]`로 주입하면
`on_llm_start`/`on_llm_end`/`on_llm_error`가 호출되어:
  - `langgraph_llm_calls_total{model, purpose, status}` Counter inc
  - `langgraph_llm_latency_seconds{model, purpose}` Histogram observe
  - `langgraph_llm_tokens_total{model, purpose, direction}` Counter inc (usage_metadata 있을 때)

`purpose`는 호출 사이트가 ContextVar(`_llm_purpose_var`)로 set한 값을 읽음 —
ContextVar 미설정 시 fallback `"unknown"`.

`booking_node`의 google-genai 직접 호출은 별도 `traced_call("gemini.booking_grounding")`로
계측 — 본 callback은 LangChain 객체 전용.
"""

from __future__ import annotations

import logging
import os
import time
from contextvars import ContextVar
from typing import Any, Optional
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler  # pyright: ignore[reportMissingImports]
from langchain_core.messages import BaseMessage  # pyright: ignore[reportMissingImports]
from langchain_core.outputs import LLMResult  # pyright: ignore[reportMissingImports]

from src.observability.metrics import (
    langgraph_llm_calls_total,
    langgraph_llm_cost_usd_total,
    langgraph_llm_latency_seconds,
    langgraph_llm_tokens_total,
)

# P3-D: Gemini 토큰 → USD 환산. 환경변수로 단가 조정.
# 기본값은 gemini-2.5-flash 공식 단가 (per 1M tokens, 2026 기준).
_GEMINI_USD_PER_M_INPUT = float(os.environ.get("GEMINI_COST_USD_PER_M_INPUT", "0.075"))
_GEMINI_USD_PER_M_OUTPUT = float(os.environ.get("GEMINI_COST_USD_PER_M_OUTPUT", "0.30"))

logger = logging.getLogger(__name__)

_llm_purpose_var: ContextVar[Optional[str]] = ContextVar("llm_purpose", default=None)


def set_llm_purpose(purpose: str) -> Any:
    """LangChain LLM 호출 직전 purpose 설정. 반환 토큰으로 reset."""
    return _llm_purpose_var.set(purpose)


def reset_llm_purpose(token: Any) -> None:
    try:
        _llm_purpose_var.reset(token)
    except (ValueError, LookupError):
        pass


def _resolve_purpose() -> str:
    val = _llm_purpose_var.get()
    return val if val else "unknown"


def _resolve_model(serialized: Optional[dict[str, Any]], **kwargs: Any) -> str:
    if isinstance(serialized, dict):
        params = serialized.get("kwargs") or {}
        model = params.get("model")
        if isinstance(model, str) and model:
            return model
    inv = kwargs.get("invocation_params") or {}
    model = inv.get("model")
    if isinstance(model, str) and model:
        return model
    return "unknown"


def _extract_token_counts(response: LLMResult) -> tuple[int, int]:
    """LLMResult에서 input/output 토큰 추출. 키가 없거나 형식 다르면 (0, 0)."""
    llm_output = getattr(response, "llm_output", None) or {}
    usage = llm_output.get("usage_metadata") or llm_output.get("token_usage") or {}
    input_keys = ("prompt_token_count", "input_tokens", "prompt_tokens")
    output_keys = ("candidates_token_count", "output_tokens", "completion_tokens")
    in_tok = next((int(usage[k]) for k in input_keys if k in usage and usage[k] is not None), 0)
    out_tok = next((int(usage[k]) for k in output_keys if k in usage and usage[k] is not None), 0)
    if in_tok == 0 and out_tok == 0:
        for gen_list in response.generations or []:
            for gen in gen_list:
                msg = getattr(gen, "message", None)
                if msg is None:
                    continue
                u = getattr(msg, "usage_metadata", None) or {}
                if isinstance(u, dict):
                    in_tok += int(u.get("input_tokens") or 0)
                    out_tok += int(u.get("output_tokens") or 0)
    return in_tok, out_tok


class LLMMetricsCallback(BaseCallbackHandler):
    """모든 ChatGoogleGenerativeAI 호출의 latency / 토큰 / 성공·에러 카운트."""

    def __init__(self) -> None:
        super().__init__()
        self._start_times: dict[str, float] = {}
        self._models: dict[str, str] = {}
        self._purposes: dict[str, str] = {}

    def _key(self, run_id: Optional[UUID]) -> str:
        return str(run_id) if run_id is not None else "no-run-id"

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        del prompts
        key = self._key(run_id)
        self._start_times[key] = time.monotonic()
        self._models[key] = _resolve_model(serialized, **kwargs)
        self._purposes[key] = _resolve_purpose()

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        del messages
        key = self._key(run_id)
        self._start_times[key] = time.monotonic()
        self._models[key] = _resolve_model(serialized, **kwargs)
        self._purposes[key] = _resolve_purpose()

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        key = self._key(run_id)
        start = self._start_times.pop(key, None)
        model = self._models.pop(key, "unknown")
        purpose = self._purposes.pop(key, "unknown")
        if start is not None:
            langgraph_llm_latency_seconds.labels(model=model, purpose=purpose).observe(time.monotonic() - start)
        langgraph_llm_calls_total.labels(model=model, purpose=purpose, status="ok").inc()
        in_tok, out_tok = _extract_token_counts(response)
        if in_tok > 0:
            langgraph_llm_tokens_total.labels(model=model, purpose=purpose, direction="input").inc(in_tok)
            # P3-D: USD 환산 누적 (per 1M tokens 단가)
            langgraph_llm_cost_usd_total.labels(model=model, purpose=purpose, direction="input").inc(
                in_tok * _GEMINI_USD_PER_M_INPUT / 1_000_000
            )
        if out_tok > 0:
            langgraph_llm_tokens_total.labels(model=model, purpose=purpose, direction="output").inc(out_tok)
            langgraph_llm_cost_usd_total.labels(model=model, purpose=purpose, direction="output").inc(
                out_tok * _GEMINI_USD_PER_M_OUTPUT / 1_000_000
            )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        del error, kwargs
        key = self._key(run_id)
        start = self._start_times.pop(key, None)
        model = self._models.pop(key, "unknown")
        purpose = self._purposes.pop(key, "unknown")
        if start is not None:
            langgraph_llm_latency_seconds.labels(model=model, purpose=purpose).observe(time.monotonic() - start)
        langgraph_llm_calls_total.labels(model=model, purpose=purpose, status="error").inc()


# 싱글톤 인스턴스 — 모든 ChatGoogleGenerativeAI 생성 시 `callbacks=[LLM_METRICS_CALLBACK]`로 주입.
LLM_METRICS_CALLBACK: LLMMetricsCallback = LLMMetricsCallback()
