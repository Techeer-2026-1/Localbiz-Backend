"""애플리케이션 설정 — 환경변수 로딩 (pydantic-settings).

싱글턴 패턴으로 한 번만 로딩. 환경변수 또는 .env 파일에서 읽음.
DB_*, OPENSEARCH_*, GEMINI_LLM_API_KEY,
JWT_SECRET, JWT_ALGORITHM, JWT_EXPIRE_MINUTES, GOOGLE_CLIENT_ID.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """환경변수 기반 설정. .env 파일도 지원."""

    # --- PostgreSQL (Cloud SQL) ---
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "localbiz"
    db_user: str = "postgres"
    db_password: str = ""

    # --- OpenSearch ---
    opensearch_host: str = "localhost"
    opensearch_port: int = 9200
    opensearch_user: str = "admin"
    opensearch_pass: str = ""

    # --- Gemini ---
    gemini_llm_api_key: str = ""

    # --- Google Calendar (calendar_node + OAuth) ---
    google_calendar_client_id: str = ""
    google_calendar_client_secret: str = ""
    google_calendar_redirect_uri: str = "http://localhost:8000/api/v1/auth/google/calendar/callback"
    frontend_base_url: str = "https://seoul-ai-guide.vercel.app"

    # --- App ---
    debug: bool = False

    # --- P3-A: intent_router + query_preprocessor 통합 (feature flag, 기본 OFF) ---
    # true 시 단일 Gemini 호출로 intent + preprocess. 정확도 evaluation 통과 후 ON.
    enable_combined_intent_preprocess: bool = False

    # --- JWT (Auth #4 회원가입 PR 도입) ---
    jwt_secret: Optional[str] = None
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 10080  # 7일 (60*24*7)

    # --- Google OAuth (Auth #5 Google 로그인 PR에서 사용 예정) ---
    google_client_id: str = ""

    # --- GCS (이미지 업로드 — feat/#103) ---
    gcs_bucket_name: str = ""

    # --- Naver Open API (event_search_node — DB fallback 검색) ---
    naver_client_id: str = ""
    naver_client_secret: str = ""

    # --- External APIs (booking_node) ---
    # Google Places Text Search v1 — 음식점/카페/주점 예약 URL 조회
    google_places_api_key: str = ""
    google_vision_api_key: str = ""
    # 서울시 공공서비스예약 API — P1은 URL 패턴만, 실제 API 연동은 후속 plan
    seoul_public_api_key: str = ""
    # KOPIS (공연예술통합전산망) API — P1은 URL 패턴만, 실제 API 연동은 후속 plan
    kopis_api_key: str = ""

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",  # .env에 Settings에 없는 변수가 있어도 무시 (에러 안 냄)
    }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """설정 싱글턴. 첫 호출 시 환경변수 파싱, 이후 캐시 반환."""
    return Settings()
