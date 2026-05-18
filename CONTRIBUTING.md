# Contributing to AnyWay

> 이 문서는 *기존 팀원·신규 팀원* 모두가 자력으로 PR을 머지할 수 있게 하는 최소 핸드북.
> 셋업이 처음이면 먼저 [`README.md`](README.md) 의 6단계 onboarding 완료할 것.

---

## 0. 핵심 원칙 4개

1. **plan-driven** — 코드 짜기 전에 plan 작성.
2. **기획 문서 권위** — 코드와 충돌 시 기획서가 옳음. 기획 변경은 plan으로.
3. **19 불변식** — [`CLAUDE.md`](CLAUDE.md) 의 19개 룰. 위반 = 머지 거부.
4. **destructive op 격리** — rm/mv/||/$VAR 같은 줄 금지.

---

## 1. 워크플로 (issue → plan → branch → PR → merge)

### 1.1 issue 작성

[Issue templates](.github/ISSUE_TEMPLATE/) 에서 종류 선택:
- 🐛 bug — 재현 가능한 결함
- ✨ feature — 새 기능 (plan 필수)
- 🔧 chore — 리팩토링/문서/CI

### 1.2 plan 작성 (feature/non-trivial chore)

생성 위치: `.sisyphus/plans/{YYYY-MM-DD}-{slug}/plan.md`
APPROVED 라인이 plan.md 마지막에 있어야 코드 편집을 진행할 수 있음.

### 1.3 branch + commit

브랜치 prefix:
- `feat/` — 새 기능
- `fix/` — 버그 fix
- `refactor/` — 리팩토링
- `docs/` — 문서
- `chore/` — 인프라/의존성
- `test/` — 테스트만

```bash
git checkout -b feat/place-recommend-node
```

커밋 메시지:
- `feat: PLACE_RECOMMEND 노드 추가 (사유 표시 포함)`
- `fix: opensearch SSL 인증 거부 처리`
- `docs: README dev-environment 링크 갱신`
- `chore: ruff 0.16 업그레이드`
- `refactor: intent_router_logic INTENT_TO_NODE 매핑 분리`

**main 직접 commit 금지** (pre-commit hook 차단).

### 1.4 PR 생성

```bash
gh pr create --title "feat: PLACE_RECOMMEND 노드 추가" --body "$(cat <<'EOF'
[plan link]
[19 불변식 체크]
[검증]
EOF
)"
```

PR template ([.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md)) 가 자동 로드됨. 19 불변식 체크박스 + plan 링크 + 검증 결과 필수.

### 1.5 머지 전 체크

- [ ] CI green (`validate` workflow 통과)
- [ ] 리뷰어 1명 이상 approve
- [ ] PR template 모든 체크박스
- [ ] plan 상태가 `APPROVED`
- [ ] 충돌 해결

main 머지는 **squash merge** 권장 (커밋 history 단순화).

---

## 2. validate.sh 6단계

```bash
./validate.sh
```

| 단계 | 검사 | 실패 시 |
|---|---|---|
| 1 | venv 활성화 | `cd backend && python3.11 -m venv venv && pip install -r requirements.txt -r requirements-dev.txt` |
| 2 | ruff check | `cd backend && ruff check --fix .` (사용자 승인) |
| 3 | ruff format --check | `cd backend && ruff format .` |
| 4 | pyright basic | 메시지에 따라 코드 수정. legacy는 `_legacy_*`로 mv |
| 5 | pytest | 수집 0건이면 자동 스킵 |
| 6 (bonus) | 기획 무결성 + plan 무결성 | `기획/v5/` 같은 stale link / 6 지표 누락 / plan 필수 섹션 누락 |

CI에서도 동일한 6단계가 실행됨 (`.github/workflows/validate.yml`).

---

## 3. <a name="hook-troubleshooting"></a>트러블슈팅

### destructive op 격리

`&& ... || rm -rf` chain은 fail 시 fallback으로 destructive op가 실행될 수 있음 (2026-04-10 사고).

```bash
# ❌ 금지
mkdir -p foo && do_thing || rm -rf foo

# ✅ 권장 — 별도 호출
mkdir -p foo
do_thing
[ $? -ne 0 ] && rm -rf "${foo:?}"  # 별도 if 블록
```

변수 가드:
```bash
# ❌ 금지
rm -rf $TARGET/old

# ✅ 권장
rm -rf "${TARGET:?TARGET must be set}/old"
```

### append-only 테이블 UPDATE/DELETE

**해결**: 19 불변식 #3 — messages/feedback/population_stats/langgraph_checkpoints는 INSERT only. SQL을 INSERT로 바꾸거나 다른 테이블 사용.

### ruff/pyright 실패

코드 수정. UP045(`X | None`) 발생 시 → CLAUDE.md 정책 `Optional[str]` 사용.

### pre-commit hook fail

```bash
cd backend
pre-commit run --all-files  # 로컬 진단
# 메시지에 따라 수정 → git add -A → git commit 재시도
```

---

## 4. 자주 묻는 것

### Q. 빠른 1줄 fix인데도 plan 작성이 필요한가?

**아니오**. 1줄 fix는 plan 면제. 단:
- 19 불변식과 무관해야 함
- ERD 영향 없어야 함
- intent/응답 블록 변경 없어야 함

### Q. main에 직접 push 하고 싶다

**불가**. pre-commit `no-commit-to-branch` + GitHub branch protection (require PR review). 응급 hotfix도 PR 거쳐야 함.

### Q. legacy 코드를 수정해도 되나?

`backend/_legacy_src/`, `backend/_legacy_scripts/` 는 **참조 전용**. ruff/pyright 검사 제외 (`extend-exclude`). 수정 금지. 새 코드를 `backend/src/`, `backend/scripts/` 에 작성.

### Q. 새 의존성 추가는?

`backend/requirements.txt` (런타임) 또는 `backend/requirements-dev.txt` (개발 도구) 에 핀. PR 설명에 *왜 필요한지* + *대안 검토* 명시.

### Q. 1Password 자격증명 노출됨

즉시 PM(이정) DM. DB_PASSWORD/API key 즉시 회전. 노출 경로 추적 (.env 커밋? Slack? screenshot?).

---

## 5. 배포 체크리스트

### Google Calendar OAuth 실배포 시 필수

로컬 개발 환경에서는 `localhost` URI로 동작하지만, 실배포 시 아래 두 가지를 반드시 처리해야 함.

**① GCP Console 설정**
`console.cloud.google.com` → 사용자 인증 정보 → AnyWay OAuth 클라이언트 ID → 승인된 리디렉션 URI에 추가:
```text
https://실제도메인/api/v1/auth/google/calendar/callback
```

**② 서버 .env 수정**
```bash
GOOGLE_CALENDAR_REDIRECT_URI=https://실제도메인/api/v1/auth/google/calendar/callback
```

코드 수정 없이 이 두 가지만 하면 됨. 로컬용 localhost URI는 그대로 두면 개발 환경에서도 계속 동작함.

---

## 6. 참고

- `README.md` — 7단계 onboarding
- `CLAUDE.md` — 19 불변식 + 절대 금지
- `docs/dev-environment.md` — DB/OS 셋업
- `기획/AGENTS.md` — 기획 문서 변경 규약
- `backend/AGENTS.md` — backend 디렉터리 가이드
- `.sisyphus/plans/TEMPLATE/plan.md` — plan 양식
