# LocalBiz BE 버그 트리아지 (2026-05-28)

> 범위: **백엔드에서 고칠 수 있는 항목만**. FE 미연결(중단/재생성/피드백 버튼 미표시, 공유 링크 스크롤)·운영(모니터링, SSH 권한)은 제외.
> 출처: `backend/src` 코드 직접 조사. ✅ = 코드로 확인, 🔶 = 가설(추가 확인 필요).

## TL;DR — 우선순위

| # | 클러스터 | 심각도 | 성격 | 핵심 파일 |
|---|---|---|---|---|
| C1 | "응답 안 옴" (빈 블록 / hang) | **P0** | 코드 | `api/sse.py`, 각 노드 empty-path |
| C2 | query_preprocessor 다중 지역 파싱 | **P0** | 코드 | `graph/query_preprocessor_node.py`, `intent_router_node.py` |
| C3 | 장소 추천 keyword → name ILIKE | **P1** | 코드 | `graph/place_recommend_node.py:98-100` |
| C4 | 코스 추천 multi-area 장소 슬롯 오염 | **P1** | 코드 | `graph/course_plan_node.py` |
| C5 | 캘린더: 코스 첫 장소만 등록 | **P1** | 계약(BE/FE) | `graph/calendar_node.py` |
| C6 | 혼잡도 전부 "보통" | **P2** | 데이터 | `graph/crowdedness_node.py:266` |
| C7 | 예약 프롬프트 | **P2** | 프롬프트 | `graph/booking_node.py` |
| C8 | 이미지 업로드 간헐 실패 | **P2** | 코드+인프라 | `api/upload.py`, `graph/image_search_node.py` |
| C9 | 장소 검색 최신 장소 누락 | **P2** | 데이터 | OS `places_vector` 인덱스 |
| C10 | 장소 추천 사유 동일 | **P3** | 설계(C3 종속) | `place_recommend_node.py` rerank |

---

## 무엇 / 어떻게 / 왜 (한눈 요약)

| # | 무엇이 문제 | 어떻게 고침 | 왜 |
|---|---|---|---|
| C2 | 지역/장소 2개 쿼리에서 전처리가 첫 번째만 잡고 두 번째를 버림 → 두 번째 검색 0건 | 전처리 Gemini 프롬프트에 복수 지역/장소 추출 명시 + few-shot, multi-intent `sub_query` 지역 보존, `geo_mapping` 구어 지명 매핑 점검 | "응답 안 옴" 3종의 공통 상류 원인 — 한 곳 고치면 다수 해소 |
| C1 | 노드가 예외 없이 빈 블록 반환 → 내용 없는 `done`만 전송(빈 화면), 외부 호출 무한 대기(hang) | intent 종료 시 콘텐츠 블록 0개면 `text` 안내 블록 보장, Naver/임베딩에 timeout+retry | 0건일 때 빈 화면 대신 안내가 나와야 함(안전망), hang 방지 |
| C3 | `keyword`를 `name ILIKE`로 걸어 '쉴만한 곳'의 '휴식'이 가게 이름 '휴식' 매칭 | 의미 키워드는 ILIKE 제거→OS k-NN 의미검색, `name ILIKE`는 고유명사 한정 | 이름 매칭은 의미 검색이 아님 — 직접적 오답 원인, 수정 작음 |
| C10 | 모든 추천 장소에 동일 사유 텍스트 | C3 수정으로 후보 정상화 → 자동 개선, 잔존 시 per-place 사유 | C3의 garbage-in 하류 증상 |
| C4 | 다중 경유 코스의 장소 슬롯에 LLM 서술문("…서울의 낭만을")이 들어감 | place 슬롯은 실제 route place(place_id)만, LLM은 메타데이터만, 개수 불일치 시 fallback | 실제 좌표·예약 가능한 장소여야 코스가 쓸모 있음 |
| C5 | 코스 캘린더 등록 시 첫 장소만 반영 | (계약 결정) N장소→N이벤트 vs 1이벤트, 이정원과 합의 후 | BE 단독 버그 아님 — FE 계약 문제 |
| C6 | 혼잡도 전부 "보통" | 코드 아님 — `population_stats` 적재·최신 base_date 확인(ETL) | 데이터 없으면 `avg_pop=0`→무조건 "보통"(로직은 정상) |
| C7 | 예약이 free-text·fallback 없음 → 링크 품질 들쭉날쭉 | JSON mode + 필드 고정 + grounding 0건 fallback | 구조화해야 FE가 안정 파싱·표시 |
| C8 | 이미지 업로드 간헐 실패 | upload/download timeout 명시 + 재시도 + 에러 분류 로깅 | "어제 됨/오늘 안 됨"은 외부 의존성 일시 장애 → 재시도가 표준 처방 |
| C9 | 최신 장소(젠틀몬스터 사옥) 검색 누락 | OS `places_vector` 재적재 + 고유명사 PG 우선 랭킹 | PG엔 있어도 OS 인덱스 미동기 시 k-NN 누락 |

