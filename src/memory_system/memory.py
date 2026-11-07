"""Memory class - Main API for AI Memory System.

Provides a mem0-style interface for memory operations including
add, search, update, delete, reset, and consolidate.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from src.core.llm_registry import LLMRegistry, ResolvedLLMRoute
from src.observability import (
    build_chat_session_id,
    build_memory_batch_session_id,
    get_client,
    observability_scope,
    observe,
)

from .concurrency import run_blocking
from .config import MemoryConfig
from .processors import (
    EpisodicMemoryManager,
    MemoryUsageJudge,
    NarrativeMemoryManager,
    SemanticWriter,
)
from .utils import build_filter, normalize

if TYPE_CHECKING:
    from src.services.llm_gateway_service import LLMGatewayService

    from .clients.embedding import EmbeddingClient
    from .clients.milvus_store import MilvusStore


logger = logging.getLogger(__name__)


@dataclass
class MemoryRecord:
    """A memory record returned from search operations.

    Schema fields:
    - id: Record ID
    - user_id: User identifier
    - character_id: Character identifier
    - memory_type: "episodic" or "semantic"
    - ts: Unix timestamp of write time
    - chat_id: Conversation/thread identifier
    - text: Main natural-language content
    - group_id: Narrative group ID (-1 = ungrouped)

    Optional provenance fields (phase18 evaluation hooks; always populated by
    ``search_with_trace``, may stay None on compatibility paths):
    - distance: similarity distance reported by the vector store (search hits only)
    - is_seed: True for top-k episodic seed hits, False for group-expanded members
    - source_turn_id: reserved for source-turn provenance when persisted
    """
    id: int
    user_id: str
    character_id: str
    memory_type: str
    ts: int
    chat_id: str
    text: str
    group_id: int = -1
    distance: float | None = None
    is_seed: bool | None = None
    source_turn_id: str | None = None


MEMORY_RETRIEVAL_TRACE_VERSION = "1"


@dataclass
class MemoryRetrievalTrace:
    """Structured provenance of one memory retrieval (phase18 hook).

    Never inferred from reply text: IDs/distances come from the store, seed
    hits are recorded before narrative-group expansion, expanded members are
    marked ``is_seed=False``. ``trace_status='unavailable'`` signals missing
    inputs — never fill zeros.
    """

    trace_version: str
    query_fingerprint: str
    retrieved_ids: list[int]
    seed_ids: list[int]
    expanded_ids: list[int]
    semantic_ids: list[int]
    semantic_mode: str  # "all" | "top_k"
    distances: dict[int, float]
    is_seed: dict[int, bool]
    retrieval_started_at: float
    retrieval_finished_at: float
    duration_ms: int
    trace_status: str = "ok"


@dataclass
class ConsolidationStats:
    """Statistics from a consolidation run.

    The first two fields are the legacy totals kept for existing callers.
    Phase18 adds comparable before/after counts: physical row counts (episodic
    is never deleted, so physical rows only grow) and logical fact counts
    (distinct facts before dedup/supersession vs after extraction), plus
    action counts, duration and a batch fingerprint.
    """

    memories_processed: int = 0
    semantic_created: int = 0
    physical_before_episodic: int = 0
    physical_before_semantic: int = 0
    physical_after_episodic: int = 0
    physical_after_semantic: int = 0
    logical_before_count: int = 0
    logical_after_count: int = 0
    action_counts: dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0
    batch_fingerprint: str = ""


class Memory:
    """AI Memory System main class.
    
    Provides a mem0-style API interface for memory operations.
    Uses factory pattern to instantiate infrastructure clients and processors.
    """
    
    def __init__(
        self,
        config: MemoryConfig,
        *,
        gateway_service: LLMGatewayService,
        llm_registry: LLMRegistry,
    ):
        """Initialize the memory system.
        
        Args:
            config: Memory system configuration.
            
        Raises:
            MilvusConnectionError: If connection to Milvus fails
        """
        self._config = config
        
        # Initialize infrastructure clients using factory pattern
        self._embedding_client = self._create_embedding_client()
        self._gateway_service = gateway_service
        self._llm_registry = llm_registry
        self._llm_route = self._resolve_llm_route()
        self._store = self._create_milvus_store()
        
        # Initialize processor modules
        self._memory_manager = EpisodicMemoryManager(self._gateway_service, self._llm_route)
        self._semantic_writer = SemanticWriter(self._gateway_service, self._llm_route)
        self._memory_usage_judge = MemoryUsageJudge(self._gateway_service, self._llm_route)
        self._narrative_manager = NarrativeMemoryManager(self._store, self._config)
        
        # Create collection if not exists
        self._store.create_collection(dim=self._config.embedding_dim)
        
        logger.info(
            f"Memory system initialized with collection '{self._config.collection_name}'"
        )

    def _resolve_llm_route(self) -> ResolvedLLMRoute:
        route = self._config.llm_route or self._llm_registry.resolve_memory_route()
        if route is None:
            raise RuntimeError("memory llm route is not configured")
        return route
    
    def _create_embedding_client(self) -> EmbeddingClient:
        """Factory method to create EmbeddingClient."""
        from .clients.embedding import EmbeddingClient

        return EmbeddingClient(
            api_key=self._config.embedding_api_key,
            base_url=self._config.embedding_base_url,
            model=self._config.embedding_model,
            dimensions=self._config.embedding_dim,
        )

    def _create_milvus_store(self) -> MilvusStore:
        """Factory method to create MilvusStore."""
        from .clients.milvus_store import MilvusStore

        return MilvusStore(
            uri=self._config.milvus_uri,
            token=self._config.milvus_token,
            collection_name=self._config.collection_name,
            groups_collection_name=self._config.group_collection_name,
        )

    def _encode_embeddings(
        self,
        texts: list[str],
        *,
        user_id: str,
        character_id: str,
        chat_id: str | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []

        config = getattr(self, "_config", None)
        embedding_model = getattr(config, "embedding_model", None)
        client = get_client()
        start_observation = getattr(client, "start_observation", None)
        observation = None
        if callable(start_observation):
            observation = start_observation(
                name="memory.embed",
                as_type="embedding",
                input={"count": len(texts)},
                model=embedding_model,
                metadata={
                    "user_id": user_id,
                    "character_id": character_id,
                    "chat_id": chat_id,
                    "input_lengths": [len(text) for text in texts[:3]],
                },
            )

        try:
            encode_with_usage = getattr(self._embedding_client, "encode_with_usage", None)
            if callable(encode_with_usage):
                embedding_result = encode_with_usage(texts)
                vectors = embedding_result.vectors
                usage_details = embedding_result.usage_details
            else:
                vectors = self._embedding_client.encode(texts)
                usage_details = None
        except Exception as exc:
            if observation is not None and hasattr(observation, "update"):
                observation.update(
                    level="ERROR",
                    status_message=f"Memory embedding failed: {type(exc).__name__}",
                )
            if observation is not None and hasattr(observation, "end"):
                observation.end()
            raise

        if observation is not None and hasattr(observation, "update"):
            payload: dict[str, Any] = {"output": {"count": len(vectors)}}
            if usage_details:
                payload["usage_details"] = usage_details
            observation.update(**payload)
        if observation is not None and hasattr(observation, "end"):
            observation.end()
        return vectors

    @observe(name="memory.manage_async")
    async def manage_async(
        self,
        user_text: str,
        assistant_text: str,
        user_id: str,
        character_id: str,
        chat_id: str,
        metadata: dict[str, Any] | None = None
    ) -> list[int]:

        """Manage memories with CRUD operations based on conversation (async).
        
        Core implementation that offloads blocking operations to thread pool.
        Processes conversation through EpisodicMemoryManager to determine
        add, update, or delete operations on episodic memories.
        
        This is the preferred method in async environments (Gradio, FastAPI, etc.).
        
        Args:
            user_text: User input text
            assistant_text: Assistant response text
            user_id: User identifier
            chat_id: Conversation/thread identifier
            metadata: Optional additional metadata (ignored in v2 schema)
            
        Returns:
            List of newly added memory IDs
        """
        with observability_scope(
            user_id=user_id,
            session_id=build_chat_session_id(chat_id),
            tags=["memory", "manage"],
            metadata={
                "operation": "memory.manage",
                "chat_id": chat_id,
                "character_id": character_id,
            },
        ):
            episodic_filter = build_filter(user_id, character_id, 'memory_type == "episodic"')
            episodic_memories = await run_blocking(
                self._store.query,
                filter_expr=episodic_filter,
                limit=10000,
                output_fields=["id", "text"],
            )

            result = await self._memory_manager.manage_memories(
                user_text=user_text,
                assistant_text=assistant_text,
                episodic_memories=episodic_memories,
                user_id=user_id,
                chat_id=chat_id,
                character_id=character_id,
            )

            added_ids: list[int] = []
            for op in result.operations:
                if op.operation_type == "delete" and op.memory_id is not None:
                    await run_blocking(self.delete, op.memory_id, user_id, character_id)

            for op in result.operations:
                if op.operation_type == "update" and op.memory_id is not None:
                    await run_blocking(
                        self.update,
                        op.memory_id,
                        {"text": op.text},
                        user_id,
                        character_id,
                    )

            add_operations = [op for op in result.operations if op.operation_type == "add"]
            if add_operations:
                add_texts = [op.text for op in add_operations if op.text is not None]
                embeddings = await run_blocking(
                    self._encode_embeddings,
                    add_texts,
                    user_id=user_id,
                    character_id=character_id,
                    chat_id=chat_id,
                )
                current_ts = int(time.time())
                entities = []

                for i, text in enumerate(add_texts):
                    entities.append(
                        {
                            "user_id": user_id,
                            "character_id": character_id,
                            "memory_type": "episodic",
                            "ts": current_ts,
                            "chat_id": chat_id,
                            "text": text,
                            "vector": embeddings[i],
                            "group_id": -1,
                        }
                    )

                added_ids = await run_blocking(self._store.insert, entities)

            logger.info(
                "Memory operation 'manage_async': type=episodic, user_id=%s, chat_id=%s, added=%s, updated=%s, deleted=%s",
                user_id,
                chat_id,
                len(added_ids),
                len([op for op in result.operations if op.operation_type == "update"]),
                len([op for op in result.operations if op.operation_type == "delete"]),
            )
            return added_ids

    @observe(name="memory.search")
    def search(
        self,
        query: str,
        user_id: str,
        character_id: str,
        chat_id: str,
    ) -> dict[str, list[MemoryRecord]]:
        """Compatibility wrapper: retrieve memories and discard the trace."""
        results, _ = self.search_with_trace(query, user_id, character_id, chat_id)
        return results

    @observe(name="memory.search_with_trace")
    def search_with_trace(
        self,
        query: str,
        user_id: str,
        character_id: str,
        chat_id: str,
    ) -> tuple[dict[str, list[MemoryRecord]], MemoryRetrievalTrace]:
        """Search memories for a user with narrative group expansion.

        Retrieves both episodic and semantic memories. Episodic memories are
        expanded to include all members of their narrative groups. Returns the
        results plus a structured retrieval trace (phase18 hook): seed hits are
        recorded before group expansion, expanded members are marked
        ``is_seed=False``, distances come from the vector store.
        """
        started_at = time.monotonic()
        query_fingerprint = hashlib.sha256((query or "").encode("utf-8")).hexdigest()

        def _finish_trace(
            *,
            retrieved_ids: list[int],
            seed_ids: list[int],
            expanded_ids: list[int],
            semantic_ids: list[int],
            semantic_mode: str,
            distances: dict[int, float],
            is_seed: dict[int, bool],
            trace_status: str = "ok",
        ) -> MemoryRetrievalTrace:
            finished_at = time.monotonic()
            return MemoryRetrievalTrace(
                trace_version=MEMORY_RETRIEVAL_TRACE_VERSION,
                query_fingerprint=query_fingerprint,
                retrieved_ids=retrieved_ids,
                seed_ids=seed_ids,
                expanded_ids=expanded_ids,
                semantic_ids=semantic_ids,
                semantic_mode=semantic_mode,
                distances=distances,
                is_seed=is_seed,
                retrieval_started_at=started_at,
                retrieval_finished_at=finished_at,
                duration_ms=int((finished_at - started_at) * 1000),
                trace_status=trace_status,
            )

        with observability_scope(
            user_id=user_id,
            session_id=build_chat_session_id(chat_id),
            tags=["memory", "search"],
            metadata={
                "operation": "memory.search",
                "chat_id": chat_id,
                "character_id": character_id,
                "query_length": len(query),
            },
        ):
            query_vectors = self._encode_embeddings(
                [query],
                user_id=user_id,
                character_id=character_id,
                chat_id=chat_id,
            )
            if not query_vectors:
                # Embedding failed: no retrieval happened; never fake zeros.
                trace = _finish_trace(
                    retrieved_ids=[],
                    seed_ids=[],
                    expanded_ids=[],
                    semantic_ids=[],
                    semantic_mode="all" if self._config.use_all_semantic else "top_k",
                    distances={},
                    is_seed={},
                    trace_status="unavailable",
                )
                return {"episodic": [], "semantic": []}, trace

            query_vector = query_vectors[0]
            q = normalize(np.array(query_vector))

            semantic_mode = "all" if self._config.use_all_semantic else "top_k"
            semantic_distances: dict[int, float] = {}
            if self._config.use_all_semantic:
                semantic_filter = build_filter(user_id, character_id, 'memory_type == "semantic"')
                semantic_records = self._store.query(
                    filter_expr=semantic_filter,
                    limit=1000,
                    output_fields=[
                        "id",
                        "group_id",
                        "user_id",
                        "character_id",
                        "memory_type",
                        "ts",
                        "chat_id",
                        "text",
                    ],
                )
                semantic_memories = [self._hit_to_memory_record(hit) for hit in semantic_records]
            else:
                semantic_filter = build_filter(user_id, character_id, 'memory_type == "semantic"')
                semantic_results = self._store.search(
                    vectors=[query_vector],
                    filter_expr=semantic_filter,
                    limit=self._config.k_semantic,
                    output_fields=[
                        "id",
                        "group_id",
                        "user_id",
                        "character_id",
                        "memory_type",
                        "ts",
                        "chat_id",
                        "text",
                    ],
                )
                semantic_memories = []
                if semantic_results and semantic_results[0]:
                    for hit in semantic_results[0]:
                        distance = hit.get("distance")
                        if distance is not None:
                            semantic_distances[int(hit.get("id", 0))] = float(distance)
                        semantic_memories.append(
                            self._hit_to_memory_record(hit, distance=distance)
                        )

            episodic_filter = build_filter(user_id, character_id, 'memory_type == "episodic"')
            episodic_results = self._store.search(
                vectors=[q.tolist()],
                filter_expr=episodic_filter,
                limit=self._config.k_episodic,
                output_fields=[
                    "id",
                    "group_id",
                    "user_id",
                    "character_id",
                    "memory_type",
                    "ts",
                    "chat_id",
                    "text",
                ],
            )

            seeds = episodic_results[0] if episodic_results and episodic_results[0] else []
            # Record seed provenance BEFORE narrative-group expansion.
            seed_id_list: list[int] = []
            seed_distances: dict[int, float] = {}
            for hit in seeds:
                seed_id = int(hit.get("id", 0))
                seed_id_list.append(seed_id)
                distance = hit.get("distance")
                if distance is not None:
                    seed_distances[seed_id] = float(distance)

            if not seeds:
                logger.info(
                    "Memory operation 'search': user_id=%s, episodic_results=0, semantic_results=%s",
                    user_id,
                    len(semantic_memories),
                )
                trace = _finish_trace(
                    retrieved_ids=[record.id for record in semantic_memories],
                    seed_ids=[],
                    expanded_ids=[],
                    semantic_ids=[record.id for record in semantic_memories],
                    semantic_mode=semantic_mode,
                    distances=semantic_distances,
                    is_seed={},
                )
                return {"episodic": [], "semantic": semantic_memories}, trace

            expansion_group_ids = set()
            for hit in seeds:
                g_id = hit.get("group_id")
                if g_id is None:
                    continue
                g_id = int(g_id)
                if g_id == -1:
                    continue
                expansion_group_ids.add(g_id)

            expanded_member_ids = set()
            if expansion_group_ids:
                sorted_group_ids = sorted(expansion_group_ids)
                members_res = self._store.query(
                    filter_expr=build_filter(
                        user_id,
                        character_id,
                        f"group_id in {sorted_group_ids}",
                    ),
                    output_fields=["id"],
                )
                expanded_member_ids.update(
                    int(row["id"]) for row in members_res if row.get("id") is not None
                )

            seed_ids = {int(hit["id"]) for hit in seeds}
            all_ids = seed_ids | expanded_member_ids
            final_memories: list[MemoryRecord] = []
            if all_ids:
                id_list = sorted(all_ids)
                mem_res = self._store.query(
                    filter_expr=build_filter(
                        user_id,
                        character_id,
                        f"id in {id_list}",
                    ),
                    output_fields=[
                        "id",
                        "user_id",
                        "character_id",
                        "memory_type",
                        "ts",
                        "chat_id",
                        "text",
                        "group_id",
                    ],
                )
                id2row = {row["id"]: row for row in mem_res}
                for hit in seeds:
                    row = id2row.get(int(hit["id"]))
                    if row:
                        final_memories.append(
                            self._hit_to_memory_record(
                                row,
                                distance=seed_distances.get(int(hit["id"])),
                                is_seed=True,
                            )
                        )
                for memory_id in sorted(mid for mid in expanded_member_ids if mid not in seed_ids):
                    row = id2row.get(memory_id)
                    if row:
                        final_memories.append(
                            self._hit_to_memory_record(row, is_seed=False)
                        )

            logger.info(
                "Memory operation 'search': user_id=%s, episodic_results=%s (seeds=%s, expanded=%s), semantic_results=%s",
                user_id,
                len(final_memories),
                len(seeds),
                len(expanded_member_ids),
                len(semantic_memories),
            )
            is_seed_map: dict[int, bool] = {record.id: True for record in final_memories if record.is_seed}
            is_seed_map.update(
                {record.id: False for record in final_memories if record.is_seed is False}
            )
            trace = _finish_trace(
                retrieved_ids=[record.id for record in final_memories]
                + [record.id for record in semantic_memories],
                seed_ids=seed_id_list,
                expanded_ids=sorted(mid for mid in expanded_member_ids if mid not in seed_ids),
                semantic_ids=[record.id for record in semantic_memories],
                semantic_mode=semantic_mode,
                distances={**seed_distances, **semantic_distances},
                is_seed=is_seed_map,
            )
            return {
                "episodic": final_memories,
                "semantic": semantic_memories,
            }, trace

    def list_all_for_user_character(
        self,
        *,
        user_id: str,
        character_id: str,
        semantic_limit: int = 1000,
        episodic_limit: int = 1000,
    ) -> dict[str, list[MemoryRecord]]:
        semantic_filter = build_filter(user_id, character_id, 'memory_type == "semantic"')
        semantic_records = self._store.query(
            filter_expr=semantic_filter,
            limit=semantic_limit,
            output_fields=[
                "id",
                "group_id",
                "user_id",
                "character_id",
                "memory_type",
                "ts",
                "chat_id",
                "text",
            ],
        )
        episodic_filter = build_filter(user_id, character_id, 'memory_type == "episodic"')
        episodic_records = self._store.query(
            filter_expr=episodic_filter,
            limit=episodic_limit,
            output_fields=[
                "id",
                "group_id",
                "user_id",
                "character_id",
                "memory_type",
                "ts",
                "chat_id",
                "text",
            ],
        )
        return {
            "semantic": [self._hit_to_memory_record(row) for row in semantic_records],
            "episodic": sorted(
                (self._hit_to_memory_record(row) for row in episodic_records),
                key=lambda item: item.ts,
                reverse=True,
            ),
        }

    def count_for_user_character(self, *, user_id: str, character_id: str) -> int:
        return int(self._store.count(build_filter(user_id, character_id)) or 0)

    async def record_proactive_outreach_async(
        self,
        *,
        proactive_message: str,
        user_id: str,
        character_id: str,
        chat_id: str,
    ) -> int | None:
        summary = proactive_message.strip()
        if not summary:
            return None

        memory_text = f'The character proactively messaged the user: "{summary[:200]}"'

        try:
            with observability_scope(
                user_id=user_id,
                session_id=build_chat_session_id(chat_id),
                tags=["memory", "proactive_outreach"],
                metadata={
                    "operation": "memory.proactive_outreach",
                    "chat_id": chat_id,
                    "character_id": character_id,
                },
            ):
                embeddings = await run_blocking(
                    self._encode_embeddings,
                    [memory_text],
                    user_id=user_id,
                    character_id=character_id,
                    chat_id=chat_id,
                )
                if not embeddings:
                    return None

                ids = await run_blocking(
                    self._store.insert,
                    [
                        {
                            "user_id": user_id,
                            "character_id": character_id,
                            "memory_type": "episodic",
                            "ts": int(time.time()),
                            "chat_id": chat_id,
                            "text": memory_text,
                            "vector": embeddings[0],
                            "group_id": -1,
                        }
                    ],
                )
                return ids[0] if ids else None
        except Exception:
            logger.exception(
                "Failed to record proactive outreach memory. user_id=%s character_id=%s chat_id=%s",
                user_id,
                character_id,
                chat_id,
            )
            return None

    
    def _hit_to_memory_record(
        self,
        hit: dict[str, Any],
        *,
        distance: float | None = None,
        is_seed: bool | None = None,
    ) -> MemoryRecord:
        """Convert a search hit to MemoryRecord.

        ``distance`` defaults to the store-reported hit distance when present;
        explicit values come from the trace-enabled search path.
        """
        if distance is None:
            raw_distance = hit.get("distance")
            distance = float(raw_distance) if raw_distance is not None else None
        return MemoryRecord(
            id=hit.get("id", 0),
            user_id=hit.get("user_id", ""),
            character_id=hit.get("character_id", ""),
            memory_type=hit.get("memory_type", ""),
            ts=hit.get("ts", 0),
            chat_id=hit.get("chat_id", ""),
            text=hit.get("text", ""),
            group_id=hit.get("group_id", -1),
            distance=distance,
            is_seed=is_seed,
        )

    
    @observe(name="memory.narrative_assign")
    def assign_to_narrative_group(
        self,
        memory_ids: list[int],
        user_id: str,
        character_id: str,
        chat_id: str,
    ) -> dict[int, int]:

        """将被使用的情景记忆分配到叙事组。
        
        Args:
            memory_ids: 被MemoryUsageJudge判断为实际使用的情景记忆ID列表
            user_id: 用户标识
            
        Returns:
            Dict[int, int] - memory_id到group_id的映射
        """
        with observability_scope(
            user_id=user_id,
            session_id=build_chat_session_id(chat_id),
            tags=["memory", "narrative_assign"],
            metadata={
                "operation": "memory.narrative_assign",
                "chat_id": chat_id,
                "character_id": character_id,
                "memory_ids_count": len(memory_ids),
            },
        ):
            return self._narrative_manager.assign_to_narrative_group(
                memory_ids,
                user_id,
                character_id,
            )

    async def judge_and_assign_narrative_group_async(
        self,
        episodic_memories: list[MemoryRecord],
        last_user: str,
        last_assistant: str,
        user_id: str,
        character_id: str,
        chat_id: str,
        *,
        trace_sink: Any | None = None,
    ) -> list[int]:
        """Judge used episodic memory IDs and assign them to narrative groups.

        Intended for fire-and-forget usage from streaming endpoints.
        Never raises; returns used memory ids (best-effort) for logging/observability.

        ``trace_sink`` is an optional evaluation-only callback receiving a
        structured ``MemoryUsageTrace``; production callers leave it None.
        """
        try:
            if not episodic_memories:
                return []

            judge_input = [
                {"id": int(mem.id), "text": mem.text}
                for mem in episodic_memories
                if mem and mem.id
            ]
            if not judge_input:
                return []

            used_ids = await self._memory_usage_judge.judge_used_memory_ids(
                episodic_memories=judge_input,
                last_user=last_user,
                last_assistant=last_assistant,
                user_id=user_id,
                chat_id=chat_id,
                character_id=character_id,
                trace_sink=trace_sink,
            )
            if not used_ids:
                return []

            await run_blocking(
                self.assign_to_narrative_group,
                used_ids,
                user_id,
                character_id,
                chat_id,
            )
            return used_ids
        except Exception as e:
            logger.warning(f"Failed to judge/assign narrative memory groups: {e}")
            return []
     
     
 
 
    def update(
        self,
        memory_id: int,
        data: dict[str, Any],
        user_id: str | None = None,
        character_id: str | None = None
    ) -> bool:
        """Update a memory record in-place (same ID).

        The updated record is detached from narrative groups (`group_id=-1`).
        If the record previously belonged to a group, that old group is
        recomputed (or deleted when empty).
        """
        if "text" not in data:
            logger.warning(f"Memory operation 'update' failed: memory_id={memory_id}, no 'text' field provided")
            return False
        
        if user_id is None or character_id is None:
            logger.warning(
                f"Memory operation 'update' failed: memory_id={memory_id}, user_id and character_id are required"
            )
            return False

        try:
            # 1) Read existing record so we can preserve identity fields and old group metadata.
            original_memories = self._store.query(
                filter_expr=build_filter(
                    user_id,
                    character_id,
                    f"id == {memory_id}"
                ),
                output_fields=[
                    "id",
                    "user_id",
                    "character_id",
                    "memory_type",
                    "ts",
                    "chat_id",
                    "text",
                    "group_id",
                ]
            )

            if not original_memories:
                logger.warning(f"Memory operation 'update' failed: memory_id={memory_id}, memory not found")
                return False
            
            original = original_memories[0]
            old_group_id = int(original.get("group_id", -1))

            # 2) Compute embedding first to avoid partial updates on upstream failures.
            new_text = str(data["text"])
            embeddings = self._encode_embeddings(
                [new_text],
                user_id=user_id,
                character_id=character_id,
            )
            
            if not embeddings:
                logger.warning(f"Memory operation 'update' failed: memory_id={memory_id}, embedding generation failed")
                return False

            # 3) In-place upsert keeps memory_id stable.
            updated = self._store.update_memory(
                memory_id=memory_id,
                user_id=user_id,
                character_id=character_id,
                patch={
                    "text": new_text,
                    "vector": embeddings[0],
                    "ts": int(time.time()),
                    "group_id": -1,
                },
            )
            if not updated:
                logger.warning(f"Memory operation 'update' failed: memory_id={memory_id}, upsert failed")
                return False

            # 4) If detached from a previous narrative group, refresh that old group.
            if old_group_id != -1:
                try:
                    self._narrative_manager.cleanup_group(
                        user_id=user_id,
                        character_id=character_id,
                        group_id=old_group_id,
                    )
                except Exception as e:
                    logger.warning(
                        "Memory update succeeded but old group cleanup failed: memory_id=%s group_id=%s err=%s",
                        memory_id,
                        old_group_id,
                        e,
                    )

            logger.info(
                "Memory operation 'update': memory_id=%s, text_updated=True, in_place=True, "
                "group_reset=True, old_group_id=%s",
                memory_id,
                old_group_id,
            )
            return True
        except Exception as e:
            logger.warning(f"Memory operation 'update' failed: memory_id={memory_id}, error: {e}")
            return False
    
    def delete(self, memory_id: int, user_id: str | None = None, character_id: str | None = None) -> bool:
        """Delete a memory record with narrative group cleanup.

        
        Args:
            memory_id: ID of memory to delete
            user_id: User ID (required for narrative group cleanup)
            
        Returns:
            True if deletion succeeded
        """
        original_group_id: int | None = None
        if user_id is not None and character_id is not None:
            try:
                existing = self._store.query(
                    filter_expr=build_filter(
                        user_id,
                        character_id,
                        f"id == {memory_id}",
                    ),
                    output_fields=["group_id"],
                    limit=1,
                )
                if existing:
                    original_group_id = int(existing[0].get("group_id", -1))
            except Exception as e:
                logger.warning(f"Failed to query group_id before deleting memory {memory_id}: {e}")
        
        count = self._store.delete(ids=[memory_id])

        success = count > 0

        if (
            success
            and user_id is not None
            and character_id is not None
            and original_group_id is not None
            and original_group_id != -1
        ):
            try:
                self._narrative_manager.cleanup_group(
                    user_id=user_id,
                    character_id=character_id,
                    group_id=original_group_id,
                    exclude_memory_id=memory_id,
                )
            except Exception as e:
                logger.warning(f"Failed to cleanup narrative group for memory {memory_id}: {e}")
        
        if success:
            logger.info(
                f"Memory operation 'delete': memory_id={memory_id}, affected_count=1"
            )
        else:
            logger.warning(
                f"Memory operation 'delete' failed: memory_id={memory_id}, "
                f"affected_count=0 (not found)"
            )
        
        return success
    
    def reset(self, user_id: str, character_id: str) -> int:
        """Delete all memories for a user.

        
        Args:
            user_id: User identifier
            
        Returns:
            Number of deleted memories
        """
        filter_expr = build_filter(user_id, character_id)
        count = self._store.delete(filter_expr=filter_expr)
        groups_deleted = 0
        try:
            groups_deleted = self._store.delete_groups_for_scope(
                user_id=user_id,
                character_id=character_id,
            )
        except Exception as e:
            logger.warning(
                "Memory reset group cleanup failed: user_id=%s character_id=%s err=%s",
                user_id,
                character_id,
                e,
            )

        
        logger.info(
            "Memory operation 'reset': user_id=%s, affected_count=%s, groups_deleted=%s",
            user_id,
            count,
            groups_deleted,
        )
        
        return count
    


    def purge_character(self, character_id: str) -> int:
        """Delete every memory and narrative group for a character, all users.

        Used when a character is permanently deleted: the character id will
        never be referenced again, so no per-user scoping applies.

        Args:
            character_id: Character identifier

        Returns:
            Number of deleted memories
        """
        filter_expr = build_filter(None, character_id)
        count = self._store.delete(filter_expr=filter_expr)
        groups_deleted = 0
        try:
            groups_deleted = self._store.delete_groups_for_character(character_id)
        except Exception as e:
            logger.warning(
                "Memory purge group cleanup failed: character_id=%s err=%s",
                character_id,
                e,
            )

        logger.info(
            "Memory operation 'purge_character': character_id=%s, affected_count=%s, groups_deleted=%s",
            character_id,
            count,
            groups_deleted,
        )

        return count

    @observe(name="memory.consolidate.batch.async")
    async def consolidate_async(self, user_id: str, character_id: str) -> ConsolidationStats:
        """Run consolidation process for memories.

        
        Performs batch pattern merging: analyzes multiple episodic memories together
        to extract stable, long-term semantic facts.
        
        Args:
            user_id: User identifier
            character_id: Character identifier
             
        Returns:
            ConsolidationStats with operation counts
        """
        # Consolidation must never mix across users/characters, otherwise semantic memory
        # becomes polluted and can leak information across identities.
        if not user_id or not character_id:
            raise ValueError("user_id and character_id are required for consolidate()")

        stats = ConsolidationStats()
        consolidation_started = time.monotonic()

        # 1. Query episodic memories to process
        episodic_filter = build_filter(user_id, character_id, 'memory_type == "episodic"')
        semantic_filter = build_filter(user_id, character_id, 'memory_type == "semantic"')

        episodic_memories = await run_blocking(
            self._store.query,
            filter_expr=episodic_filter,
            limit=1000,
            output_fields=["user_id", "chat_id", "text"],
        )
        stats.memories_processed = len(episodic_memories)
        stats.physical_before_episodic = len(episodic_memories)
        if not episodic_memories:
            logger.info(
                "Consolidation skipped for user_id=%s character_id=%s: no episodic memories",
                user_id or "all",
                character_id or "all",
            )
            stats.duration_ms = int((time.monotonic() - consolidation_started) * 1000)
            stats.action_counts = {"no_op": 1}
            return stats

        semantic_memories = await run_blocking(
            self._store.query,
            filter_expr=semantic_filter,
            limit=1000,
            output_fields=["text"],
        )
        stats.physical_before_semantic = len(semantic_memories)
        
        logger.info(
            f"Consolidation started for user_id={user_id or 'all'}: "
            f"processing {len(episodic_memories)} episodic memories, "
            f"{len(semantic_memories)} existing semantic memories"
        )
        
        # 2. Prepare batch processing data
        episodic_texts = [mem.get("text", "") for mem in episodic_memories]
        existing_semantic_texts = [mem.get("text", "") for mem in semantic_memories]

        consolidation_data = {
            "episodic_texts": episodic_texts,
            "existing_semantic_texts": existing_semantic_texts
        }

        # Logical fact identity: distinct episodic + distinct semantic facts
        # before extraction; after extraction the episodic layer is logically
        # superseded by the semantic layer (physical episodic rows are kept).
        distinct_episodic = {text.strip() for text in episodic_texts if str(text).strip()}
        distinct_semantic_before = {
            text.strip() for text in existing_semantic_texts if str(text).strip()
        }
        stats.logical_before_count = len(distinct_episodic) + len(distinct_semantic_before)
        stats.batch_fingerprint = hashlib.sha256(
            "\n".join(sorted(distinct_episodic | distinct_semantic_before)).encode("utf-8")
        ).hexdigest()
        
        # 3. Call batch pattern merging
        with observability_scope(
            user_id=user_id,
            session_id=build_memory_batch_session_id(user_id),
            tags=["memory", "consolidate"],
            metadata={
                "operation": "memory.consolidate.batch",
                "character_id": character_id,
            },
        ):
            extraction = await self._semantic_writer.extract(
                consolidation_data,
                user_id=user_id,
                character_id=character_id,
            )
        
        # 4. Create new semantic memories
        created_facts: list[str] = []
        if extraction.write_semantic and extraction.facts:
            # Use first episodic memory as source for metadata (user_id, chat_id)
            source_memory = episodic_memories[0] if episodic_memories else {}
            await run_blocking(
                self._create_semantic_memories,
                source_memory,
                extraction.facts,
                character_id,
            )
            stats.semantic_created += len(extraction.facts)
            created_facts = [str(fact) for fact in extraction.facts]

        # Final structured stats (phase18): physical episodic rows never shrink,
        # logical after-count = distinct semantic facts once episodic is
        # superseded by the extracted semantic layer.
        stats.physical_after_episodic = stats.physical_before_episodic
        stats.physical_after_semantic = stats.physical_before_semantic + len(created_facts)
        distinct_semantic_after = distinct_semantic_before | {
            text.strip() for text in created_facts if text.strip()
        }
        stats.logical_after_count = len(distinct_semantic_after)
        stats.action_counts = {
            "create": len(created_facts),
            "retain": stats.physical_after_episodic,
        }
        if not created_facts:
            stats.action_counts["no_op"] = 1
        stats.duration_ms = int((time.monotonic() - consolidation_started) * 1000)

        # Log consolidation statistics
        logger.info(
            f"Consolidation complete for user_id={user_id or 'all'}: "
            f"processed={stats.memories_processed}, semantic_created={stats.semantic_created}"
        )

        return stats


    
    def _create_semantic_memories(
        self,
        source_memory: dict[str, Any],
        facts: list[str],
        character_id: str | None
    ) -> list[int]:

        """Create semantic memories from extracted facts.
        
        In v2 schema, all information is stored in the text field.
        """
        user_id = source_memory.get("user_id", "")
        source_chat_id = source_memory.get("chat_id", "")
        if character_id is None:
            return []

        
        # Generate embeddings for facts
        embeddings = self._encode_embeddings(
            facts,
            user_id=user_id,
            character_id=character_id,
            chat_id=source_chat_id or None,
        )
        
        if len(embeddings) != len(facts):
            return []
        
        entities = []
        current_ts = int(time.time())
        
        for i, fact in enumerate(facts):
            # In v2 schema, the fact is stored directly in text field
            entity = {
                "user_id": user_id,
                "character_id": character_id,
                "memory_type": "semantic",
                "ts": current_ts,
                "chat_id": source_chat_id,
                "text": fact,
                "vector": embeddings[i],
                "group_id": -1,
            }

            entities.append(entity)
        
        ids = self._store.insert(entities)
        
        logger.info(
            f"Memory operation 'create_semantic': type=semantic, user_id={user_id}, "
            f"source_chat_id={source_chat_id}, affected_count={len(ids)}"
        )
        
        return ids
    
    @property
    def store(self) -> MilvusStore:
        """Access the underlying MilvusStore (for testing)."""
        return self._store
    
    @property
    def config(self) -> MemoryConfig:
        """Access the configuration."""
        return self._config

    @property
    def gateway_service(self) -> LLMGatewayService:
        return self._gateway_service

    @property
    def llm_route(self) -> ResolvedLLMRoute:
        return self._llm_route
