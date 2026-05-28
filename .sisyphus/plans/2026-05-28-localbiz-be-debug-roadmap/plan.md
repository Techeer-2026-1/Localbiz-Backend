# Plan: LocalBiz BE 버그 디버깅 로드맵

- Phase: P1–P3 (디버깅 — 다중 Phase 기능 교차)
- 상태: 최종 결정: APPROVED (PM 이정 승인 2026-05-28 — 단계 A부터 착수)
- 유형: 우산(roadmap) plan — 각 클러스터는 착수 시 개별 plan으로 분리
- 근거 문서: `docs/2026-05-28-localbiz-be-bug-triage.md`

## Context

QA 결과 BE에서 다수 기능이 오작동(가장 치명적인 "응답 안 옴" 다발 포함). 코드 직접 조사로 10개 클러스터(C1–C10)의 근본 원인 또는 가설을 확보했다. 이 로드맵은 **무엇을 어떤 순서로 고칠지**를 정의하고 의존성·중복 작업을 제거한다. 실제 수정은 각 단계(묶음) 착수 시 별도 plan으로 진행한다.

핵심 통찰: "응답 안 옴"(C1)의 상당수는 전처리 다중 지역 파싱 실패(C2)가 상류 원인이며, 장소추천 동일 사유(C10)는 keyword name-ILIKE(C3)의 하류 증상이다. → 묶어서 고치면 작업량 대비 효과가 크다.

## 1. 요구사항

- BE에서 고칠 수 있는 10개 클러스터를 우선순위·의존성에 따라 단계화한다 (FE 미연결·운영 제외).
- 각 단계는 (a) 근본 원인 재현/확정 → (b) 최소 수정 → (c) 검증 순의 "디버깅" 흐름을 따른다.
- 🔶(가설) 항목은 수정 전 **재현·계측으로 원인 확정 단계를 반드시 포함**한다.
- 데이터/인프라 성격(C6 population_stats, C9 OS 인덱스, C8 GCS 권한)은 코드 트랙과 분리해 별도 트랙으로 표기한다.
- C5(캘린더)는 BE/FE 계약 결정이 선행 — 합의 전 코드 착수 금지.

## 2. 영향 범위

| 클러스터 | 주요 파일 | 성격 |
|---|---|---|
| C1 응답안옴 | `src/api/sse.py`, 각 노드 empty-path | 코드 |
| C2 다중지역 파싱 | `src/graph/query_preprocessor_node.py`, `src/graph/intent_router_node.py`, `src/utils/geo_mapping.py` | 코드 |
| C3 keyword ILIKE | `src/graph/place_recommend_node.py:98-100` | 코드 ✅ |
| C4 코스 슬롯 오염 | `src/graph/course_plan_node.py` | 코드 |
| C5 캘린더 N장소 | `src/graph/calendar_node.py`, `src/api/calendar.py` | BE/FE 계약 |
| C6 혼잡도 보통 | `population_stats`(데이터), `src/graph/crowdedness_node.py:266` | 데이터 |
| C7 예약 프롬프트 | `src/graph/booking_node.py` | 프롬프트 |
| C8 이미지 업로드 | `src/api/upload.py`, `src/graph/image_search_node.py` | 코드+인프라 |
| C9 최신 장소 | OS `places_vector`, `scripts/etl/load_vectors.py` | 데이터 |
| C10 동일 사유 | `src/graph/place_recommend_node.py` rerank | 설계(C3 종속) |

테스트 영향: `backend/tests/`의 `test_query_preprocessor.py`, `test_review_compare_node.py`, `test_event_recommend.py`, `test_place_recommend_node.py`, `test_course_plan_node.py`, `test_crowdedness_node.py`, `test_image_search_node.py`, `test_sse_history.py`, `test_multi_intent.py`.

## 3. 19 불변식 체크리스트

이 로드맵의 수정은 기능 버그 픽스 위주로, 다음 불변식을 **유지(위반 금지)**해야 한다. 단계별 plan에서 재확인.

- [ ] #8 DB 쿼리 파라미터 바인딩 유지 — C3에서 `name ILIKE` 제거하되 asyncpg `$n` 바인딩 유지, f-string SQL 금지.
- [ ] #9 `Optional[str]` 문법 유지 (`str | None` 금지) — 모든 신규/수정 시그니처.
- [ ] #10 SSE 16종 콘텐츠 블록 + 제어 이벤트(`status`/`done_partial`) 경계 유지 — C1 안전망이 새 블록 타입을 만들지 않고 기존 `text`/`error`만 사용.
- [ ] #11 intent별 블록 순서 고정 — C1/C4 수정이 블록 순서를 바꾸지 않음.
- [ ] #12 공통 쿼리 전처리(Gemini JSON mode) 위치 유지 — C2는 프롬프트/파싱 강화이지 단계 이동이 아님.
- [ ] #13 행사 검색 DB→Naver fallback 순서 유지 — C1 타임아웃 추가가 순서를 바꾸지 않음.
- [ ] #3 append-only(messages 등) UPDATE/DELETE 금지 — C1 메시지 저장 경로 변경 시 준수.
- [ ] #7 임베딩 768d Gemini 통일 — C3/C9 검색 경로에서 OpenAI 미사용.
- [ ] #6 6지표 고정 — C6/C10 사유·점수 관련 수정이 지표명·개수 변경 안 함.

## 4. 작업 순서

각 단계 = 별도 PR + 별도 하위 plan(`.sisyphus/plans/{date}-{slug}/`). 단계 내부는 **재현 → 원인 확정 → 수정 → 검증**.

