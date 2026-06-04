# CLAUDE.md 메타 문서 정합성 수정

- Status: APPROVED
- Phase: meta (문서 정합성 — 코드 무영향)
- Date: 2026-06-04
- Author: Claude (Opus 4.7, 1M context)
- Origin: `/init` 분석 중 발견한 CLAUDE.md ↔ 코드 ↔ CONTRIBUTING.md ↔ backend/AGENTS.md 간 불일치.

---

## 1. 요구사항

`CLAUDE.md` 가 future Claude 인스턴스의 운영 가이드인데, 세 가지 드리프트가 존재한다:

1. **Intent 권위 불일치** — `CLAUDE.md` flow (14건) ↔ `backend/AGENTS.md` "12+1" (FAVORITE 포함, EVENT_RECOMMEND/CALENDAR 누락) ↔ 실제 `intent_router_node.py:IntentType` (16 enum / 15 routable). 권위가 분산됨.
2. **CONTRIBUTING.md 의 destructive op 격리 룰 (2026-04-10 사고)** 이 CLAUDE.md "절대 금지" 에 미반영.
3. **`_archive` 디렉토리 정책** (ruff extend-exclude, 참조 전용) 이 CLAUDE.md 미언급. CONTRIBUTING.md 표기 (`_legacy_*`) 와 실제 패턴 (`_archive`) 가 다름 — 실측 (`pyproject.toml:4`) 기준 정정.

부가 보강:
- pre-commit hook 자동 적용 동작 한 줄
- 불변식 #10 의 `text_stream` 영속 동작 실측 (`sse.py:489`) 반영 — `{"type":"text_stream","content":full_text}` 로 저장 (TextBlock 변환 X).

### 수용 기준 (Acceptance Criteria)

| # | 기준 |
|---|---|
| AC1 | CLAUDE.md flow 다이어그램 아래 "intent 권위는 `backend/src/graph/intent_router_node.py:IntentType`" 한 줄 추가 |
| AC2 | CLAUDE.md "절대 금지" 에 destructive chain (`A && B \|\| rm`) + `${VAR:?msg}` 가드 + `_archive` 수정 금지 3 bullet 추가 |
| AC3 | CLAUDE.md 에 pre-commit hook 자동 적용 한 줄 |
| AC4 | 불변식 #10 의 `text_stream` 설명에 실측 영속 동작 명시 |
| AC5 | `./validate.sh` 통과 (md 변경) |

## 2. 영향 범위

| 파일 | 변경 |
|---|---|
| `CLAUDE.md` | +8~12 lines, 기존 라인 수정 0 (순수 추가) |
| (별건) `backend/AGENTS.md`, `CONTRIBUTING.md` | follow-up 이슈로 분리 — 본 plan 범위 밖 |

## 3. 19 불변식 체크리스트

본 plan 은 마크다운 문서 1 종 수정으로 **코드 영향 0**. 19 불변식 전부 변경 없음.

- [x] #1~#18: 무관 (메타 문서)
- [x] #19 기획 우선: CLAUDE.md 자체가 본 plan 의 대상 — 단일 권위 강화 방향이라 합치.

## 4. 작업 순서

1. **AC1**: CLAUDE.md `### LangGraph Flow` 다이어그램 직후에 권위 라인 1줄 추가.
2. **AC2**: CLAUDE.md `## 절대 금지` 섹션에 3 bullet 추가:
   - bash `||` fallback 에 destructive op 금지 (2026-04-10 사고)
   - 변수 치환은 `"${VAR:?msg}"` 가드 필수
   - `backend/_archive/` 수정 금지 — ruff `extend-exclude=["_archive"]` (`pyproject.toml:4`)
3. **AC3**: CLAUDE.md "Common Commands" 직후 또는 "코드리뷰 체크리스트" 앞에 pre-commit hook 자동 적용 1 라인.
4. **AC4**: 불변식 #10 의 `text_stream` 설명에 실측 영속 동작 1 라인 (sse.py:489).
5. `./validate.sh` 통과 확인.

## 5. 검증 계획

| 단계 | 검사 | 통과 기준 |
|---|---|---|
| 문법 | markdown lint (수동 read-through) | 깨진 link / 표 없음 |
| 사실 정합성 | `grep IntentType backend/src/graph/intent_router_node.py` 와 추가 라인 일치 | 16 enum / 15 routable 확인 |
| 사실 정합성 | `grep extend-exclude backend/pyproject.toml` 결과 = `["_archive"]` 와 추가 라인 일치 | 정확 매칭 |
| 사실 정합성 | `sse.py:489` 의 `assistant_blocks.append({"type":"text_stream","content":full_text})` 확인 | 정확 매칭 |
| Project | `./validate.sh` | 6 단계 전부 통과 (특히 plan 무결성) |

### Out of Scope

- `backend/AGENTS.md` 의 "12+1" 표기 정정 — follow-up 이슈
- `CONTRIBUTING.md` 의 `_legacy_*` → `_archive` 정정 — follow-up 이슈
- `기획/AGENTS.md` — 기획서 변경 프로토콜 (PM 합의 + 버전 bump) 별건

---

## Approval

- [x] PM (이정) — `/team /ralph "둘 다 순차적"` 명시적 실행 승인 (2026-06-04). CLAUDE.md 변경은 본인 권한.
- [ ] BE 1명 sign-off — 사후 PR review 로 대체
- [x] **최종 결정: APPROVED**
