# ParlaSoul Backend

FastAPI backend for **ParlaSoul**, an AI character chat platform with branching
conversations, long-term vector memory, learning cards, and real-time voice calls.

The service owns the business API (`/v1/*`), public media delivery (`/media/*`),
and the health endpoint (`/health`). Browser pages and authentication UI live in
the companion frontend repository.

> Deep-dive documentation under `docs/` is currently written in Chinese. This
> README and `docs/architecture.md` provide an English overview.

## Features

- **Branching chat tree** — every assistant reply is a node with multiple
  candidates, so users can regenerate, edit, and fork a conversation without
  losing history.
- **Streaming generation** — SSE streams for new turns, regeneration, and edits,
  followed by post-processing tasks (reply cards, learning cards, growth
  rewards, memory writes, TTS).
- **Long-term memory** — semantic and episodic memories are stored in Milvus and
  retrieved before generation; PostgreSQL keeps only business state.
- **Real-time voice** — WebRTC sessions (aiortc) with VAD, streaming STT,
  streaming TTS, barge-in handling, and per-call telemetry.
- **Character and voice management** — character CRUD, LLM presets, dialogue
  styles, system voices, and cloned voice profiles.
- **Learning assistance** — word cards, reply cards, feedback cards, and saved
  items built from conversation content.
- **Growth system** — check-ins, streaks, KPI trends, leaderboards, and share
  cards.
- **Billing** — Dodo Payments subscriptions and WeChat one-time payment orders,
  reconciled into user entitlements through webhooks.
- **Media pipeline** — presigned Cloudflare R2 uploads, AVIF variant generation,
  and cached public media delivery.
- **Evaluation harness** — reproducible offline evaluation runs with rule-based
  and LLM judges.

## Architecture

```mermaid
flowchart TB
    Client[Web / RTC client] -->|JWT · HTTPS · WebRTC| Gateway[FastAPI app]

    subgraph Users["Users & settings"]
        UserAPI[Users / settings API] --> UserDB[(PostgreSQL)]
    end

    subgraph Characters["Characters & voices"]
        CharAPI[Characters / voices API] --> CharSvc[Character & voice service]
        CharSvc --> CharDB[(PostgreSQL)]
        CharSvc --> R2[(Cloudflare R2)]
        CharSvc --> TTS[TTS providers]
    end

    subgraph Chats["Chat tree & LLM"]
        ChatAPI[Chats / turns API] --> ChatSvc[Chat & turn service]
        ChatSvc --> ChatDB[(PostgreSQL)]
        ChatSvc --> LLMGateway[LLM gateway]
        LLMGateway --> LLM[DeepSeek · DashScope · OpenRouter · OpenAI · Anthropic]
    end

    subgraph Memory["Vector memory"]
        MemAPI[Memories API] --> MemSvc[Memory service]
        MemSvc --> Milvus[(Milvus)]
    end

    subgraph Realtime["Real-time voice"]
        RtAPI[Realtime API] --> RtSvc[Session manager & RTC pipeline]
        RtSvc --> Telemetry[(PostgreSQL telemetry)]
        RtSvc --> TURN[STUN / TURN]
    end

    subgraph Billing["Billing"]
        BillAPI[Payments / webhooks API] --> BillSvc[Billing service]
        BillSvc --> BillDB[(PostgreSQL)]
        BillSvc --> Dodo[Dodo Payments]
    end

    Gateway --> UserAPI & CharAPI & ChatAPI & MemAPI & RtAPI & BillAPI
    Realtime --> Chats
```

See [`docs/architecture.md`](docs/architecture.md) for the request lifecycle,
subsystem boundaries, and design constraints.

## Tech stack

| Area | Choice |
| --- | --- |
| Language | Python 3.11 – 3.13 |
| Web framework | FastAPI, Uvicorn |
| ORM / migrations | SQLModel, SQLAlchemy 2 async, Alembic |
| Primary database | PostgreSQL 17 |
| Vector database | Milvus 2.5 (self-hosted or Zilliz Cloud) |
| Cache / sessions | Redis 7 |
| Object storage | Cloudflare R2 (S3-compatible) |
| Real-time media | aiortc, onnxruntime (Silero VAD) |
| LLM access | OpenAI-compatible clients for DeepSeek, DashScope, OpenRouter, OpenAI, Anthropic |
| Voice | DashScope real-time STT/TTS, MiniMax TTS |

## Repository layout

