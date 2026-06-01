"""Request-scoped ContextVar — 노드·외부 호출·로그가 동일 trace_id 외 식별자(request_id/user_id/thread_id)를 공유하기 위한 컨텍스트.

SSE 핸들러 진입 시 set, 종료 시 reset. asyncio TaskGroup·anyio TaskGroup에서 자동 inherit.

Phase 1에서는 정의만 두고 SSE 핸들러 주입은 Phase 2(로깅 PR)에서 동시에 들어간다 —
Phase 2의 `TraceContextFilter`가 이 ContextVar를 읽어 로그 레코드에 주입.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

# 모듈 import 시점에 기본값 None으로 초기화. 진입부에서 set, 종료에서 reset 권장.
request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
user_id_var: ContextVar[Optional[int]] = ContextVar("user_id", default=None)
thread_id_var: ContextVar[Optional[str]] = ContextVar("thread_id", default=None)
