# AnyWay — LocalBiz Intelligence

> 서울 로컬 라이프 AI 챗봇. 자연어 → 장소·행사·코스·분석.
> 하이브리드 검색 (PostgreSQL + PostGIS / OpenSearch 768d k-NN) + LangGraph + Gemini.

**팀** 이정 (BE/PM) · 정조셉 (BE) · 한정수 (BE) · 강민서 (BE) · 이정원 (FE)
**조직** [Techeer-2026-1](https://github.com/Techeer-2026-1)

---

## 🚀 Quick Start (clone 직후 30초 안내)

```bash
# 1. clone
gh repo clone Techeer-2026-1/LocalBiz-Seoul && cd LocalBiz-Seoul

# 3. backend venv + 의존성
cd backend
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 4. .env 작성 (1Password 값 채우기)
cp .env.example .env
$EDITOR .env

# 5. shell rc 에 DB_PASSWORD export (postgres MCP 용)
echo 'export DB_PASSWORD="<1Password 값>"' >> ~/.zshrc
source ~/.zshrc

# 6. 검증 + Claude Code 첫 prompt
cd ..
./validate.sh             # → ✅ 모든 검증 통과
claude                    # → "places 테이블 컬럼 조회해줘" 입력
```

**막히면**: [`docs/dev-environment.md`](docs/dev-environment.md) 트러블슈팅 / [`CONTRIBUTING.md` § Hook 차단](CONTRIBUTING.md#hook-troubleshooting) / PM 이정 DM.

자세한 단계별 설명은 [§ 6단계 Onboarding](#6단계-onboarding-목표-60분).

---

## 한 단락 설명

사용자가 자연어로 *"홍대에서 비 오는 날 갈 만한 분위기 카페"* 를 물으면, AnyWay는 12+1 intent로 분류 → 공통 쿼리 전처리 → PostGIS·k-NN 하이브리드 검색 → LangGraph 노드가 6 지표(만족도/접근성/청결도/가성비/분위기/전문성)로 추론 → 16종 SSE 이벤트 타입 (place/places/events/course/map_markers/chart/calendar/...)을 SSE 스트림으로 스트리밍한다. FE는 `EventSource` 또는 `@microsoft/fetch-event-source`로 수신. 코스 추천은 카테고리별 병렬 검색 → ST_DWithin → Greedy NN → OSRM 폴리라인까지. 데이터는 places 53만 (18 카테고리), events 7,301. 리뷰 분석은 런타임 Gemini 6 지표 lazy 채점 + 768d 임베딩으로 OpenSearch place_reviews에 배치 적재.

자세한 기획·아키텍처는 [노션 서비스 통합 기획서](https://www.notion.so/33d7a82c52e281f0a57fd84ac07c56f8) 참조 (source of truth).

---

## 디렉터리 구조

```
AnyWay/
├── backend/                  # FastAPI + LangGraph (Python 3.11)
│   ├── src/                  # 비즈니스 코드 (api, graph, models, ...)
│   ├── scripts/              # ETL, 마이그레이션, run_migration.py
│   ├── _archive/             # PoC 코드 (로컬 참조 전용, gitignored)
│   ├── tests/
│   ├── pyproject.toml        # ruff config
│   ├── pyrightconfig.json    # 타입 체커 (basic)
│   ├── .pre-commit-config.yaml
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   ├── .env.example          # 1Password 항목 명세
│   └── AGENTS.md             # backend 전용 agent 가이드
├── 기획/                      # source of truth (기획서 + ERD docx + AGENTS.md)
├── docs/
│   └── dev-environment.md    # Cloud SQL Auth Proxy / GCE OS / 1Password 셋업
├── .claude/                  # Claude Code 하네스
│   ├── hooks/                # Phase 1-3 hooks (강제 가드)
│   ├── skills/               # localbiz-* 스킬 + safe-destructive-ops
│   ├── agents/               # metis, momus 서브에이전트
│   └── settings.json
├── .sisyphus/                # Phase 3 Prometheus Planning
│   ├── plans/                # plan + reviews 영구 기록
│   ├── notepads/             # Phase 5 예약
│   └── state/                # 휘발성 (gitignore)
├── .github/
│   ├── workflows/            # CI (validate.sh)
│   ├── ISSUE_TEMPLATE/
│   └── PULL_REQUEST_TEMPLATE.md
├── CLAUDE.md                 # 19 데이터 모델 불변식 + 절대 금지 사항
├── validate.sh               # 6단계 검증 단일 진입점
├── frontend/                 # Next.js + Three.js (FE 합류 후 초기화)
│   └── README.md
├── .env.example
├── .gitignore
└── README.md (이 파일)
```

---

## 7단계 Onboarding (목표 60분)

### 1. Clone

```bash
gh repo clone Techeer-2026-1/LocalBiz-Seoul
cd AnyWay
```

### 2. 1Password — 공유 자격증명 받기

팀 1Password vault에서 **`AnyWay-Dev-Shared`** 항목 열람.
DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD, OPENSEARCH_HOST, GEMINI/ANTHROPIC/Google/Naver/Seoul API 키 모두 단일 항목에 들어 있음.

> 🔒 5인 팀 공유 단일 자격증명. 분실·노출 시 즉시 PM(이정)에게 알림.

### 3. Backend venv + 의존성

```bash
cd backend
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

### 4. .env 작성

```bash
cp .env.example .env
# 에디터로 .env 열어서 1Password 항목 값 채워 넣기
```

또한 shell rc(`~/.zshrc` 또는 `~/.bashrc`)에 다음 export 추가 (postgres MCP가 사용):

```bash
export DB_PASSWORD="<1Password 값>"
```

shell 재시작 후 `echo $DB_PASSWORD` 가 값을 출력해야 함.

> 🔧 Cloud SQL Auth Proxy / GCE OpenSearch SSH 터널 셋업은 [`docs/dev-environment.md`](docs/dev-environment.md) 참조.

### 5. oh-my-claudecode (OMC) 설치

Claude Code 멀티에이전트 오케스트레이션 플러그인. 팀 전원 설치 권장.

```bash
# 글로벌 설치
npm i -g oh-my-claude-sisyphus@latest

# backend 디렉토리에서 셋업
cd backend
omc setup
```

셋업 완료 후 Claude Code 세션에서 `/omc-setup`으로 인터랙티브 설정 가능. 주요 명령어:

| 명령어 | 설명 |
|---|---|
| `/team 3:executor "작업 설명"` | 팀 모드 (단계별 파이프라인) |
| `/autopilot "작업 설명"` | 자율 실행 모드 |
| `/ralph "작업 설명"` | 완료 보장 루프 |
| `/ultrawork "작업 설명"` | 최대 병렬 실행 |

자세한 사용법: [oh-my-claudecode GitHub](https://github.com/Yeachan-Heo/oh-my-claudecode)

### 6. validate.sh 통과 확인

```bash
cd ..  # 프로젝트 루트
./validate.sh
```

기대 출력: `✅ 모든 검증 통과` (6단계 ruff/format/pyright/pytest/기획무결성/plan무결성).

### 7. Claude Code 첫 prompt 시나리오

```bash
claude
```

세션 진입 후 다음을 입력:

> "places 테이블 컬럼 조회해줘"

`localbiz-erd-guard` 스킬이 자동 발동되고, postgres MCP로 information_schema가 조회되면 onboarding 완료.

---

## 핵심 룰 (반드시 읽기)

### 1. plan-driven workflow

코드 작성 *전에* `.sisyphus/plans/{YYYY-MM-DD}-{slug}/plan.md` 작성 → Metis/Momus 검토 → APPROVED → 구현. `localbiz-plan` 스킬이 자동 발동.

### 2. 19 데이터 모델 불변식

[`CLAUDE.md`](CLAUDE.md) 의 19개 룰 (PK 이원화, append-only 4테이블, 임베딩 768d, Optional[str], SSE 이벤트 타입 16종, ...) 위반은 hook이 차단. PR 머지 전 체크리스트 필수.

### 3. plan-driven · 하네스 hook 구조

| 단계 | Hook | 강제력 |
|---|---|---|
| 사용자 프롬프트 | `skill_router`, `intent_gate` | soft 인젝션 + planning_mode flag |
| Edit/Write/MultiEdit 전 | `pre_edit_skill_check`, `pre_edit_planning_mode` | hard block |
| Bash 전 | `pre_bash_guard` | destructive op 차단 (rm -rf $UNGUARDED, \|\| rm 등) |
| Edit/Write 후 | `post_edit_python` | ruff + pyright + append-only SQL 차단 |
| Skill 호출 후 | `skill_invocation_log` | pending 정리 |

자세한 규약은 `.claude/hooks/*.sh` 상단 주석.

### 4. 우회

`/force` 키워드를 프롬프트에 포함하면 skill_router/intent_gate가 인젝션 스킵. 단, `pre_bash_guard`(destructive op)·`post_edit_python`(ruff/pyright)는 우회 불가 — 코드를 안전하게 분리해야 함.

### 5. branch / commit 컨벤션

- 브랜치: `feat/`, `fix/`, `docs/`, `refactor/`, `chore/`, `test/`
- 커밋: `feat: ...`, `fix: ...`, `docs: ...`, `refactor: ...`, `chore: ...`
- main 직접 commit 금지 (pre-commit 차단)
- PR 머지 시 [PR template](.github/PULL_REQUEST_TEMPLATE.md) 의 19 불변식 체크박스 필수

자세한 워크플로는 [`CONTRIBUTING.md`](CONTRIBUTING.md) 참조.

---

## 자주 묻는 것

### Q. validate.sh가 ruff 못 찾는다고 함

```bash
cd backend && source venv/bin/activate && pip install -r requirements-dev.txt
```

### Q. Claude Code hook이 내 Edit를 차단함

차단 메시지 안내대로 진행:
- `pre_edit_skill_check`: 메시지에 적힌 스킬을 Skill 도구로 호출
- `pre_edit_planning_mode`: `.sisyphus/plans/.../plan.md`에 `최종 결정: APPROVED` 라인 추가하거나 사용자가 `/force` 입력
- `pre_bash_guard`: 명령을 별도 호출로 분리하거나 `${VAR:?}` 가드 추가

자세한 대처는 [`CONTRIBUTING.md` § Hook 차단 트러블슈팅](CONTRIBUTING.md#hook-troubleshooting).

### Q. postgres MCP가 연결 안 됨

DB_PASSWORD 환경변수 점검:
```bash
echo $DB_PASSWORD  # 비어 있으면 ~/.zshrc export 후 shell 재시작
```

자세한 진단: [`docs/dev-environment.md` § postgres MCP](docs/dev-environment.md).

---

## 라이선스 / 비공개

본 repo는 **private**. 외부 공유 금지. 1Password 자격증명 노출 시 PM 즉시 알림.