```text
src/api/            HTTP layer: routers, dependencies, response envelope
src/schemas/        Request/response contracts
src/core/           Settings, auth (JWKS), logging, unit of work, LLM registry
src/db/             SQLModel models, async session, startup schema guard
src/repositories/   Database access and upstream gateway adapters
src/services/       Business orchestration
src/memory_system/  Embedding, Milvus storage, memory processing
src/realtime/       WebRTC sessions, VAD, audio pipeline, telemetry
src/evaluation/     Offline evaluation harness
src/jobs/           Scheduled background jobs
alembic/            Database migrations
config/             Runtime JSON configuration
docs/               Architecture and operational documentation
tests/              Unit and integration tests
```

## Getting started

### Prerequisites

- Python 3.11 – 3.13
- PostgreSQL 17
- Milvus 2.5 (local, or a Zilliz Cloud endpoint)
- Redis 7 (media cache and upload sessions)
- An embedding provider and at least one LLM provider

### 1. Install

```bash
git clone https://github.com/ChasePassion/parlasoul-backend.git
cd parlasoul-backend

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### 2. Configure

```bash
cp .env.example .env
```

Fill in at least these values:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://user:password@host:5432/database` |
| `SECRET_KEY` | Application secret; generate with `openssl rand -hex 32` |
| `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` | Outbound email |
| `MILVUS_URL` | Milvus / Zilliz endpoint |
| `EMBEDDING_API_KEY`, `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL` | Embedding provider |
| `LLM_*_API_KEY` | At least one chat provider (DeepSeek, DashScope, OpenRouter, OpenAI, Anthropic) |
| `DASHSCOPE_API_KEY`, `MINIMAX_TTS_API_KEY` | Optional: voice and speech features |
| `R2_*`, `REDIS_URL` | Optional: media upload and caching |

Authentication is delegated to the companion frontend: this service verifies
Better Auth JWTs against `BETTER_AUTH_JWKS_URL`. For backend-only development,
`tools/voice-lab/jwks_server.py` can expose a local JWKS endpoint and mint test
tokens.

### 3. Migrate and run

```bash
python -m alembic upgrade head
python -m uvicorn src.main:app --reload --port 8000
curl http://127.0.0.1:8000/health
```

Optional integrations (voice, media, billing) can be left unconfigured; the
endpoints that need them fail with structured errors. Required settings —
database, Milvus, and the embedding provider — are validated at startup.

### Docker

```bash
docker build -t parlasoul-backend .
docker run --env-file .env -p 8000:8000 parlasoul-backend
```

`compose.yaml` and `compose.production.yaml` describe the maintainers'
production topology (PostgreSQL, Redis, backend, frontend, nginx, coturn). They
are a reference for self-hosting, not a turnkey deployment: review the bind
mounts, certificates, and environment files before using them.

## Testing and linting

```bash
python -m pytest -q
python -m ruff check --select F src tests tools alembic
python -m ruff check src/bootstrap src/jobs src/core/config.py src/core/uow.py \
  src/core/error_codes.py src/main.py src/api/main.py src/schemas/common.py \
  src/schemas/sse.py tools alembic
python tools/arch_guard.py
python -m compileall src tests
```

CI runs the same commands against a PostgreSQL service; see
`.github/workflows/ci.yml`.

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — request lifecycle, subsystem
  boundaries, and design constraints
- [`docs/database/database-semantics.md`](docs/database/database-semantics.md) —
  table semantics and row-level security model
- [`docs/realtime-call-architecture.md`](docs/realtime-call-architecture.md) —
  WebRTC session, audio pipeline, and turn-taking design
- [`docs/evaluation-dataset-construction-guide.md`](docs/evaluation-dataset-construction-guide.md) —
  evaluation dataset format

Exact database DDL is defined by the Alembic migrations under
`alembic/versions/`. SQL fixtures read by the early revisions live in
`alembic/sql/`; one-off migration reports are not kept in this repository.

## Related repositories

- Frontend: <https://github.com/ChasePassion/parlasoul-frontend>

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Security issues should be reported
privately as described in [`SECURITY.md`](SECURITY.md).

## License

Source code is licensed under the **GNU Affero General Public License v3.0**
(AGPL-3.0). See [`LICENSE`](LICENSE).

The ParlaSoul name, logo, and brand assets are not covered by the source-code
license and remain the property of the project owner. Third-party model and
voice providers are governed by their own terms; you are responsible for the
API usage and generated content you produce with them.
