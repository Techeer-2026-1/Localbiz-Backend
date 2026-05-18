# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Coding Principles

### 1. Think Before Coding
- State assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First
- No features beyond what was asked. No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

### 3. Surgical Changes
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken. Match existing style.
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.
- Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution
- Transform tasks into verifiable goals with success criteria.
- For multi-step tasks, state a brief plan: `[Step] → verify: [check]`
- Loop until verified. Weak criteria ("make it work") require clarification.

---

# LocalBiz Intelligence (AnyWay)

서울 로컬 라이프 AI 챗봇. 자연어 → 장소/행사/코스/분석. 하이브리드 검색(PG+PostGIS / OpenSearch 768d k-NN) + LangGraph + Gemini.

**팀:** 이정(BE/PM) · 정조셉(BE) · 한정수(BE) · 강민서(BE) · 이정원(FE)
**Source of truth:** `기획/ERD_테이블_컬럼_사전_v6.3.md` / `기획/API 명세서 *.csv` / `기획/기능 명세서 *.csv` / `기획/ETL_적재_현황.md` (상세: `기획/_legacy/서비스 통합 기획서 v2.md`)
**개발 단계:** Phase 1-5 완료. Phase 6 (KAIROS) 대기.

## Common Commands

```bash
# 전체 검증 (PR 전 필수) — 프로젝트 루트에서 실행
./validate.sh   # 1)ruff check → 2)ruff format --check → 3)pyright → 4)pytest → 5)기획 무결성 → 6)plan 무결성

# Backend 서버
cd backend && source venv/bin/activate
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload

# 린터/포매터/타입체커 (backend/ 에서)
ruff check . && ruff format .
pyright src scripts

# 테스트 (backend/ 에서)
pytest                              # 전체
pytest tests/test_foo.py::test_bar -v  # 단일

# ETL 스크립트 (반드시 프로젝트 루트에서 PYTHONPATH=.)
PYTHONPATH=. python backend/scripts/etl/crawl_reviews.py --naver-only --category 음식점 --limit 20
PYTHONPATH=. python backend/scripts/etl/load_vectors.py --limit 100
```

## Architecture

### LangGraph Flow (`backend/src/graph/real_builder.py`)

```
SSE → intent_router → query_preprocessor → (조건부 라우팅)
  ├── PLACE_SEARCH     → place_search     → response_builder → END
  ├── PLACE_RECOMMEND  → place_recommend  → response_builder → END
  ├── EVENT_SEARCH     → event_search     → response_builder → END
  ├── EVENT_RECOMMEND  → event_recommend  → response_builder → END
  ├── COURSE_PLAN      → course_plan      → response_builder → END
  ├── DETAIL_INQUIRY   → detail_inquiry   → response_builder → END
  ├── BOOKING          → booking          → response_builder → END
  ├── CALENDAR         → calendar         → response_builder → END
  └── GENERAL          → general          → response_builder → END
```

**핵심 패턴**: `AgentState.response_blocks`는 `Annotated[list, operator.add]`로 정의되어 각 노드가 list를 반환하면 자동 append. `text_stream` 블록은 `{"type":"text_stream","system":"...","prompt":"..."}`을 `response_blocks`에 추가하면 `sse.py`에서 Gemini `astream()`으로 토큰 단위 스트리밍.

### Backend Module Layout

| 경로 | 역할 |
|---|---|
| `src/main.py` | FastAPI 앱 + lifespan (DB pool, OS 초기화/해제) |
| `src/api/sse.py` | SSE 핸들러, `text_stream` Gemini 스트리밍 |
| `src/config.py` | `get_settings()` 환경 변수 로딩 |
| `src/graph/state.py` | `AgentState` TypedDict (LangGraph 전체 상태) |
| `src/graph/real_builder.py` | `build_graph()` — StateGraph 구성 + 컴파일 |
| `src/graph/*_node.py` | LangGraph 노드 구현 (intent_router 등) |
| `src/models/blocks.py` | 16종 SSE 이벤트 타입 Pydantic 모델 + `CONTENT_BLOCK_TYPES` registry |
| `src/db/postgres.py` | asyncpg pool (init/close) |
| `src/db/opensearch.py` | OpenSearch 768d k-NN 벡터 검색 |
| `src/tools/` | ReAct 에이전트 도구 (Phase 1 이후 생성 예정) |
| `scripts/etl/` | ETL 스크립트 (`embed_utils.py`의 `embed_texts()` 공유) |

### Plan-Driven Workflow

코드 작성 전에 `.sisyphus/plans/{YYYY-MM-DD}-{slug}/plan.md` 작성 → APPROVED → 구현.

### Extension Points

- **새 노드**: `src/graph/*_node.py` → `real_builder.py`에 등록 → `intent_router_logic.py`에 매핑
- **새 도구**: `src/tools/` → `search_agent.py` 또는 `action_agent.py`의 tools 리스트에 등록
- **새 ETL**: `scripts/etl/` → `embed_utils.py`의 `embed_texts()` 사용, argparse + `--dry-run` 필수
- **새 응답 블록/intent**: 기획서 §4.5 먼저 업데이트 → `src/models/blocks.py` Pydantic 모델 → 노드 구현
- **DB 스키마 변경**: ERD 보고서 버전 bump → `scripts/migrations/` 마이그레이션 스크립트

## Tech Stack (변경 금지)

