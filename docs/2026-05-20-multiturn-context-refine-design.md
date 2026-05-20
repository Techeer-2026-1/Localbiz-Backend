# 멀티턴 대화 컨텍스트 보강 + REFINE Intent 설계서

> 날짜: 2026-05-20
> 브랜치: feat/#135
> 상태: 설계 확정 → 구현 대기

## 1. 문제 정의

사용자가 이전 응답(코스, 장소 목록, 행사 등)의 수정을 요청할 때 엉뚱한 응답을 반환함.

**근본 원인 3가지:**
1. `sse.py:345` — 각 graph 실행 시 `conversation_history: []` 전달
2. `_stream_gemini()` — 대화 이력 없이 system_prompt + user_prompt만 사용
3. 각 노드 — 이전 턴의 구조화된 결과(place_id, stops 등)를 참조할 수 없음

## 2. 설계 요건

| 항목 | 결정 |
|---|---|
| 컨텍스트 방식 | 구조화된 블록(previous_blocks) 직접 주입 |
| 라우팅 | 신규 REFINE intent 추가 |
| 수정 유형 | 부분 교체/삭제/추가, 조건 변경, 전체 재생성 (5종) |
| 적용 대상 | places, events, course, chart, cost_estimate 등 모든 구조화된 응답 |
| 실행 방식 | REFINE 노드가 파싱 → 원본 노드 재호출 (방식 1: State 확장) |
| 대상 특정 | 기본 직전 응답, 명시적 참조 시 Gemini 추론 fallback |
| 이력 범위 | 최근 5턴(10메시지), 구조화된 블록 포함 |

## 3. 아키텍처

### 3.1 흐름도

```
사용자: "3번 장소 카페로 바꿔줘"
  ↓
SSE handler: _load_recent_history(5턴, 구조화 블록 포함)
  ↓
classify_intents → REFINE (confidence 0.9)
  ↓
intent_router_node → intent="REFINE"
  ↓
query_preprocessor_node (기존 동작)
  ↓
refine_node:
  1. 이전 응답 블록 로드 (DB messages → 직전 assistant blocks)
  2. Gemini로 수정 지시 파싱 → RefinementInstruction
  3. 원본 intent 판별 (course → COURSE_PLAN)
  4. state에 previous_blocks + refinement 주입
  5. 원본 노드 함수 직접 호출 (course_plan_node(state))
  ↓
원본 노드: previous_blocks 유무로 신규/수정 분기
  - 수정 모드: 이전 결과 기반으로 부분 수정 후 반환
  ↓
response_builder → SSE 전송
```

### 3.2 AgentState 확장

```python
# src/graph/state.py
class AgentState(TypedDict, total=False):
    # 기존 필드
    query: str
    intent: Optional[str]
    processed_query: Optional[dict[str, Any]]
    response_blocks: Annotated[list[dict[str, Any]], operator.add]
    thread_id: str
    user_id: Optional[int]
    error: Optional[str]
    conversation_history: list[dict[str, str]]

    # 신규 필드
    previous_blocks: Optional[list[dict[str, Any]]]  # 이전 응답 블록
    refinement: Optional[dict[str, Any]]              # 수정 지시 파싱 결과
    original_intent: Optional[str]                     # 수정 대상 원본 intent
```

### 3.3 RefinementInstruction 스키마

```python
# refine_node.py 내부
class RefinementInstruction(BaseModel):
    """Gemini가 파싱한 수정 지시."""
    action: str          # "replace" | "remove" | "add" | "change_condition" | "regenerate"
    target_index: Optional[int] = None    # 수정 대상 인덱스 (1-indexed, 부분 수정 시)
    target_id: Optional[str] = None       # 수정 대상 ID (place_id/event_id)
    new_condition: Optional[str] = None   # 교체/추가 조건 ("카페", "가성비 좋은")
    full_instruction: str = ""            # 원본 수정 요청 텍스트
```

### 3.4 REFINE intent 분류

`_CLASSIFY_MULTI_SYSTEM_PROMPT`에 REFINE 추가:

```
- REFINE: user wants to modify, replace, remove, or regenerate a previous response.
  Examples: "3번 장소 바꿔줘", "그거 말고 다른 거", "카페 빼줘", "다시 추천해줘",
  "강남 말고 홍대로", "마음에 안 들어", "2번 행사 다른 걸로"
```

