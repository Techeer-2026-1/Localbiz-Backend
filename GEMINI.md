# GEMINI.md

## Project Overview
**AnyWay — LocalBiz Intelligence** is an AI chatbot specializing in Seoul's local life (places, events, courses, and analysis). It utilizes a hybrid search architecture (PostgreSQL/PostGIS + OpenSearch 768d k-NN) orchestrated by **LangGraph** and **Gemini 2.5 Flash**.

- **Main Technologies**: Python 3.11, FastAPI, LangGraph, PostgreSQL 16 (PostGIS), OpenSearch 2.17, Gemini 2.5 Flash, Next.js, Three.js.
- **Architecture**: Intent-driven routing (12+1 types) → Common Query Preprocessing → Hybrid Search → SSE (Server-Sent Events) streaming with 16 standardized response blocks.

## Building and Running

### Prerequisites
- **Python 3.11**
- **1Password**: Shared credentials (`AnyWay-Dev-Shared`) for API keys and DB secrets.
- **PostgreSQL MCP**: Export `DB_PASSWORD` to your shell RC for Claude/Gemini tools to access DB schema.

### Backend Setup
```bash
cd backend
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env  # Fill with 1Password values
```

### Running the Project
- **Backend Server**: `cd backend && uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload`
- **Validation (Full Check)**: `./validate.sh` (Runs ruff, pyright, pytest, and specification integrity checks).
- **Linter/Formatter**: `ruff check .` / `ruff format .`
- **Type Checker**: `pyright src scripts`
- **Tests**: `pytest`

### ETL & Database
- **Migrations**: `python backend/scripts/run_migration.py`
- **ETL (Run from root)**: `PYTHONPATH=. python backend/scripts/etl/crawl_reviews.py --naver-only --category 음식점 --limit 20`

## Development Conventions

### 1. Plan-Driven Workflow (Mandatory)
Before writing code, you **MUST** create a plan in `.sisyphus/plans/{YYYY-MM-DD}-{slug}/plan.md`.
- Implement only after the plan is marked as `최종 결정: APPROVED`.

### 2. 19 Data Model Invariants (Non-Negotiable)
The project enforces 19 strict rules to maintain data integrity. Key invariants include:
- **PK Duality**: places/events use UUID (VARCHAR(36)); others use BIGINT AI.
- **Append-Only Tables**: `messages`, `population_stats`, `feedback`, `langgraph_checkpoints` must **NEVER** be updated or deleted.
- **Embedding Standard**: Use **768d Gemini** (`text-embedding-004`) only. OpenAI embeddings are strictly prohibited.
- **6 Fixed Metrics**: `score_satisfaction`, `accessibility`, `cleanliness`, `value`, `atmosphere`, `expertise`.
- **SSE Event Types**: 16 fixed types (intent, text, text_stream, place, places, events, course, etc.).
- **Async Only**: Use `async def` and `await`. No sync wrappers or blocking calls.

### 3. Source of Truth
- **Specifications**: `기획/` directory contains CSVs for API/Function specs and MD for ERD.
- **Rule Priorities**: `기획/` (Specs) > `CLAUDE.md` (Invariants) > `GEMINI.md`.

## Key Directory Structure
- `backend/src/graph/`: LangGraph nodes and state definitions.
- `backend/src/api/sse.py`: Core SSE streaming logic.
- `backend/src/models/blocks.py`: Pydantic models for the 16 response block types.
- `backend/scripts/etl/`: Data ingestion and vectorization pipelines.
- `.sisyphus/`: Permanent records of project plans and decisions.