---

## C1. "응답 안 옴" 클러스터 (P0) — 리뷰 비교 / 행사 추천 / 특정 대화

**확인된 동작** (`api/sse.py`):
- ✅ 노드가 **예외를 던지면** `sse.py:502-506`(외부 try/except)에서 잡아 `error` 블록 + `done(status=error)` 방출 → 사용자는 에러 메시지를 봄. (SSE가 에러를 삼키는 게 아님)
- ✅ 노드가 **예외 없이 `response_blocks=[]`를 반환**하면: 스트리밍되는 블록이 없고 `assistant_blocks`가 비어 `sse.py:500`의 `done(status="done")`만 전송 → **화면상 빈 응답**.
- 🔶 외부 호출(Naver fallback, 임베딩)이 타임아웃 없이 대기하면 `graph.astream`이 yield를 안 해 **요청 hang** → 사용자 체감 "응답 안 옴".

**진짜 원인은 노드 단**: "응답 안 옴"을 만드는 노드가 빈 결과 경로에서 (a) 빈 리스트를 반환하거나 (b) 안내 텍스트 블록 없이 끝나거나 (c) 외부 호출에서 멈춤.

**수정 방향**:
1. **방어선(공통)**: `sse.py`에서 한 intent가 끝났는데 그 intent에서 콘텐츠 블록이 0개면 최소 1개의 `text` 안내 블록을 보장(예: "결과를 찾지 못했어요"). 빈 `done`이 화면 공백으로 보이지 않게.
2. **노드별**: `review_compare_node`·`event_recommend_node`·`event_search_node`의 빈/실패 경로가 **항상** `text`(또는 `disambiguation`) 블록을 append하는지 점검. (리뷰 비교는 disambiguation 반환이 확인되나, 이는 C2의 입력 파싱 실패가 선행 원인.)
3. **타임아웃**: Naver fallback / 임베딩 호출에 명시적 timeout + `utils/resilience.py:retry_call` 적용(이미 일부 노드는 사용 중).

---

## C2. query_preprocessor 다중 지역/장소 파싱 (P0) — C1·multi-intent의 공통 상류 원인

증상 3건이 모두 **전처리 단계에서 두 번째 지역/장소를 잃는 것**으로 수렴:
- 중첩 요청 "명동 카페 + 홍대 밥집" → 홍대 밥집 "검색 결과 없음". multi-intent 분리(`intent_router_node.py` `classify_intents`)에서 두 번째 `sub_query`가 지역("홍대") 컨텍스트를 잃거나, 잃지 않아도 `query_preprocessor`가 district로 해석 못 함.
- 리뷰 비교 "홍대점이랑 명동점" → 두 장소가 keywords로 안 쪼개져 `_extract_place_names()`가 빈 리스트 → disambiguation(C1).
- 행사 추천 "명동 다음주 음악 행사" → district("명동") 또는 날짜("다음주") 미해석 시 0건 → 빈 결과.

🔶 **수정 방향**:
1. `query_preprocessor` Gemini 프롬프트에 **복수 지역/장소 추출** 명시 + few-shot 예시("A와 B 비교", "X에서 ~하고 Y에서 ~").
2. `geo_mapping`(`utils/geo_mapping.py`)이 "홍대"·"명동" 같은 구어 지명 → district를 실제로 해석하는지 검증(미스 시 0건의 직접 원인).
3. multi-intent `sub_query`에 원본 지역어가 보존되도록 `_CLASSIFY_MULTI_SYSTEM_PROMPT` 예시 보강.

> ※ C1·C2는 사실상 한 작업으로 묶어 잡는 게 효율적. C2(상류 파싱) 고치면 C1 증상 상당수 해소, C1(하류 방어선)은 안전망.

---

## C3. 장소 추천 keyword → name ILIKE (P1) ✅

`graph/place_recommend_node.py:98-100`:
```python
if keywords:
    params.append(f"%{keywords[0]}%")
    sql += f" AND name ILIKE ${len(params)}"   # ← '쉴만한 곳'의 keyword '휴식'이 가게 이름 '휴식' 매칭
```
바로 위 `neighborhood`(94번)는 이미 `# name ILIKE 제거 — 노이즈 방지` 주석과 함께 address ILIKE로 전환됨. **동일 수정이 keywords에는 미적용**.

**수정 방향**: 의미 기반 키워드("쉴만한", "분위기 좋은")는 `name ILIKE`에서 제거하고 OS k-NN(`_search_os_places`/`_search_os_reviews`) 의미 검색으로 라우팅. `name ILIKE`는 고유명사(가게명) 질의에만. C10(동일 사유)은 이 garbage-in이 사라지면 상당 부분 개선.

---

## C4. 코스 추천 multi-area 장소 슬롯 오염 (P1) 🔶

