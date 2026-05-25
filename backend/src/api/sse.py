"""SSE 핸들러 — 기획서 section 4.5 권위.

토큰 단위 스트리밍 (text_stream), 16종 콘텐츠 블록 순차 전송,
intent별 블록 순서 고정. SSE(Server-Sent Events) 기반.

결정 근거: 기획/SSE_vs_WebSocket_결정.md

흐름:
  1. seed user 보장 (개발용)
  2. conversations auto-create
  3. user 메시지 INSERT
  4. LangGraph astream() 실행
  5. 각 노드 출력 블록을 SSE 이벤트로 전송
  6. text_stream 블록 → Gemini astream()으로 토큰 스트리밍
  7. assistant 메시지 INSERT (블록 목록)
  8. done 이벤트 전송
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from src.config import get_settings  # pyright: ignore[reportMissingImports]
from src.models.blocks import (  # pyright: ignore[reportMissingImports]
    DoneBlock,
    StatusFrame,
    serialize_block,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# SSE 이벤트 헬퍼
# ---------------------------------------------------------------------------
def format_sse_event(event_type: str, data: Any) -> str:
    """SSE 이벤트 문자열 생성.

    Args:
        event_type: 이벤트 타입 (intent, text_stream, places, done 등).
        data: JSON-serializable dict.

    Returns:
        SSE 포맷 문자열: ``event: {type}\\ndata: {json}\\n\\n``
    """
    json_str = json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}\ndata: {json_str}\n\n"


def format_block_event(block: Any) -> str:
    """Pydantic 블록 → SSE 이벤트 문자열."""
    if hasattr(block, "model_dump"):
        data = serialize_block(block)
    else:
        data = block
    event_type = data.get("type", "unknown")
    return format_sse_event(event_type, data)


def format_status_event(message: str, node: Optional[str] = None) -> str:
    """SSE 제어 이벤트 (status) 생성. messages에 저장하지 않음."""
    frame = StatusFrame(message=message, node=node)
    return format_block_event(frame)


def format_done_event(
    status: str = "done",
    error_message: Optional[str] = None,
    message_id: Optional[int] = None,
) -> str:
    """done 이벤트 생성."""
    done = DoneBlock(status=status, error_message=error_message, message_id=message_id)
    return format_block_event(done)


def format_error_event(
    code: str,
    message: str,
    recoverable: bool = True,
) -> str:
    """error 이벤트 생성 (DB 미저장, SSE 전송만)."""
    from src.models.blocks import ErrorBlock  # pyright: ignore[reportMissingImports]

    error = ErrorBlock(code=code, message=message, recoverable=recoverable)
    return format_block_event(error)


# ---------------------------------------------------------------------------
# 글로벌 페르소나 — 모든 text_stream 응답에 일관 적용
# ---------------------------------------------------------------------------
_GLOBAL_PERSONA = (
    "너는 서울 로컬 라이프 AI 챗봇 AnyWay야.\n"
    "말투: 친절하고 자연스러운 존댓말(~요 체), 서론·인사 없이 바로 핵심부터, 간결하게.\n"
    "이모지는 꼭 필요한 경우에만 최소로 사용해."
)

# ---------------------------------------------------------------------------
# 노드별 status 메시지 (SSE 제어 이벤트, DB 미저장)
# ---------------------------------------------------------------------------
_NODE_STATUS_MESSAGES: dict[str, str] = {
    "intent_router": "의도를 분석하고 있어요...",
    "query_preprocessor": "질문을 정리하고 있어요...",
    "place_search": "장소를 검색하고 있어요...",
    "place_recommend": "장소를 추천하고 있어요...",
    "event_search": "행사를 검색하고 있어요...",
    "event_recommend": "행사를 추천하고 있어요...",
    "course_plan": "코스를 계획하고 있어요...",
    "general": "답변을 생성하고 있어요...",
    "detail_inquiry": "상세 정보를 조회하고 있어요...",
    "booking": "예약 정보를 확인하고 있어요...",
    "calendar": "일정을 추가하고 있어요...",
    "image_search": "이미지를 분석하고 있어요...",
    "review_compare": "장소를 비교하고 있어요...",
    "analysis": "장소를 분석하고 있어요...",
}


# ---------------------------------------------------------------------------
# DB 헬퍼 — conversations, messages
# ---------------------------------------------------------------------------
async def _ensure_conversation(pool: Any, thread_id: str, user_id: int) -> None:
    """conversations 테이블에 thread_id가 없으면 auto-create.

    ON CONFLICT DO NOTHING으로 동시 요청 race condition 방지.
    """
    await pool.execute(
        "INSERT INTO conversations (thread_id, user_id, title) VALUES ($1, $2, $3) ON CONFLICT (thread_id) DO NOTHING",
        thread_id,
        user_id,
        "새 대화",
    )


async def _load_recent_history(pool: Any, thread_id: str) -> list[dict[str, str]]:
    """최근 대화 이력 조회 (최근 10메시지 = 5턴). 구조화 블록 요약 포함."""
    try:
        rows = await pool.fetch(
            "SELECT role, blocks FROM messages WHERE thread_id = $1 ORDER BY message_id DESC LIMIT 10",
            thread_id,
        )
    except Exception:
        logger.warning("_load_recent_history: DB 조회 실패 thread_id=%s → 빈 이력 반환", thread_id)
        return []

    history: list[dict[str, str]] = []
    for row in reversed(rows):
        role: str = row["role"]
        blocks = row["blocks"]
        if isinstance(blocks, str):
            try:
                blocks = json.loads(blocks)
            except Exception:
                continue
        parts: list[str] = []
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text" and role == "user":
                parts.append(block.get("content", ""))
            elif btype == "text_stream" and role == "assistant":
                parts.append(block.get("content", ""))
            elif btype == "text" and role == "assistant":
                # #151: EVENT 빈 결과 정적 안내(text 블록)도 다음 턴 컨텍스트에 포함
                parts.append(block.get("content", ""))
            elif btype == "places" and role == "assistant":
                items = block.get("items", [])
                names = [it.get("name", "") for it in items if isinstance(it, dict) and it.get("name")]
                if names:
                    parts.append(f"[장소 {len(names)}건: {', '.join(names[:5])}]")
            elif btype == "events" and role == "assistant":
                items = block.get("items", [])
                titles = [it.get("title", "") for it in items if isinstance(it, dict) and it.get("title")]
                if titles:
                    parts.append(f"[행사 {len(titles)}건: {', '.join(titles[:5])}]")
            elif btype == "course" and role == "assistant":
                title = block.get("title", "")
                stops = block.get("stops", [])
                stop_names = []
                for stop in stops:
                    if isinstance(stop, dict):
                        place = stop.get("place", {})
                        name = place.get("name", "") if isinstance(place, dict) else ""
                        if name:
                            stop_names.append(name)
                label = title or "코스"
                if stop_names:
                    parts.append(f"[{label} ({len(stop_names)}곳): {', '.join(stop_names)}]")
            elif btype == "chart" and role == "assistant":
                chart_places = block.get("places", [])
                cnames = [cp.get("name", "") for cp in chart_places if isinstance(cp, dict) and cp.get("name")]
                if cnames:
                    parts.append(f"[비교: {' vs '.join(cnames[:3])}]")
        content = " ".join(p for p in parts if p).strip()
        if content:
            history.append({"role": role, "content": content})
    return history


async def _insert_message(
    pool: Any,
    thread_id: str,
    role: str,
    blocks: list[dict[str, Any]],
) -> int:
    """messages 테이블에 INSERT (append-only, 불변식 #3).

    message_id는 BIGSERIAL auto-increment (불변식 #1).
    삽입된 message_id 반환.
    """
    blocks_json = json.dumps(blocks, ensure_ascii=False)
    row = await pool.fetchrow(
        "INSERT INTO messages (thread_id, role, blocks) VALUES ($1, $2, $3::jsonb) RETURNING message_id",
        thread_id,
        role,
        blocks_json,
    )
    return int(row["message_id"])


# ---------------------------------------------------------------------------
# Gemini 토큰 스트리밍
# ---------------------------------------------------------------------------
async def _stream_gemini(
    system_prompt: str,
    user_prompt: str,
    conversation_history: Optional[list[dict[str, str]]] = None,
) -> AsyncIterator[str]:
    """Gemini 2.5 Flash로 토큰 단위 스트리밍. 대화 이력 포함.

    재시도(retry) 미적용 (#124): 스트림 도중 실패 시 이미 전송된 토큰을
    되돌릴 수 없어 재시도하면 토큰이 중복 출력된다. 호출부(event_generator)가
    예외를 잡아 GEMINI_API_ERROR로 graceful 처리한다.

    Yields:
        각 토큰 문자열 (delta).
    """
    from langchain_google_genai import ChatGoogleGenerativeAI  # pyright: ignore[reportMissingImports]

    settings = get_settings()
    llm = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        google_api_key=settings.gemini_llm_api_key,
        temperature=0.7,
        streaming=True,
    )

    messages: list[tuple[str, str]] = [
        ("system", system_prompt),
    ]

    if conversation_history:
        for msg in conversation_history[-5:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            lc_role = "human" if role == "user" else "ai"
            messages.append((lc_role, content))

    messages.append(("human", user_prompt))

    async for chunk in llm.astream(messages):
        content = chunk.content
        if content:
            yield str(content)


# ---------------------------------------------------------------------------
# SSE 엔드포인트
# ---------------------------------------------------------------------------
@router.get("/api/v1/chat/stream")
async def chat_stream(
    request: Request,
    thread_id: str,
    query: str,
    token: Optional[str] = None,
    retry: bool = False,
) -> StreamingResponse:
    """메인 채팅 SSE 엔드포인트.

    Args:
        request: FastAPI Request (disconnect 감지용).
        thread_id: 대화 스레드 ID.
        query: 사용자 쿼리 텍스트.
        token: JWT 토큰 (query parameter fallback).
        retry: True이면 응답 재생성 (user INSERT 생략, append-only 준수).
    """

    async def event_generator() -> AsyncIterator[str]:
        from src.db.postgres import get_pool  # pyright: ignore[reportMissingImports]
        from src.graph.real_builder import build_graph  # pyright: ignore[reportMissingImports]

        logger.info(
            "SSE stream started: thread_id=%s, query_len=%d",
            thread_id,
            len(query),
        )

        try:
            # 0. JWT 인증 — token query parameter에서 user_id 추출
            if not token:
                yield format_error_event("AUTH_MISSING", "인증 토큰이 필요합니다.", recoverable=False)
                yield format_done_event(status="error", error_message="인증 토큰이 필요합니다.")
                return

            try:
                from src.core.security import decode_access_token  # pyright: ignore[reportMissingImports]

                payload = decode_access_token(token)
                user_id = int(payload["sub"])
            except Exception:
                logger.info("SSE JWT decode failed: thread_id=%s", thread_id)
                _msg = "유효하지 않은 인증 토큰입니다. 다시 로그인해 주세요."
                yield format_error_event("AUTH_INVALID", _msg, recoverable=False)
                yield format_done_event(status="error", error_message=_msg)
                return

            # 1. DB 준비 — conversation auto-create
            try:
                pool = get_pool()
            except RuntimeError:
                logger.exception("DB pool 미획득: thread_id=%s", thread_id)
                yield format_error_event("DB_POOL_UNAVAILABLE", "서버 DB 연결에 실패했습니다.", recoverable=False)
                yield format_done_event(status="error", error_message="서버 DB 연결에 실패했습니다.")
                return

            await _ensure_conversation(pool, thread_id, user_id)

            # 2. user 메시지 INSERT (retry 시 생략)
            if retry:
                # 방어: 해당 thread의 마지막 'user' 메시지가 현재 쿼리와 같은지 확인
                # retry=true여도 쿼리가 바뀌었다면 새로운 턴으로 간주하고 INSERT 해야 함
                last_user_msg = await pool.fetchrow(
                    "SELECT blocks FROM messages WHERE thread_id = $1 AND role = 'user' ORDER BY message_id DESC LIMIT 1",
                    thread_id,
                )
                is_same_query = False
                if last_user_msg:
                    try:
                        blocks = last_user_msg["blocks"]
                        # blocks가 리스트이고 첫 번째 블록이 텍스트이며 내용이 같으면 중복으로 간주
                        if (
                            isinstance(blocks, list)
                            and len(blocks) > 0
                            and blocks[0].get("type") == "text"
                            and blocks[0].get("content") == query
                        ):
                            is_same_query = True
                    except Exception:
                        logger.warning("Failed to check last user message: thread_id=%s", thread_id)

                if not is_same_query:
                    # 유저 메시지가 없거나 내용이 다르면(새로운 질문이면) fallback → INSERT
                    user_blocks: list[dict[str, Any]] = [{"type": "text", "content": query}]
                    await _insert_message(pool, thread_id, "user", user_blocks)
            else:
                user_blocks = [{"type": "text", "content": query}]
                await _insert_message(pool, thread_id, "user", user_blocks)

            # 3. 복수 intent 분류 (Gemini 1회) + LangGraph 순차 실행
            from src.graph.intent_router_node import classify_intents  # pyright: ignore[reportMissingImports]

            graph = build_graph(checkpointer=None)
            conversation_history = await _load_recent_history(pool, thread_id)

            try:
                intents = await classify_intents(query, conversation_history=conversation_history)
            except Exception:
                logger.exception("classify_intents failed: thread_id=%s", thread_id)
                intents = []

            # fallback: 분류 실패 시 기존 단일 intent 동작 (intent 미주입 → intent_router가 분류)
            if not intents:
                intents_with_query: list[tuple[str, float, str, bool]] = [
                    ("", 0.0, query, True)  # intent="" → 미주입, 기존 로직
                ]
            else:
                intents_with_query = [
                    (intent.value, conf, sub_q, idx == len(intents) - 1)
                    for idx, (intent, conf, sub_q) in enumerate(intents)
                ]

            assistant_blocks: list[dict[str, Any]] = []
            cancelled = False
            gemini_error = False

            for intent_value, _conf, sub_query, is_last in intents_with_query:
                if cancelled or gemini_error:
                    break

                input_state: dict[str, Any] = {
                    "query": sub_query,
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "conversation_history": conversation_history,
                }
                # intent 주입 → intent_router_node가 classify 스킵
                if intent_value:
                    input_state["intent"] = intent_value

                async for event in graph.astream(input_state):
                    if await request.is_disconnected():
                        logger.info("Client disconnected: thread_id=%s", thread_id)
                        cancelled = True
                        break

                    for node_name, node_output in event.items():
                        if not isinstance(node_output, dict):
                            continue

                        status_msg = _NODE_STATUS_MESSAGES.get(node_name)
                        if status_msg:
                            yield format_status_event(status_msg, node=node_name)

                        blocks = node_output.get("response_blocks", [])
                        for block in blocks:
                            if not isinstance(block, dict):
                                continue

                            block_type = block.get("type", "")

                            # done 블록: 마지막 intent만 저장, 중간은 완전 무시
                            if block_type == "done":
                                if is_last:
                                    assistant_blocks.append(block)
                                continue

                            if block_type == "text_stream":
                                node_system = block.get("system", "")
                                system_prompt = (
                                    f"{_GLOBAL_PERSONA}\n\n{node_system}" if node_system else _GLOBAL_PERSONA
                                )
                                user_prompt = block.get("prompt", sub_query)
                                full_text = ""

                                try:
                                    async for delta in _stream_gemini(system_prompt, user_prompt, conversation_history):
                                        if await request.is_disconnected():
                                            cancelled = True
                                            break
                                        full_text += delta
                                        yield format_sse_event("text_stream", {"type": "text_stream", "delta": delta})
                                except Exception:
                                    logger.exception("Gemini streaming failed: thread_id=%s", thread_id)
                                    gemini_error = True

                                if full_text:
                                    assistant_blocks.append({"type": "text_stream", "content": full_text})

                                if cancelled or gemini_error:
                                    break
                            elif block_type == "vision_debug":
                                # 디버그 전용 — SSE 전송만, messages 미저장
                                yield format_sse_event(block_type, block)
                            else:
                                yield format_sse_event(block_type, block)
                                assistant_blocks.append(block)

                        if cancelled or gemini_error:
                            break

                # 중간 intent 완료 → done_partial emit (DB 미저장)
                if not is_last and not cancelled and not gemini_error:
                    yield format_sse_event(
                        "done_partial",
                        {
                            "type": "done_partial",
                            "completed_intent": intent_value,
                        },
                    )

            # 4. assistant 메시지 INSERT (done 전에 완료)
            persistence_success = True
            assistant_message_id: Optional[int] = None
            if assistant_blocks:
                try:
                    assistant_message_id = await _insert_message(pool, thread_id, "assistant", assistant_blocks)
                except Exception:
                    logger.exception("assistant message INSERT failed: thread_id=%s", thread_id)
                    persistence_success = False

            # 5. 종료 이벤트 (INSERT 후 전송)
            if cancelled:
                yield format_done_event(status="cancelled")
            elif gemini_error:
                _msg = "AI 응답 생성에 일시적으로 실패했습니다. 잠시 후 다시 시도해 주세요."
                yield format_error_event("GEMINI_API_ERROR", _msg, recoverable=True)
                yield format_done_event(status="error", error_message=_msg)
            elif not persistence_success:
                _msg = "응답 저장에 실패했습니다. 대화 내역에 남지 않을 수 있습니다."
                yield format_error_event("PERSISTENCE_ERROR", _msg, recoverable=True)
                yield format_done_event(status="error", error_message=_msg)
            else:
                yield format_done_event(status="done", message_id=assistant_message_id)

        except Exception:
            logger.exception("SSE error: thread_id=%s", thread_id)
            _msg = "서버 내부 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
            yield format_error_event("INTERNAL_ERROR", _msg, recoverable=True)
            yield format_done_event(status="error", error_message=_msg)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
