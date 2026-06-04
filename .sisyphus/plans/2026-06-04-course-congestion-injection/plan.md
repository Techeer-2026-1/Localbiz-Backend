# Course stop congestion 주입

- Status: APPROVED
- Phase: P2 (장소 분석 / 혼잡도 확장)
- Date: 2026-06-04
- Author: Claude (Opus 4.7, 1M context)
- Origin: FE/BE 혼잡도 연동 점검 결과 (`/init` command-args, 2026-06-04). 관련 사전 작업: `docs/be-requests.md` #3 (Places 블록 congestion).

---

## 1. 요구사항

코스 추천 (`CourseBlock.stops[].place`) 에 혼잡도 필드를 주입해 FE 코스 카드에 혼잡도 뱃지가 표시되도록 한다. PlaceBlock 에 이미 적용된 패턴 (`place_recommend_node.py:731-748`) 을 동일 스키마·동일 호출 흐름으로 재사용한다. FE 측 어댑터/렌더링은 사전 적용 완료 — BE 주입만으로 활성화.

### 수용 기준 (Acceptance Criteria)

| # | 기준 |
|---|---|
| AC1 | `CoursePlaceInfo` (blocks.py) 에 `congestion: Optional[CongestionInfo] = None` 필드 존재 |
| AC2 | `course_plan_node` 메인 경로에서 unique districts → `asyncio.gather(return_exceptions=True)` → cong_map → 각 stop place 에 주입 |
| AC3 | `_handle_refinement` 의 replace/add 분기에서 신규 stop place 에도 동일 주입 |
| AC4 | `fetch_congestion_by_district` 실패 / None 반환 시 graceful (예외 전파 X, congestion=None) |
| AC5 | 신규 테스트 3건: happy path / failure graceful / refine replace |
| AC6 | `./validate.sh` 통과 (ruff / format / pyright / pytest) |

## 2. 영향 범위

| 파일 | 변경 |
|---|---|
| `backend/src/models/blocks.py` | +1 line — `CoursePlaceInfo.congestion` 필드 |
| `backend/src/graph/course_plan_node.py` | +17 lines (메인 주입) / +12 lines (refine 주입) / +1 line (`_build_blocks` place dict) |
| `backend/tests/test_course_plan_node.py` | +130 lines — congestion 테스트 3건 |

총 LOC: ~160 추가 / 0 수정 (순수 추가).

## 3. 19 불변식 체크리스트

- [x] #1 PK 이원화: 변경 없음 (CoursePlaceInfo 는 places.place_id UUID 사용 유지)
- [x] #2 PG ↔ OS 동기화: 변경 없음 (DB 스키마 변경 X)
- [x] #3 append-only: 변경 없음 (write 경로 무관)
- [x] #4 소프트 삭제: 변경 없음
- [x] #5 의도적 비정규화: place.district 비정규화 사용 — 기존 패턴
- [x] #6 6 지표: 변경 없음 (congestion 은 6 지표와 무관)
- [x] #7 임베딩 768d: 변경 없음
- [x] #8 asyncpg 파라미터 바인딩: `fetch_congestion_by_district` 가 기존 `$1`, `$2` 사용 — 재호출만
- [x] #9 Optional[str]: 신규 코드 `Optional[CongestionInfo]` 사용
- [x] #10 SSE 이벤트 16종: 변경 없음 (course 블록 내부 필드 추가, 새 type 추가 아님)
- [x] #11 intent 별 블록 순서: 변경 없음 (course 블록 자체 순서 유지)
- [x] #12 공통 쿼리 전처리: 변경 없음
- [x] #13 행사 검색 순서: 무관
- [x] #14 대화 이력 이원화: 무관
- [x] #15 이중 인증: 무관
- [x] #16 북마크 패러다임: 무관
- [x] #17 공유링크: 무관
- [x] #18 Phase 분리: P2 라벨 (장소 분석 확장)
- [x] #19 기획 우선: FE/BE 양측 스키마 합의 (`CongestionInfo {level, updated_at, source}`) 일치

## 4. 작업 순서

1. **`blocks.py`** — `CoursePlaceInfo` 에 `congestion` 필드 1 라인 추가 (PlaceBlock 동형).
2. **`course_plan_node.py` 메인 경로** — `route` 확정 후 (`_pick_by_sequence` / `_greedy_nn_route` 직후), `_build_blocks` 호출 이전에 unique districts 추출 → `asyncio.gather(return_exceptions=True)` → `cong_map` → 각 `p["congestion"]` 주입.
3. **`course_plan_node.py` `_build_blocks`** — stop place dict 의 `summary` 다음에 `"congestion": place.get("congestion")` 1 라인 추가.
4. **`course_plan_node.py` `_handle_refinement`** — `replace`/`add` 분기의 신규 stop 생성 직후, `apply_replace`/`apply_add` 호출 이전에 `fetch_congestion_by_district` 단발 호출 → `new_stop["place"]["congestion"]` 주입.
5. **`test_course_plan_node.py`** — 신규 테스트 3건:
   - `test_course_plan_node_congestion_injection`: unique districts 호출 횟수 검증 + 모든 stop 매핑 확인
   - `test_course_plan_node_congestion_failure_graceful`: `side_effect=RuntimeError` → congestion None, 예외 없음
   - `test_refine_replace_injects_congestion`: refine replace 후 신규 stop 의 congestion 매핑 확인
6. **`./validate.sh`** — 전체 통과 확인 (ruff / format / pyright / pytest / 기획 무결성 / plan 무결성).

## 5. 검증 계획

| 단계 | 검사 | 통과 기준 |
|---|---|---|
| Static | `ruff check . && ruff format --check .` | 0 issues |
| Type | `pyright src/models/blocks.py src/graph/course_plan_node.py tests/test_course_plan_node.py` | 0 errors |
| Unit | `pytest backend/tests/test_course_plan_node.py -v` | 신규 3건 포함 모든 테스트 통과 |
| Integration | `pytest backend/tests/ -q` | 396+ 테스트 통과, 회귀 0 건 |
| Project | `./validate.sh` | 6 단계 전부 통과 |
| Manual (선택) | `curl -N` SSE 스트림 → `course.stops[].place.congestion` 키 존재 | 강남/홍대/성수 3개 구역 검증 |

### Risks & Mitigations

| Risk | Mitigation |
|---|---|
| N+1 DB 쿼리 (stop 5개 × 조회) | `set(districts)` unique 추출 + `asyncio.gather` 병렬 — 평균 2~3 call/코스 |
| `fetch_congestion_by_district` 실패가 코스 전체 응답을 깸 | `try/except + return_exceptions=True` 이중 graceful 처리 |
| Pydantic extra field 경고 | Step 1 에서 명시 필드 추가 — 경고 없음 |
| 기존 테스트 회귀 | 28 → 31 (신규 3건만 추가), 기존 mock 흐름 변경 없음 |
| `population_stats` ETL 누락으로 production None 응답 다수 | 별도 follow-up (`docs/be-requests.md` #2) — 본 plan 범위 밖 |

---

## Approval

- [x] PM (이정) — `/team /ralph "둘 다 순차적"` 명시적 실행 승인 (2026-06-04)
- [ ] FE (이정원) — 사후 PR review sign-off 대기 (사전 어댑터 적용 완료됨)
- [x] **최종 결정: APPROVED**
