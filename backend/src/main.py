"""AnyWay backend — FastAPI 진입점.

FastAPI 앱 객체를 생성하고, 서버 시작/종료 시 DB 커넥션 풀을 관리한다.
각 API 라우터(auth, users, chats, sse 등)를 여기서 등록한다.

핵심 개념:
  - FastAPI: Python 비동기 웹 프레임워크. Flask와 비슷하지만 async/await 네이티브.
  - lifespan: 서버 시작 시 1번 실행 → yield → 서버 종료 시 1번 실행. DB 풀 관리에 사용.
  - include_router: 다른 파일에서 정의한 엔드포인트 그룹을 앱에 연결.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.health import health_check  # pyright: ignore[reportMissingImports]
from src.observability.logging import configure_logging  # pyright: ignore[reportMissingImports]

# JSON 구조화 로깅 + trace_id/request_id/user_id/thread_id 자동 주입.
# LOG_FORMAT=plain 환경변수로 평문 fallback 가능 (로컬 개발용).
configure_logging()


def _install_global_llm_callback() -> None:
    """모든 ChatGoogleGenerativeAI 인스턴스에 LLM_METRICS_CALLBACK을 자동 부착.

    각 호출 사이트(~15곳)에 callbacks 파라미터를 일일이 추가하지 않기 위해 __init__를
    monkey-patch한다. 동일 callback 인스턴스가 중복 부착되지 않도록 dedup 처리.
    """
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI  # pyright: ignore[reportMissingImports]

        from src.observability.llm_callback import LLM_METRICS_CALLBACK  # pyright: ignore[reportMissingImports]
    except Exception:
        logging.getLogger(__name__).warning("LLM callback 주입 실패 — Gemini 호출 메트릭 미수집", exc_info=True)
        return

    orig_init = ChatGoogleGenerativeAI.__init__

    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        cbs = kwargs.get("callbacks") or []
        try:
            cb_list = list(cbs)
        except TypeError:
            cb_list = []
        if LLM_METRICS_CALLBACK not in cb_list:
            cb_list.append(LLM_METRICS_CALLBACK)
        kwargs["callbacks"] = cb_list
        orig_init(self, *args, **kwargs)

    ChatGoogleGenerativeAI.__init__ = patched_init  # type: ignore[method-assign]


# 모듈 import 시점에 1회만 패치 — 모든 노드가 import하기 전에 적용되도록.
_install_global_llm_callback()

logger = logging.getLogger(__name__)


async def _pool_gauge_loop(interval_seconds: float = 10.0) -> None:
    """asyncpg pool 사용량을 주기적으로 Prometheus Gauge에 반영.

    Pool 초기화 전이거나 일시적 RuntimeError 시 0으로 보고. 종료는 task cancel.
    """
    import asyncio

    from src.db.postgres import get_pool_in_use_count  # pyright: ignore[reportMissingImports]
    from src.observability.metrics import langgraph_pg_pool_in_use  # pyright: ignore[reportMissingImports]

    while True:
        try:
            langgraph_pg_pool_in_use.set(get_pool_in_use_count())
        except Exception:
            langgraph_pg_pool_in_use.set(0)
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """서버 시작/종료 시 리소스를 관리하는 lifecycle 함수.

    사용 이유:
      - DB 커넥션 풀은 서버 시작 시 한 번 만들고, 모든 요청이 공유한다.
      - 매 요청마다 새 DB 연결을 만들면 느리고 리소스 낭비.
      - 서버 종료 시 풀을 닫아야 DB 연결이 깨끗하게 정리된다.

    흐름:
      1. 서버 시작 → yield 위쪽 코드 실행 (DB pool, OS client 초기화)
      2. yield → 서버가 요청을 받기 시작
      3. 서버 종료 → yield 아래쪽 코드 실행 (pool/client 정리)
    """
    import asyncio

    _ = application  # FastAPI가 넘겨주지만 여기선 안 쓴다

    # --- Startup: 서버가 뜰 때 1번 실행 ---
    from src.db.opensearch import init_os_client  # pyright: ignore[reportMissingImports]
    from src.db.postgres import init_pool  # pyright: ignore[reportMissingImports]

    try:
        # asyncpg 커넥션 풀 생성 (min 2 ~ max 10 연결)
        # 이후 모든 API 핸들러가 get_pool()로 이 풀을 가져다 쓴다
        await init_pool()
        logger.info("PostgreSQL pool initialized")
    except Exception:
        logger.warning("PostgreSQL pool init failed (DB 미연결 시 정상)")

    try:
        await init_os_client()
        logger.info("OpenSearch client initialized")
    except Exception:
        logger.warning("OpenSearch client init failed (OS 미연결 시 정상)")

    pool_gauge_task = asyncio.create_task(_pool_gauge_loop())

    yield  # ← 이 지점에서 서버가 요청을 받기 시작

    # --- Shutdown: 서버가 내려갈 때 1번 실행 ---
    from src.db.opensearch import close_os_client  # pyright: ignore[reportMissingImports]
    from src.db.postgres import close_pool  # pyright: ignore[reportMissingImports]

    pool_gauge_task.cancel()
    try:
        await pool_gauge_task
    except (asyncio.CancelledError, Exception):
        pass

    await close_os_client()
    logger.info("OpenSearch client closed")

    await close_pool()
    logger.info("PostgreSQL pool closed")


# FastAPI 앱 객체 생성. uvicorn이 이 객체를 실행한다.
# 실행 명령: python -m uvicorn src.main:app --reload
app = FastAPI(
    title="AnyWay — LocalBiz Intelligence",
    description="서울 로컬 라이프 AI 챗봇",
    version="0.1.0",
    lifespan=lifespan,  # 위에서 정의한 lifecycle 함수 연결
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https://[a-zA-Z0-9-]+\.vercel\.app$|^http://localhost:\d+$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Prometheus 메트릭 노출 (#155). /metrics 엔드포인트로 기본 메트릭 5종 expose.
# `/health`·`/metrics` 자체 호출은 통계에서 제외(노이즈·자기참조 방지).
# `include_in_schema=False`로 OpenAPI 스펙엔 노출 안 됨.
# 로컬 한정 — GCE 배포 시 인증/IP allowlist 별도 처리 필요.
from prometheus_fastapi_instrumentator import Instrumentator  # noqa: E402  # pyright: ignore[reportMissingImports]

Instrumentator(
    excluded_handlers=["/health", "/metrics"],
).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

from src.telemetry import setup_tracing  # noqa: E402  # pyright: ignore[reportMissingImports]

setup_tracing(app)


# --- 라우터 등록 ---
# 각 파일에서 정의한 router를 앱에 연결.
# include_router 하면 해당 라우터의 모든 엔드포인트가 앱에 추가된다.
# 예: chats_router의 GET /api/v1/chats → app.get("/api/v1/chats")로 등록
from src.api.auth import router as auth_router  # noqa: E402, I001  # pyright: ignore[reportMissingImports]
from src.api.bookmarks import router as bookmarks_router  # noqa: E402  # pyright: ignore[reportMissingImports]
from src.api.place_bookmarks import router as place_bookmarks_router  # noqa: E402  # pyright: ignore[reportMissingImports]
from src.api.chats import router as chats_router  # noqa: E402  # pyright: ignore[reportMissingImports]
from src.api.calendar import router as calendar_events_router  # noqa: E402  # pyright: ignore[reportMissingImports]
from src.api.google_calendar_auth import router as google_calendar_auth_router  # noqa: E402  # pyright: ignore[reportMissingImports]
from src.api.share import router as share_router  # noqa: E402  # pyright: ignore[reportMissingImports]
from src.api.sse import router as sse_router  # noqa: E402  # pyright: ignore[reportMissingImports]
from src.api.upload import router as upload_router  # noqa: E402  # pyright: ignore[reportMissingImports]
from src.api.users import router as users_router  # noqa: E402  # pyright: ignore[reportMissingImports]

app.include_router(auth_router)  # /api/v1/auth/* 엔드포인트 (회원가입 등)
app.include_router(users_router)  # /api/v1/users/* 엔드포인트 (닉네임 변경 등)
app.include_router(bookmarks_router)  # /api/v1/users/me/bookmarks/* 엔드포인트 3개
app.include_router(place_bookmarks_router)  # /api/v1/users/me/place-bookmarks/* 엔드포인트 3개
app.include_router(chats_router)  # /api/v1/chats/* 엔드포인트 5개
app.include_router(sse_router)  # /api/v1/chat/stream SSE 엔드포인트
app.include_router(calendar_events_router)  # /api/v1/users/me/calendar/events GET/DELETE
app.include_router(google_calendar_auth_router)  # /api/v1/auth/google/calendar OAuth 2종
app.include_router(share_router)  # 공유 링크 3개
app.include_router(upload_router)  # /api/v1/upload/image 이미지 업로드


@app.get("/health")
def health(verbose: Optional[bool] = None) -> dict[str, str]:
    """Liveness probe — 컨테이너 헬스체크와 CI 확인용.

    Kubernetes/Docker에서 이 엔드포인트를 주기적으로 호출해서
    서버가 살아있는지 확인한다.
    """
    return health_check(verbose=verbose)
