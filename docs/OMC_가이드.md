# oh-my-claudecode (OMC) 사용 가이드

## 개요

**OMC(oh-my-claudecode)**는 Claude Code의 다중 에이전트 오케스트레이션 레이어로, 전문화된 에이전트와 스킬을 조율하여 복잡한 개발 작업을 자동으로 처리하는 시스템입니다. 요구사항 분석부터 구현, 테스트, 검증까지 전체 개발 라이프사이클을 자동화합니다.

**버전**: 4.14.0

---

## 핵심 명령어

| 명령어 | 설명 | 사용 예시 |
|--------|------|---------|
| `/autopilot` | 아이디어부터 완성된 코드까지 전체 자동 실행 | `/autopilot "REST API 사용자 관리 기능 만들어"` |
| `/ralph` | 작업 완료까지 반복 실행 (검증 필수) | `/ralph "모든 TypeScript 에러 고쳐"` |
| `/ultrawork` | 병렬 작업 실행 엔진 | `/ulw "3가지 독립적인 리팩토링 작업"` |
| `/team` | N개의 에이전트 팀으로 작업 분산 | `/team 5:executor "모든 TypeScript 에러 수정"` |
| `/deep-interview` | 소크라테스식 질문으로 사양 명확화 | `/deep-interview "뭘 만들지 애매해"` |
| `/ccg` | Claude + Codex + Gemini 3중 모델 오케스트레이션 | `/ccg "이 아키텍처 검토해줘"` |
| `/skillify` | 반복 워크플로우를 재사용 스킬로 변환 | `/skillify` |
| `/wiki` | 프로젝트 지식 베이스 (마크다운) | `/wiki` |
| `/cancel` | 실행 중인 모드 중단 | `/cancel` |

---

## 에이전트 목록

| 에이전트 | 모델 | 역할 |
|---------|------|------|
| **explore** | Haiku | 코드베이스 검색, 파일/패턴 찾기 |
| **planner** | Opus | 전략적 계획, 인터뷰 기반 작업 계획 |
| **architect** | Opus | 아키텍처 설계, 기술 검토 (읽기 전용) |
| **executor** | Sonnet | 구현, 다중 파일 코드 변경 |
| **designer** | Sonnet | UI/UX 구현, 인터페이스 디자인 |
| **code-reviewer** | Opus | 코드 리뷰, 품질/성능/보안 검토 |
| **debugger** | Sonnet | 버그 원인 분석, 빌드 오류 해결 |
| **verifier** | Sonnet | 작업 완료 검증, 증거 기반 확인 |
| **writer** | Haiku | 기술 문서작성 (README, API 문서) |
| **analyst** | Opus | 요구사항 분석 (사전 계획 단계) |
| **scientist** | Sonnet | 데이터 분석, 연구 실행 |
| **qa-tester** | Sonnet | 대화형 CLI 테스트 (tmux 사용) |
| **document-specialist** | Sonnet | 외부 문서, 레퍼런스 조사 |
| **critic** | Opus | 계획/코드 검토, 다각적 피드백 |
| **security-reviewer** | Opus | 보안 취약점 탐지 (OWASP Top 10) |
| **code-simplifier** | Opus | 코드 단순화, 가독성 개선 |
| **git-master** | Sonnet | Git 히스토리 관리, 커밋 분할 |
| **test-engineer** | Sonnet | 테스트 전략, TDD 워크플로우 |
| **tracer** | Sonnet | 원인 추적, 가설 검증 |

---

## 주요 스킬 상세

### 1. Autopilot — 전체 자동 실행

아이디어부터 검증된 완성 코드까지.

**언제 쓰나**: "만들어줘", "자동으로 해", 전체 라이프사이클이 필요할 때

**실행 단계**:
1. Phase 0 — 요구사항 분석 (확대)
2. Phase 1 — 구현 계획 작성
3. Phase 2 — 병렬 구현
4. Phase 3 — QA 순환 (최대 5회)
5. Phase 4 — 다각적 검증

```
/autopilot "완전한 사용자 인증 시스템 만들어 (로그인, JWT, 권한)"
```

### 2. Ralph — 완료 보장 루프

작업이 끝날 때까지 반복. "끝날 때까지 계속 해", "반드시 완료해"

**옵션**:
```
/ralph --no-deslop "작업"            # 정리 단계 건너뛰기
/ralph --critic=architect "작업"     # Architect로 검증
/ralph --critic=critic "작업"        # Critic으로 검증
```

```
/ralph "모든 TypeScript 에러 수정하고 테스트도 통과시켜"
```

### 3. Ultrawork — 병렬 실행

독립적인 작업들을 동시에 빠르게 실행.

```
/ulw "
1. 인증 모듈 리팩토링 (executor)
2. UI 컴포넌트 디자인 (designer)
3. 테스트 작성 (test-engineer)
"
```

### 4. Team — 팀 모드