`graph/course_plan_node.py` `_llm_course_compose()` — Gemini가 stop별 메타데이터를 반환하지만, 다중 경유(명동→이태원) 시 LLM이 "명동에서 이태원까지 서울의 낭만을…" 같은 **코스 서술문을 stop/place 슬롯에 넣음**. 코드가 LLM 응답의 stop 개수·구조를 검증하지 않음.

**수정 방향**: `_build_blocks`에서 place는 **항상 `_greedy_nn_route()`가 만든 실제 place 객체(place_id 보유)**에서만 채우고, LLM 출력은 `summary/arrival_time` 등 메타데이터로만 사용. stop 수 ≠ route 수면 identity 매핑 fallback.

---

## C5. 캘린더: 코스 첫 장소만 등록 (P1) — BE/FE 계약 🔶

`graph/calendar_node.py` `_extract_calendar_fields()` + `_create_event()` — 현재 **요청당 단일 이벤트** 생성(`location`=첫 장소, 나머지는 description에 텍스트 나열). Google Calendar는 이벤트당 location 1개.

**판단 필요**: (a) 코스 N개 장소 → N개 이벤트로 만들지(노드가 place 리스트 루프 + `_create_event` N회), (b) 1 이벤트 + description 유지. (a)면 BE 수정으로 해결 가능, FE 버튼 계약 확인 필요. → **이정원과 계약 합의 후 진행**.

---

## C6. 혼잡도 전부 "보통" (P2) — 데이터 ✅(로직)

`graph/crowdedness_node.py:266`: `level = "보통" if avg_pop == 0 else _classify_level(...)`. `_classify_level` 임계값(0.7/1.2)은 정상. `population_stats`에 해당 dong_code/time_slot/최근 base_date 데이터가 없으면 `avg_pop=0`(COALESCE) → 무조건 "보통".

**수정 방향**: 코드 아님. `population_stats` 적재 범위·최신 base_date 확인(ETL). 데이터 있으면 정상 분류됨.

---

## C7. 예약 프롬프트 (P2) 🔶

`graph/booking_node.py` — Gemini + Google Search grounding, free-text(`resp.text`) 반환. 구조화 출력 없음, 실패 시 fallback 없음.

**수정 방향**: JSON mode + 필드 고정(`{"booking_url","phone","notes"}`), grounding 0건 시 fallback, 링크 검증. 코드 변경 최소 + 프롬프트.

---

## C8. 이미지 업로드 간헐 실패 (P2) — 코드+인프라 🔶

`api/upload.py` — `blob.upload_from_string()` 명시적 timeout 없음, `credentials.refresh()` 재시도 없음, `except Exception` 광역 처리(에러 분류 없음). `graph/image_search_node.py` 다운로드 timeout=15 고정·재시도 없음.

**수정 방향(BE)**: 업로드/다운로드 timeout 명시 + 재시도, 에러 카테고리 로깅. **인프라 확인**: GCS 라이프사이클이 uploads/를 조기 삭제하는지, WIF SA에 `storage.objectAdmin`/`iam.serviceAccountTokenCreator` 있는지, GCS 쿼터.

---

## C9. 장소 검색 최신 장소 누락 (P2) — 데이터 🔶

"젠틀몬스터 사옥" 등 신규 장소가 PG엔 있어도 OS `places_vector` 인덱스에 없으면 k-NN 누락. `min_score=0.5` 임계로 약매칭 필터링.

**수정 방향**: OS 인덱스 재적재(`scripts/etl/load_vectors.py`) + 고유명사 질의 시 PG 정확매칭 결과를 OS보다 우선 랭킹.

---

## C10. 장소 추천 사유 동일 (P3) — C3 종속

`place_recommend_node.py` `_llm_rerank()`가 후보 전체에 대해 Gemini 1회 호출 → `reasons{place_id:text}` 반환. 후보가 C3의 노이즈('휴식' 가게들)면 사유도 동일 템플릿. **C3 수정 후 재평가**, 필요 시 per-place 사유 생성.

---

## 권장 작업 묶음 (효율 순)

1. **묶음 A (P0)**: C2 전처리 다중 지역 파싱 + C1 SSE/노드 빈-결과 안전망 + 외부호출 타임아웃 → "응답 안 옴" 다수 + multi-intent 동시 해소.
2. **묶음 B (P1)**: C3 keyword ILIKE 제거(작음) → C10 자동 개선 / C4 코스 슬롯 검증.
3. **묶음 C (P2)**: C7 예약 프롬프트 / C8 이미지 타임아웃·재시도 (코드), C6·C9 데이터 적재 (ETL·인프라 별도 트랙).
4. **합의 필요**: C5 캘린더 — 이정원과 N-이벤트 vs 1-이벤트 계약 결정 먼저.

> 각 묶음 착수 시 `.sisyphus/plans/`에 plan 작성 → 19 불변식 체크 → 구현 (CLAUDE.md 규약).
