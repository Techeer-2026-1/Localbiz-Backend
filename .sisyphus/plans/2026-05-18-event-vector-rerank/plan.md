# Plan: EVENT 검색 정확도 고도화 v2 — OS events_vector k-NN + LLM Rerank

- 작성일: 2026-05-18
- 개정: rev4 (critic 3회 검토 수렴 후 validate.sh plan 템플릿에 맞춰 재구성)
- 작성자: 한정수 (BE)
- 대상 이슈: #110 [FEAT] 검색 정확도 기능 고도화
- base 브랜치: dev / 작업 브랜치: feat/#110
- 선행 작업: #76 feat: EVENT 검색 정확도 강화 v1 — expanded_query + date_range
- Phase: P1 (핵심 대화/장소/코스/예약)

---

## 1. 요구사항

### 배경

#76(v1)에서 EVENT_SEARCH / EVENT_RECOMMEND는 `expanded_query` 키워드 배열을 PG
`title ILIKE` OR 매칭으로 확장하고 date_range overlap 필터를 적용했다. 그러나 검색은
여전히 **정형(키워드 문자열 매칭) 단일 채널**이라 표현이 다른 의미 질의("아이와 갈
만한 전시" 등)를 놓친다.

PLACE_RECOMMEND 노드는 이미 `PG 정형 + OS k-NN + Gemini LLM Rerank` 하이브리드 패턴을
구현해 두었다. EVENT 노드에도 동일 패턴을 차용해 의미 검색 채널과 순위 재배치를
추가한다. `events_vector` OpenSearch 인덱스는 **이미 7,301건 적재 완료**
(`기획/ETL_적재_현황.md` L45) — 신규 ETL 불필요.

### 목표 / 성공 기준

| # | 성공 기준 | 검증 방법 |
|---|---|---|
| G1 | EVENT_SEARCH / EVENT_RECOMMEND가 `events_vector` k-NN 의미 검색 결과를 PG 결과와 병합한다 | 단위 테스트: OS mock 결과가 병합 후보에 포함 |
| G1b | OS k-NN 후보 중 지난 행사(date_end < today)·NULL date가 최종 결과에서 제외된다 | 단위 테스트: 지난/NULL 행사 OS hit fixture → post-filter 제외 |
| G2 | 병합 후보를 Gemini Flash로 Rerank하여 상위 5건 순위 재배치 + 소개 생성 | 단위 테스트: rerank mock → 순서/사유, reranked↔description index 정렬 |
| G3 | Gemini/OS/임베딩 실패 시 graceful degradation | 단위 테스트: 예외·zero-vector 주입 시 fallback |
| G4 | Naver fallback이 PG∪OS 병합 < 3건일 때만 발동, Naver 결과가 Rerank 후보에서 잘리지 않음 | 단위 테스트: 병합 건수별 분기 + Naver 보존 |
| G5 | `validate.sh` 전체 통과 | `./validate.sh` |
| G6 | 의미 질의가 키워드 매칭이 놓치는 행사를 노출한다 (정확도 목표) | 통합 환경 수용 관찰 1건 (§5) |

### 비목표 (Out of Scope)

- 신규 SSE 블록 타입 / 블록 순서 변경 없음 (불변식 #10·#11 불변).
- `events_vector` 인덱스 재적재·매핑 변경 없음 (적재 완료 상태 사용).
- ST_DWithin 공간 필터 / user_location 도입 — 별도 작업.
- `query_preprocessor` 변경 없음 — `expanded_query`는 #76에서 이미 생성됨.
- EVENT_SEARCH / EVENT_RECOMMEND 외 다른 노드 변경 없음.
- `references` / `EventItem` 블록 payload 모델 정합화 — §3 참조, 기존 inert 결함이며 본 PR 범위 밖.

---

## 2. 영향 범위

### 변경 파일

| 파일 | 변경 |
|---|---|
| `backend/src/graph/event_search_node.py` | `_embed_query_768d` `_search_os_events` `_merge_candidates` `_llm_rerank` 추가, `_generate_event_descriptions` 제거, `_MIN_PG_RESULTS`→`_MIN_MERGED_RESULTS` 개명, 노드 흐름 수정, docstring 갱신 |
| `backend/src/graph/event_recommend_node.py` | 위와 동일 (추천 사유 강조 프롬프트 차별화 유지) |
| `backend/tests/test_event_search.py` | `_search_os_events`/`_merge_candidates`/`_llm_rerank` 단위 테스트 추가 |
| `backend/tests/test_event_recommend.py` | 위와 동일 |

**미변경 유지:** `_search_pg` / `_search_naver` / `_naver_to_event_dict` / `_build_blocks` 는
로직 변경 없음 — 노드 함수에서의 호출 위치만 바뀐다.

### `events_vector` 인덱스 구조

`_source` 필드 (load_vectors.py L283-298 실측):

```text
event_id(str) · title · description · embedding(768d) · category · district
· date_start(isoformat|null) · date_end(isoformat|null) · source
```

매핑 타입 (generate_os_structure.py L249-257 — 문서 기재값, 인덱스 생성 스크립트가
리포에 없어 미검증. 단 본 설계는 매핑 타입에 무관):

```text
embedding   knn_vector 768d (HNSW, nmslib 엔진, cosinesimil)
date_start  date     date_end  date     category/district/event_id  keyword
```
**주의:** `events_vector`에는 `place_name·address·lat·lng·price·poster_url·
detail_url·summary` 와 `is_deleted` 가 없다 → OS k-NN 결과는 모두 PG 2차 보강 필수.

---

## 3. 19 불변식 체크리스트

| 불변식 | 점검 |
|---|---|
| #2 PG↔OS 동기화 | `event_id == events_vector._id` 조회만. PG 부재 OS hit는 폐기로 비동기화 안전 처리 |
| #4 소프트 삭제 | PG 2차 보강 쿼리에 `is_deleted = FALSE` 명시 |
| #7 임베딩 768d Gemini | `_embed_query_768d`가 `gemini-embedding-001` 768d. OpenAI 미사용 |
| #8 파라미터 바인딩 | PG 2차 쿼리 `ANY($1::varchar[])` 바인딩, f-string SQL 금지 |
| #9 타입 힌트 | `Optional[...]` 사용, PEP604 union(`str` 파이프 `None`) 문법 금지 |
| #10 SSE 16종 | 신규 블록 없음 |
| #11 블록 순서 | `events → text_stream → references` 순서 불변 (`_build_blocks` 미변경) |
| #13 DB 우선→fallback | Naver는 PG∪OS 병합 < 3건 시에만 — DB(PG+OS) 우선 강화 |
| #18 Phase 라벨 | P1, docstring에 명시 |
| #19 로그 위생 | 사용자 query / API 키 logger 진입 금지 |

**기존 inert 결함 (본 PR 범위 밖):** 두 event 노드의 `_build_blocks`가 만드는 dict는
`blocks.py` Pydantic 모델(`EventItem`/`ReferenceItem`)과 필드가 어긋난다. #76 이전부터
존재하며 본 플랜이 도입하지 않았다. `EventsBlock`/`EventItem`을 검증하는
`deserialize_block`은 `backend/src/` 어디에서도 호출되지 않고(정의만 존재), `sse.py`는
Pydantic 인스턴스만 직렬화하며 event 노드는 plain dict를 emit한다 → 실행되지 않음(inert),
CI/런타임 무영향. 모델↔빌더 정합화는 별도 정리 이슈 권장.

---

## 4. 작업 순서

### 4.0 검색 흐름 (변경 후)

```text
processed_query (district/category/keywords/expanded_query/date_*_resolved)
  │
  ├─ ① PG 정형 검색         _search_pg()          [기존 유지]
  └─ ② OS events_vector k-NN  _search_os_events()  [신규]
  │     - plain knn으로 50건 over-fetch (expanded_query 768d)
  │     - Python date post-filter → 상위 10건
  │   (① ② asyncio.gather 병렬)
  │
  ③ 병합 + PG 2차 보강    _merge_candidates()      [신규]
  ④ Rerank 후보 한도 컷    merged[:_MAX_PRERANK]    [OS+PG만, Naver append 전]
  ⑤ Naver fallback        병합 < 3건일 때만        [발동 조건 변경]
  ⑥ LLM Rerank            _llm_rerank()           [신규] — Gemini Flash 1회
  ⑦ 블록 생성             _build_blocks()         [기존 유지]
```

### 4.1 지난 행사 처리 — over-fetch + Python post-filter (critic C1/MAJOR-1/2 해소)

`events_vector`(nmslib 엔진)는 k-NN 쿼리 내부 `filter` 절(efficient filtering)을
지원하지 않는다(`lucene`/`faiss` 전용). 따라서 date 필터를 OS 쿼리에 넣지 않고:

1. `_search_os_events`가 plain k-NN(PLACE_RECOMMEND와 동일 쿼리 형태)으로
   `_OS_CANDIDATE_K=50`건 over-fetch. `min_score` 0.4 동일 적용.
2. 응답 `_source`에서 `embedding` 제외 (50건 전송 오버헤드 최소화).
3. Python date post-filter:
   - 항상: `date_end`[:10] ≥ `today_iso` (종료 행사·NULL date_end 제외).
   - `date_start_resolved`·`date_end_resolved` **둘 다** 있을 때만 `date_start`[:10]
     ≤ `date_end_resolved`[:10] 추가 (`_search_pg` overlap 조건 mirror).
4. 유효 후보 상위 `_OS_TOP_K=10`건 반환.

엔진/매핑 무관하게 동작하고 score 분포가 PLACE_RECOMMEND와 동일하므로 `min_score`
재검증 부담이 없다.

### 4.2 신규 함수 (event_search_node.py / event_recommend_node.py 양쪽에 복제)

> Q1 결정(사용자 승인): 코드베이스 관례대로 복제. 두 파일은 이미
> `_search_pg`·`_search_naver`·`_naver_to_event_dict`를 중복 보유.

- **`_embed_query_768d`** — PLACE_RECOMMEND L48-68 복제. Gemini 768d 단건 임베딩.
- **`_search_os_events`** — §4.1. zero-vector 가드 포함(임베딩 전부 0.0이면 빈 list).
- **`_merge_candidates`** — OS hit event_id → PG 2차 조회
  `WHERE event_id = ANY($1::varchar[]) AND is_deleted = FALSE` (events PK는
  VARCHAR(36) UUID — ERD v6.3 L38). PG 부재 OS hit는 폐기. PG 조회 예외 시
  try/except graceful (PLACE_RECOMMEND L276 패턴). event_id 기준 중복 제거,
  병합 순서 OS 우선 → PG.
- **`_llm_rerank`** — Gemini Flash, index 기반(Naver 행사는 event_id=None). 응답
  `{"ranked_indices":[...], "reasons":{...}}` → `(reranked_top5, descriptions)`,
  descriptions는 reranked 순서 정렬. 실패 시 `(candidates[:5], [])` — 병합 순서 유지.
  시스템 프롬프트: SEARCH는 중립 소개 / RECOMMEND는 추천 사유 강조.

### 4.3 제거 / 개명

- `_generate_event_descriptions()` 제거 — Rerank가 reasons로 description 동시 생성.
- `_MIN_PG_RESULTS` → `_MIN_MERGED_RESULTS` 개명 (의미: PG → PG∪OS 병합).

### 4.4 노드 함수 흐름

```python
today_iso = date.today().isoformat()
pg_events, os_events = await asyncio.gather(_search_pg(...), _search_os_events(...))
merged = await _merge_candidates(pool, pg_events, os_events)
merged = merged[:_MAX_PRERANK]            # Naver append 전 컷 (Naver 보존)
if len(merged) < _MIN_MERGED_RESULTS:
    merged += [_naver_to_event_dict(x) for x in await _search_naver(...)]
reranked, descriptions = await _llm_rerank(merged, query, keywords)
blocks = _build_blocks(query, reranked, descriptions)
```
`_MAX_PRERANK`=10. OS가 유효 10건을 채우면 PG-only 정형 결과가 컷에 밀릴 수 있음 —
의미 채널이 v2의 새 가치이고 Rerank가 최종 순위를 잡으므로 허용.

### 4.5 구현 단계

1. OS `events_vector` 매핑 라이브 확인 — 매핑 무관 설계라 불일치 시에도 진행.
2. `event_search_node.py`: `_embed_query_768d` + `_search_os_events` 추가.
3. `event_search_node.py`: `_merge_candidates` 추가.
4. `event_search_node.py`: `_llm_rerank` 추가.
5. `event_search_node.py`: 노드 흐름 교체 + `_generate_event_descriptions` 제거 + 상수 개명.
6. `event_recommend_node.py`에 2-5 복제 (추천 사유 프롬프트 차별화).
7. 두 파일 docstring 갱신.
8. 단위 테스트 추가 + `validate.sh` 통과.

---

## 5. 검증 계획

### 단위 테스트

`test_event_search.py` / `test_event_recommend.py`에 추가:
- `_search_os_events`: date post-filter 제외(G1b), resolved-date 상한, zero-vector skip(G3).
- `_merge_candidates`: OS PG 2차 보강(G1), PG 부재 hit 폐기, PG 예외 graceful, 중복 제거.
- `_llm_rerank`: ranked_indices 재배치 + descriptions index 정렬(G2), 예외 fallback(G3),
  API 키 없음, 빈 후보.

### 전체 검증

- `./validate.sh` — ruff / format / pyright / pytest / 기획·plan 무결성 (G5).
- 노드 함수(`event_search_node`/`event_recommend_node`)는 DB/OS/Naver 실의존성이
  있어 단위 테스트는 헬퍼 함수 중심, 노드 전체는 통합 환경 manual 검증.

### G6 수용 관찰 (통합 환경)

OS·DB 연결된 통합 환경에서 의미 질의 1건("아이와 갈 만한 전시" 등)이 키워드 매칭이
놓치는 행사를 노출하는지 확인. `_search_os_events`의 score 분포 / 50건 over-fetch의
date post-filter 생존율도 함께 기록 — 생존율이 낮으면 `_OS_CANDIDATE_K` 상향.

---

## 6. 검토 상태

- [x] critic 1차 검토 (rev1) — REVISE — C1 + M1~M4 + 누락 4건
- [x] rev2 — C1~M4 1차 반영
- [x] critic 2차 검토 (rev2) — REVISE — MAJOR-1(knn.filter) MAJOR-2(contingency)
- [x] rev3 — knn.filter 폐기·over-fetch+post-filter 재설계로 MAJOR-1/2 근본 해소
- [x] critic 3차 검토 (rev3) — ACCEPT-WITH-RESERVATIONS
- [x] rev4 — MAJOR-1b/MAJOR-2 라벨링, date precedence 명시, validate.sh plan 템플릿 정합
- [x] APPROVED (2026-05-18) — critic 3회 검토 수렴, 사용자 승인. feat/#110 구현 완료.
