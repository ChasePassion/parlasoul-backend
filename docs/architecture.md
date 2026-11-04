# Architecture

This document describes how the backend is organized and which subsystem owns
which state. It is the English counterpart to the deeper Chinese design notes
under `docs/`.

## 1. Scope and boundaries

The backend exposes exactly three HTTP surfaces:

- `/v1/*` — business API
- `/media/*` — public media delivery
- `/health` — liveness and readiness probe

It does **not**:

- render browser pages
- own `/api/auth/*` login routes or session management
- host the billing portal UI

Authentication is issued by the companion Next.js frontend (Better Auth). This
service only verifies the resulting JWT through JWKS.

## 2. Layering

```text
router -> dependency provider -> service -> repository / gateway -> response schema
```

| Layer | Responsibility | Rule |
| --- | --- | --- |
| `src/api` | HTTP parsing, dependency wiring, response envelope | Routers stay thin; no business logic |
| `src/schemas` | Request/response contracts | Pydantic models shared by router and service |
| `src/services` | Business orchestration, cross-module coordination | The only place that composes repositories and background tasks |
| `src/repositories` | Database access and upstream gateway adapters | Services never issue SQL directly |
| `src/core` | Settings, auth, logging context, unit of work, error codes | Cross-cutting infrastructure |
| `src/db` | SQLModel models, async session, startup schema guard | One source of truth for table shape |

Dependencies are explicit; routers never construct sessions or SDK clients.

## 3. Subsystem boundaries

Each subsystem owns its state and is composed through services. Shared state is
limited to PostgreSQL, Redis, and the R2 bucket.

### 3.1 Users and settings

Owns `users`, `user_settings`, and profile completion. The `users` table is
shared with Better Auth; the backend treats it as the canonical user identity
store and binds the current user to every database transaction.

### 3.2 Characters and voices

Owns `characters`, `voice_profiles`, and character-level LLM routing metadata.
Character avatars are stored in R2; PostgreSQL stores only the object key.
System voice catalogs are compiled into the service instead of being fetched at
runtime, so startup does not depend on provider availability.

### 3.3 Chat tree and LLM gateway

Owns `chats`, `turns`, and `candidates`. A chat is a tree, not a linear message
list: each assistant turn can hold multiple candidates, and user edits create a
new branch.

The LLM gateway wraps DeepSeek, DashScope, OpenRouter, OpenAI, and Anthropic
behind one interface. Character-level presets decide which route a conversation
uses; the gateway handles retries, timeouts, and provider-specific request
shapes.

### 3.4 Memory

Owns semantic and episodic memory. Memory text and vectors live in Milvus, not
PostgreSQL. The memory service retrieves relevant memories before generation and
runs periodic consolidation. Retrieval is best-effort: a timeout degrades to
empty memory and never blocks a chat turn.

### 3.5 Real-time voice

Owns WebRTC sessions and the audio pipeline: VAD, streaming STT, LLM
orchestration, streaming TTS, playback pacing, barge-in, and per-call telemetry.
The `turns` / `candidates` tables remain the source of truth for conversation
content; telemetry tables store only fixed enums, counters, and numeric
measurements — never raw audio, transcripts, prompts, or SDP.

### 3.6 Learning and saved items

Owns generated word cards, reply cards, feedback cards, and user-saved
snapshots. Card payloads are attached to `candidate.extra`; saved items are
materialized into `saved_items` so later edits to a candidate do not rewrite a
user's collection.

### 3.7 Growth

Owns check-ins, streaks, KPI trends, leaderboards, and share-card consumption.
Rules are pure functions in `growth_rules.py`; persistence lives in the growth
repository.

### 3.8 Billing

Owns subscription and one-time-payment state. Dodo Payments and WeChat webhooks
are persisted idempotently before entitlement recalculation, so replaying a
webhook cannot double-grant access.

### 3.9 Uploads and media

Owns presigned upload sessions, AVIF variant generation, and public media
delivery. Upload sessions and hot-object caches live in Redis; objects live in
R2; PostgreSQL stores only object keys.

## 4. Request lifecycle

1. The request context middleware assigns a request ID and logging context.
2. `get_current_user()` verifies the Better Auth JWT against JWKS and loads the
   shared `users` row.
3. The dependency provider opens an async session and binds the current user ID
   into the transaction.
4. The service performs business checks and orchestrates repositories.
5. `UnitOfWork` owns commit and rollback boundaries.
6. The response envelope wraps success and error payloads consistently.

## 5. Streaming path

Chat, regenerate, and edit are streaming operations:

1. A placeholder turn and candidate are persisted so the UI can render
   immediately.
2. The model response is streamed over SSE.
3. After the stream finishes, post-processing tasks run: title generation,
   reply cards, learning cards, growth rewards, memory writes, and TTS.
4. Post-processing failures are logged and retried independently; they do not
   corrupt the committed turn.

## 6. Data ownership

| Store | Owns |
| --- | --- |
| PostgreSQL | Users, settings, characters, chats, turns, candidates, saved items, billing state, telemetry |
| Milvus | Memory text, embeddings, group centroids |
| Redis | Upload sessions, upload locks, media hot cache, short-lived negative cache |
| Cloudflare R2 | Avatars, cover images, voice previews, generated media |

## 7. Row-level security

PostgreSQL row-level security is enabled on user-owned tables. The application
connects as a non-`BYPASSRLS` role; every transaction sets
`app.current_user_id`, which the policies use to filter rows. Migrations and
administrative restores run as a separate `BYPASSRLS` role. This split is
deliberate: if the application accidentally connects as the migrator role, RLS
silently stops filtering, so startup checks and deployment configuration must
keep the roles distinct.

## 8. Startup and shutdown

Startup:

1. Logging is configured.
2. The app factory registers middleware, exception handlers, and routers.
3. Lifespan checks database connectivity and schema compatibility.
4. Optional observability (Langfuse) and the memory system warm up.
5. Schedulers for semantic consolidation and proactive messaging start when
   enabled.
6. The real-time session manager starts.

Shutdown drains background tasks, closes database connections, and flushes the
log queue.

## 9. Design constraints

- Chat content has exactly one source of truth: the chat tree tables.
- Memory content has exactly one source of truth: Milvus.
- Media bytes never enter PostgreSQL; only object keys do.
- Telemetry never stores conversation content or network identifiers.
- New capabilities belong to the subsystem that owns their state; cross-domain
  work is composed in the service layer, not by sharing tables.
- A failing optional integration (memory, TTS, storage) must degrade a feature,
  not crash the process.