| Layer | 기술 |
|---|---|
| LLM | Gemini 2.5 Flash (메인) / Claude (이미지) — **OpenAI 임베딩 절대 금지** |
| 임베딩 | `gemini-embedding-001` 768d, nori, k-NN HNSW cosinesimil |
| Backend | FastAPI + LangGraph (Python 3.11) |
| DB | Cloud SQL PostgreSQL 16 + PostGIS / OpenSearch 2.17 (GCE) |
| FE | Next.js + Three.js (Vercel) |
| Infra | GCP — GCE(backend/OS/모니터링) + Cloud SQL + GitHub Actions |

## 19 데이터 모델 불변식 (위반 시 차단)

1. **PK 이원화**: places/events만 UUID(VARCHAR(36)). 나머지 BIGINT AI. administrative_districts는 자연키. ※ place_analysis는 v2에서 DROP (런타임 lazy 전환).
2. **PG↔OS 동기화**: place_id == places_vector._id (events / place_reviews 동일 패턴).
3. **append-only 4테이블**: messages, population_stats, feedback, langgraph_checkpoints에 UPDATE/DELETE 금지. updated_at·is_deleted 칼럼 없음.
4. **소프트 삭제**: 마스터/append-only/시계열/외부관리 테이블 제외. ERD §3 매트릭스가 source of truth.
5. **의도적 비정규화 3건만 허용**: places.district / events.{district,place_name,address} / *.raw_data(JSONB). ※ place_analysis.place_name은 v2 DROP으로 삭제.
6. **6개 지표 고정**: score_satisfaction/accessibility/cleanliness/value/atmosphere/expertise. 이름·개수 변경 금지.
7. **임베딩 통일**: 768d Gemini만. OpenAI 사용 시 PR 차단.
8. **DB 쿼리**: asyncpg 파라미터 바인딩(`$1`,`$2`) 필수. f-string SQL 금지. ORM 미사용.
9. **타입 힌트**: `Optional[str]` 사용. `str | None` 금지(파이썬 3.9 호환).
10. **SSE 이벤트 타입 16종 고정**: intent/text/text_stream/place/places/events/course/map_markers/map_route/chart/calendar/references/analysis_sources/disambiguation/done/error. ※ 16종은 messages.blocks JSON에 저장 가능한 **콘텐츠 블록** 한정. `status`(노드 전환 진행 표시)와 `done_partial`(multi-intent 구분자)은 **SSE 제어 이벤트**로 messages 미저장(런타임 한정)이므로 본 목록에서 제외 — 기획서 §4.5 "SSE 제어" 카테고리 참조.
11. **intent별 블록 순서 고정**: 기획서 §4.5. 변경하려면 .sisyphus/plans/ 작성 필요.
12. **공통 쿼리 전처리**: Intent Router 직후 모든 검색 기능 공통 (Gemini JSON mode).
13. **행사 검색 순서**: DB 우선 → 부족 시 Naver fallback. 역순 금지.
14. **대화 이력 이원화**: LangGraph checkpoint(LLM·압축가능) + messages(UI·append-only). 통합 금지.
15. **이중 인증**: auth_provider ∈ {email, google}. email → password_hash 필수, google → google_id 필수, 반대편 NULL.
16. **북마크 = 대화 위치 저장**: (thread_id, message_id, pin_type) 5종 핀. 즐겨찾기 패러다임 폐기됨.
17. **공유링크**: /shared/{share_token} GET만 인증 우회. 그 외 모두 JWT.
18. **Phase 분리**: P1=핵심대화/장소/코스/예약, P2=분석/북마크/공유, P3=피드백. 코드 추가 시 Phase 라벨 명시.
19. **기획 문서 우선**: 코드와 충돌 시 기획서가 옳음. 기획 변경은 .sisyphus/plans/ → PM 리뷰 → 버전 bump.

## 디렉토리 네비게이션

- `backend/` → 모듈 레이아웃·명령어·DB 스키마 상세는 `backend/AGENTS.md`.
- `기획/` → source of truth. 작업 규약은 `기획/AGENTS.md`. 코드와 충돌 시 기획이 우선.
- `.claude/` → `settings.json` / `settings.local.json`.
- `.sisyphus/plans/` → plan-driven workflow 영구 기록.
- `.mcp.json` → postgres MCP (read-only). information_schema 실측에 사용.

## 코드리뷰 체크리스트 (PR 머지 전 확인)

- [ ] 19 불변식 위반 없음 (특히 append-only / PK 타입 / 임베딩 / Optional 문법)
- [ ] SSE 이벤트 타입 추가/제거 시 기획서 §4.5 같이 업데이트
- [ ] DB 쿼리: 파라미터 바인딩 / 인덱스 활용 / N+1 없음
- [ ] async/await 일관성 (sync 래퍼 금지)
- [ ] 새 노드/도구 등록 위치 정확 (real_builder.py / search_agent.py / action_agent.py)
- [ ] `validate.sh` 통과
- [ ] 커밋 prefix: feat/fix/docs/refactor/test/chore. 브랜치 prefix: feat//fix//docs/. main 직접 커밋 금지.

## 절대 금지

- `.env` 커밋/업로드/외부 전송 금지 (API 키 유출 방지). 읽기는 개발 중 허용. `.env` 수정/생성 시 사용자 승인 필수.
- `git push --force`, `git reset --hard`, `--no-verify`, `docker-compose down -v`
- append-only 테이블 UPDATE/DELETE
- f-string SQL / OpenAI 임베딩 / `str | None` 문법 / ORM 도입
- 기획 문서를 코드 컨벤션에 맞춰 임의 수정