N개의 에이전트가 공유 작업 리스트에서 동시 작업.

**문법**: `/team [N:agent-type] [ralph] "task"`

```
/team 5:executor "모든 TypeScript 에러 수정"
/team 3:debugger "빌드 에러 해결"
/team ralph "완전한 사용자 관리 REST API 구축"
```

### 5. Deep-Interview — 요구사항 명확화

모호한 아이디어를 소크라테스식 질문으로 명확한 사양으로 변환.

**옵션**:
```
/deep-interview --quick "description"       # 빠른 모드
/deep-interview --standard "description"    # 표준 모드
/deep-interview --deep "description"        # 깊은 모드
```

```
/deep-interview "앱 만들고 싶은데 정확히 뭘 원하는지 잘 모르겠어"
```

### 6. CCG — 3중 모델 합성

Claude + Codex + Gemini 관점을 병렬 수집 후 종합.

**사전 요구사항**:
```bash
npm install -g @openai/codex
npm install -g @google/gemini-cli
```

```
/ccg "이 아키텍처 검토해줘 - 백엔드 + 프론트엔드"
```

### 7. Skillify — 스킬 자동 생성

세션에서 발견한 반복 워크플로우를 재사용 스킬로 변환.

```
/skillify "이번에 배운 'TypeScript 마이그레이션 패턴'을 스킬로 만들어"
```

### 8. Wiki — 지식 베이스

세션 간 누적되는 프로젝트 지식 베이스.

| 작업 | 설명 |
|------|------|
| `wiki_query` | 키워드/태그로 검색 |
| `wiki_add` | 페이지 추가 |
| `wiki_ingest` | 지식 수집 (기존 페이지 병합) |
| `wiki_list` | 모든 페이지 보기 |
| `wiki_read` | 특정 페이지 읽기 |
| `wiki_lint` | 고아 페이지, 깨진 링크 탐지 |
| `wiki_delete` | 페이지 삭제 |

---

## 모델 라우팅

| 모델 | 사용 시점 | 비용 | 속도 |
|------|---------|------|------|
| **Haiku** | 빠른 검색, 단순 조회, 문서 쓰기 | 저렴 | 빠름 |
| **Sonnet** | 표준 구현, 코드 리뷰, 디버깅 | 중간 | 보통 |
| **Opus** | 복잡한 설계, 깊은 분석, 검증 | 비쌈 | 느림 |

---

## 키워드 트리거

OMC가 자동 감지하는 키워드:

| 키워드 | 스킬 |
|--------|------|
| `"autopilot"` | autopilot |
| `"ralph"` | ralph |
| `"ulw"` | ultrawork |
| `"ccg"` | ccg |
| `"ralplan"` | ralplan |
| `"deep interview"` | deep-interview |
| `"deslop"`, `"anti-slop"` | ai-slop-cleaner |
| `"cancelomc"` | cancel |

---

## 어떤 스킬을 써야 할까?

```
요구사항이 모호하다        → /deep-interview
계획이 필요하다            → /ralplan
전체 자동화가 필요하다     → /autopilot
완료 보장이 필요하다       → /ralph
독립적 작업이 여러 개다    → /ultrawork 또는 /team
코드 리뷰가 필요하다       → /ccg
반복 패턴을 저장하고 싶다  → /skillify
지식을 누적하고 싶다       → /wiki
실행을 중단하고 싶다       → /cancel
```

---

## 팁 & 주의사항

### 해야 할 것

1. **명확한 요청**: "만들어"보다 "사용자 로그인 기능 구현해 (JWT, 이메일 검증)"
2. **검증 우선**: 작업 완료 전에 항상 verifier 호출
3. **병렬화**: 독립적 작업은 ultrawork/team으로 동시 실행
4. **지식 저장**: 중요 발견사항은 wiki에, 반복 패턴은 skillify로

### 하지 말아야 할 것

1. **자기 검증 금지**: 작성자가 자기 코드를 검증하지 말 것 → 별도 reviewer 필수
2. **순차 처리 금지**: 독립적 작업을 하나씩 하지 말 것
3. **모호한 상태로 실행 금지**: 불명확하면 deep-interview 먼저

---

## 빠른 시작 예제

```bash
# 새 기능 구축
/autopilot "완전한 사용자 프로필 페이지 (표시, 수정, 삭제)"

# 버그 수정 (완료 보장)
/ralph "모든 단위 테스트 통과시키기"

# 여러 독립 작업 병렬
/team 4:executor "
1. API 에러 처리 개선
2. 로깅 시스템 추가  
3. DB 연결 타임아웃 수정
4. 캐시 전략 구현
"

# 복잡한 요구 명확화
/deep-interview "전자상거래 플랫폼 만들고 싶은데 뭘 먼저 할지 모르겠어"
```

---

*OMC 4.14.0 기준. 더 자세한 정보: `/omc-reference` 또는 https://github.com/Yeachan-Heo/oh-my-claudecode*