### 3.5 refine_node 로직

```python
async def refine_node(state: dict[str, Any]) -> dict[str, Any]:
    """REFINE 노드 — 수정 요청 파싱 + 원본 노드 재호출."""

    thread_id = state.get("thread_id")
    query = state.get("query", "")

    # 1. 이전 응답 블록 로드
    previous_blocks = await _load_previous_blocks(thread_id)

    # 2. 수정 대상 특정 (기본: 직전, 명시적 참조 시 Gemini 추론)
    target_blocks, original_intent = await _identify_target(
        query, previous_blocks, state.get("conversation_history", [])
    )

    # 3. 수정 지시 파싱
    refinement = await _parse_refinement(query, target_blocks)

    # 4. state에 주입 + 원본 노드 호출
    state["previous_blocks"] = target_blocks
    state["refinement"] = refinement.model_dump()
    state["original_intent"] = original_intent
    state["intent"] = original_intent  # 라우팅용

    # 5. 원본 노드 직접 호출
    node_fn = _get_node_function(original_intent)
    result = await node_fn(state)

    return result
```

### 3.6 원본 노드 수정 패턴 (각 노드 공통)

```python
async def place_search_node(state: dict[str, Any]) -> dict[str, Any]:
    previous_blocks = state.get("previous_blocks")
    refinement = state.get("refinement")

    if previous_blocks and refinement:
        return await _handle_refinement(state, previous_blocks, refinement)

    # 기존 신규 검색 로직 (변경 없음)
    ...
```

각 노드의 `_handle_refinement` 구현:

| action | 동작 |
|---|---|
| replace | target_index의 항목 제거 → new_condition으로 1건 검색 → 해당 위치에 삽입 |
| remove | target_index 항목 제거 → 나머지 재구성 |
| add | new_condition으로 1건 검색 → 기존 목록에 추가 |
| change_condition | new_condition으로 전체 재검색 (기존 검색 로직 재활용) |
| regenerate | previous_blocks 무시, 기존 검색 로직 그대로 재실행 |

### 3.7 conversation_history 보강

**SSE 핸들러 변경** (`sse.py`):

```python
# 변경 전 (line 345)
"conversation_history": [],

# 변경 후
"conversation_history": conversation_history,  # _load_recent_history 결과 전달
```

**_load_recent_history 보강** — 구조화된 블록 요약 포함:

```python
# 기존: text 블록의 content만 추출
# 변경: assistant 응답의 구조화된 블록도 요약 포함
# 예: "[코스: 청담 명품 로드 (5곳) - 갤러리아명품관, 지방시 갤러리아명품관, ...]"
# 예: "[장소 5건: 스타벅스 강남점, 블루보틀 삼청, ...]"
```

**_stream_gemini 보강** — 대화 이력 전달:

```python
# 변경 전
async def _stream_gemini(system_prompt: str, user_prompt: str) -> AsyncIterator[str]:

# 변경 후
async def _stream_gemini(
    system_prompt: str,
    user_prompt: str,
    conversation_history: Optional[list[dict[str, str]]] = None,
) -> AsyncIterator[str]:
    # messages에 conversation_history 포함
```

### 3.8 그래프 변경 (real_builder.py)

```python
# 신규 노드 등록
from src.graph.refine_node import refine_node
graph.add_node("refine", refine_node)

# 라우팅 매핑에 추가
"REFINE": "refine",

# refine → response_builder 엣지
graph.add_edge("refine", "response_builder")
```

**주의**: refine_node가 내부에서 원본 노드를 직접 호출하므로, refine → 원본 노드 엣지는 필요 없음. refine_node의 반환값이 곧 response_blocks.

## 4. 수정 대상 파일 목록