### 단계 A (P0, 최우선): "응답 안 옴" + multi-intent — C1 + C2
- 의존: 없음. C2(상류 파싱)를 먼저, C1(하류 안전망)을 함께.
- 디버깅: 3개 재현 쿼리("홍대점이랑 명동점 리뷰 비교", "명동 다음주 음악 행사", "명동 카페 + 홍대 밥집")로 `query_preprocessor` 출력(district/keywords/날짜)과 각 노드 `response_blocks`를 로깅 계측 → 빈 블록/파싱 누락 지점 확정.
- 수정: (1) `query_preprocessor` 프롬프트 복수 지역/장소 추출 + few-shot, `geo_mapping` 구어 지명 매핑 보강. (2) multi-intent `sub_query` 지역 보존(`_CLASSIFY_MULTI_SYSTEM_PROMPT` 예시). (3) `sse.py` intent 종료 시 콘텐츠 블록 0개면 `text` 안내 블록 보장. (4) Naver/임베딩 외부호출 `utils/resilience` 타임아웃.
- 산출: "응답 안 옴" 다발 + 중첩 요청 두 번째 지역 동시 해소.

### 단계 B (P1): 검색 품질 — C3 → C10, 그리고 C4
- 의존: C10은 C3 수정 후 재평가(별도 착수 불필요할 수 있음).
- C3(빠른 win): `place_recommend_node._search_pg`의 keyword `name ILIKE`(98-100) 제거, 의미 키워드는 OS k-NN으로 라우팅. `name ILIKE`는 고유명사 질의 한정.
- C4: `course_plan_node._build_blocks`에서 place 슬롯은 `_greedy_nn_route()` 실제 place만 사용, LLM 출력은 메타데이터 한정, stop 수≠route 수 시 fallback.
- 검증: C3 후 "명동 쉴만한 곳"이 실제 카페 반환·사유 다양화 확인. C4 multi-area 코스에 실제 장소명만.

### 단계 C (P2 코드): C7 예약 프롬프트 + C8 이미지 업로드
- C7: `booking_node` JSON mode + 필드 고정 + grounding 0건 fallback.
- C8: `upload.py`/`image_search_node` timeout 명시 + 재시도 + 에러 카테고리 로깅.

### 트랙 D (데이터/인프라, 코드와 병렬): C6 + C9 + C8 인프라
- C6: `population_stats` 적재 범위·최신 base_date 확인, 부족 시 ETL 재적재.
- C9: OS `places_vector` 재적재(`scripts/etl/load_vectors.py`) + 고유명사 PG 우선 랭킹.
- C8 인프라: GCS 라이프사이클/WIF SA 권한(`storage.objectAdmin`, `iam.serviceAccountTokenCreator`)/쿼터 확인.

### 합의 게이트: C5 캘린더
- 이정원과 "코스 N장소 → N이벤트" vs "1이벤트+description" 계약 결정 후 별도 plan.

## 5. 검증 계획

- 각 단계 PR 전 루트에서 `./validate.sh` 통과 (ruff/pyright/pytest/기획·plan 무결성).
- 단계 A: 위 3개 재현 쿼리로 로컬 서버(`uvicorn src.main:app --reload`) SSE 수동 검증 — 빈 응답 없음, 두 지역 모두 결과. 회귀 테스트 `test_multi_intent.py`·`test_review_compare_node.py`·`test_event_recommend.py` 보강.
- 단계 B: `test_place_recommend_node.py`에 "의미 키워드는 name ILIKE를 타지 않는다" 케이스 추가. `test_course_plan_node.py`에 multi-area place_id 유효성 케이스.
- 단계 C: `test_image_search_node.py` 타임아웃/재시도 케이스, booking JSON 스키마 케이스.
- 트랙 D: MCP postgres(read-only)로 `population_stats` 최신 base_date·커버리지 실측, OS 인덱스 도큐먼트 수 대조.
- 단계별 착수 시 `code-reviewer`(또는 `ecc:python-reviewer`/`ecc:fastapi-reviewer`)로 리뷰.

## Risks

| Risk | 가능성 | 완화 |
|---|---|---|
| C2 프롬프트 강화가 다른 단일-지역 쿼리 회귀 유발 | 중 | few-shot에 단일/복수 예시 병기, 기존 회귀 테스트 유지 |
| C1 안전망이 정상 빈-결과 UX와 충돌(이중 안내) | 중 | 노드가 이미 안내 블록을 내면 sse 안전망은 미발동(블록 0개일 때만) |
| C3 name ILIKE 제거로 고유명사 검색 약화 | 중 | 고유명사 판별 분기 유지(가게명 질의는 ILIKE 경로) |
| 🔶 가설(C4/C7/C8) 실제 원인 상이 | 중 | 각 단계 1순위가 재현·계측로 원인 확정 후 수정 |
| 데이터 트랙(C6/C9)이 코드로 안 풀림 | 높 | 별도 트랙·담당 분리, 코드 PR과 비결합 |

## Acceptance

- [ ] 단계 A–C 각각 별도 plan + PR로 분리 착수, `./validate.sh` 통과
- [ ] 단계 A 후 "응답 안 옴" 3개 재현 쿼리 + multi-intent 정상
- [ ] 19 불변식 위반 없음 (#8/#9/#10/#12/#13 특히)
- [ ] 🔶 항목은 수정 전 원인 확정 단계 기록
- [ ] C5는 FE 계약 합의 후에만 착수