| 파일 | 변경 내용 |
|---|---|
| `src/graph/state.py` | previous_blocks, refinement, original_intent 필드 추가 |
| `src/graph/intent_router_node.py` | REFINE intent 추가 (IntentType, 프롬프트, 라우팅) |
| `src/graph/refine_node.py` | **신규** — 수정 파싱 + 원본 노드 재호출 |
| `src/graph/real_builder.py` | refine 노드 등록 + 라우팅 |
| `src/api/sse.py` | conversation_history 전달, _stream_gemini 이력 주입, _load_recent_history 보강 |
| `src/graph/place_search_node.py` | _handle_refinement 분기 추가 |
| `src/graph/place_recommend_node.py` | _handle_refinement 분기 추가 |
| `src/graph/event_search_node.py` | _handle_refinement 분기 추가 |
| `src/graph/event_recommend_node.py` | _handle_refinement 분기 추가 |
| `src/graph/course_plan_node.py` | _handle_refinement 분기 추가 |
| `src/graph/review_compare_node.py` | _handle_refinement 분기 추가 |
| `src/graph/cost_estimate_node.py` | _handle_refinement 분기 추가 |
| `src/graph/query_preprocessor_node.py` | REFINE intent 전처리 로직 |
| `src/models/blocks.py` | 변경 없음 (기존 블록 구조 유지) |

## 5. 수정 유형별 동작 예시

### 5.1 부분 교체 (replace)

```
사용자: "강남 카페 추천해줘" → places [A, B, C, D, E]
사용자: "3번 빼고 조용한 곳으로 바꿔줘"
  → REFINE → action=replace, target_index=3, new_condition="조용한 카페"
  → place_recommend_node(refinement mode):
    - C 제거, "조용한 카페 강남" 1건 검색 → F
    - 결과: [A, B, F, D, E]
```

### 5.2 부분 삭제 (remove)

```
사용자: "코스 추천해줘" → course [1,2,3,4,5]
사용자: "2번 장소 빼줘"
  → REFINE → action=remove, target_index=2
  → course_plan_node(refinement mode):
    - stop 2 제거, 시간/경로 재계산
    - 결과: [1,3,4,5] (order 재정렬)
```

### 5.3 부분 추가 (add)

```
사용자: "행사 검색해줘" → events [A, B, C]
사용자: "무료 행사 하나 더 추가해줘"
  → REFINE → action=add, new_condition="무료 행사"
  → event_search_node(refinement mode):
    - "무료 행사" 1건 추가 검색 → D
    - 결과: [A, B, C, D]
```

### 5.4 조건 변경 (change_condition)

```
사용자: "강남 맛집 추천해줘" → places [강남 A,B,C,D,E]
사용자: "강남 말고 홍대 쪽으로"
  → REFINE → action=change_condition, new_condition="홍대 맛집"
  → place_recommend_node(refinement mode):
    - 기존 결과 무시, "홍대 맛집"으로 전체 재검색
    - 결과: [홍대 F,G,H,I,J]
```

### 5.5 전체 재생성 (regenerate)

```
사용자: "코스 추천해줘" → course [1,2,3,4,5]
사용자: "마음에 안 들어 다시 해줘"
  → REFINE → action=regenerate
  → course_plan_node(refinement mode):
    - previous_blocks 무시, 동일 조건 재실행
```

## 6. 테스트 계획

| 시나리오 | 검증 항목 |
|---|---|
| 장소 추천 → "3번 바꿔줘" | REFINE 분류, replace 동작, 나머지 유지 |
| 코스 → "2번 빼줘" | remove 후 order 재정렬, 시간 재계산 |
| 행사 → "하나 더 추가" | add 후 기존 목록 보존 |
| 장소 → "홍대로 바꿔줘" | change_condition 전체 재검색 |
| 코스 → "다시 해줘" | regenerate 동일 조건 재실행 |
| 일반 대화 → "바꿔줘" (맥락 없음) | REFINE 미분류 or 안내 메시지 |
| 장소 추천 → 코스 추천 → "아까 카페 목록에서 2번 빼줘" | 원거리 참조 Gemini 추론 |
| conversation_history 주입 확인 | _stream_gemini에 이력 전달 여부 |

## 7. 리스크 및 완화

| 리스크 | 완화 |
|---|---|
| REFINE vs 기존 intent 오분류 | 프롬프트에 명확한 예시, confidence threshold |
| 수정 지시 파싱 실패 | fallback: regenerate로 전체 재실행 |
| 원본 노드 재호출 시 검색 결과 변동 | 부분 교체 시 변경 대상만 재검색, 나머지 유지 |
| 토큰 비용 증가 (이력 5턴) | 구조화 블록은 요약 형태로 축약 전달 |
| 이전 응답 없이 "바꿔줘" 요청 | refine_node에서 감지 → "수정할 이전 응답이 없습니다" 안내 |
