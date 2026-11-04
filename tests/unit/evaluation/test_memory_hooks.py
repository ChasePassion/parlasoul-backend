"""Phase18 memory hook guarantees:

- sink off: production return values unchanged;
- sink on: IDs / distances / seed-vs-expanded / timing / action counts
  recomputable from the trace alone;
- failures surface as explicit ``unavailable`` status, never zeros.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.memory_system.memory import Memory
from src.memory_system.processors.memory_usage_judge import (
    MemoryUsageJudge,
    MemoryUsageTrace,
    memory_usage_prompt_hash,
)


def _hit(memory_id: int, text: str, *, group_id: int = -1, distance: float | None = None) -> dict:
    row = {
        "id": memory_id,
        "user_id": "u1",
        "character_id": "c1",
        "memory_type": "episodic",
        "ts": 10,
        "chat_id": "chat-1",
        "text": text,
        "group_id": group_id,
    }
    if distance is not None:
        row["distance"] = distance
    return row


def _memory_with_stubs() -> Memory:
    memory = Memory.__new__(Memory)
    memory._config = SimpleNamespace(use_all_semantic=False, k_semantic=5, k_episodic=5)
    memory._embedding_client = Mock()
    memory._store = Mock()
    memory._embedding_client.encode.return_value = [[1.0, 0.0]]
    del memory._embedding_client.encode_with_usage
    return memory


def test_search_with_trace_records_seed_expansion_distances_and_timing() -> None:
    memory = _memory_with_stubs()
    memory._store.search.side_effect = [
        [[_hit(900, "semantic fact", distance=0.2)]],  # semantic top-k
        [[_hit(1, "seed-1", group_id=10, distance=0.9), _hit(2, "seed-2", distance=0.7)]],
    ]
    memory._store.query.side_effect = [
        [{"id": 3}, {"id": 1}],  # group members of group 10
        [
            _hit(3, "expanded-3", group_id=10),
            _hit(1, "seed-1", group_id=10),
            _hit(2, "seed-2"),
        ],
    ]

    results, trace = memory.search_with_trace("query", "u1", "c1", "chat-1")

    assert [record.id for record in results["episodic"]] == [1, 2, 3]
    assert trace.trace_status == "ok"
    assert trace.seed_ids == [1, 2]
    assert trace.expanded_ids == [3]
    assert trace.semantic_ids == [900]
    assert trace.semantic_mode == "top_k"
    assert trace.distances[1] == 0.9
    assert trace.distances[900] == 0.2
    assert 3 not in trace.distances  # expanded members carry no distance
    assert trace.is_seed[1] is True
    assert trace.is_seed[3] is False
    assert trace.duration_ms >= 0
    assert trace.retrieval_finished_at >= trace.retrieval_started_at
    # final episodic records carry provenance
    by_id = {record.id: record for record in results["episodic"]}
    assert by_id[1].is_seed is True and by_id[1].distance == 0.9
    assert by_id[3].is_seed is False and by_id[3].distance is None


def test_search_compat_returns_same_records_without_trace() -> None:
    def build() -> Memory:
        memory = _memory_with_stubs()
        memory._store.search.side_effect = [
            [[_hit(900, "semantic fact")]],
            [[_hit(1, "seed-1", distance=0.5)]],
        ]
        memory._store.query.side_effect = [[_hit(1, "seed-1")]]
        return memory

    compat = build().search("query", "u1", "c1", "chat-1")
    traced, _ = build().search_with_trace("query", "u1", "c1", "chat-1")

    assert [record.id for record in compat["episodic"]] == [
        record.id for record in traced["episodic"]
    ]
    assert [record.text for record in compat["semantic"]] == [
        record.text for record in traced["semantic"]
    ]


def test_search_embedding_failure_marks_trace_unavailable() -> None:
    memory = _memory_with_stubs()
    memory._embedding_client.encode.return_value = []

    results, trace = memory.search_with_trace("query", "u1", "c1", "chat-1")

    assert results == {"episodic": [], "semantic": []}
    assert trace.trace_status == "unavailable"
    assert trace.retrieved_ids == []


def _judge_with_gateway(response=None, error: Exception | None = None) -> MemoryUsageJudge:
    gateway = Mock()
    if error is not None:
        gateway.generate_json = AsyncMock(side_effect=error)
    else:
        gateway.generate_json = AsyncMock(return_value=response or {})
    route = SimpleNamespace(provider="dashscope", model="m")
    return MemoryUsageJudge(gateway, route)  # type: ignore[arg-type]


async def test_usage_judge_sink_off_keeps_production_return_value() -> None:
    judge = _judge_with_gateway({"used_episodic_memories": ["memory one"]})

    used = await judge.judge_used_memory_ids(
        [{"id": 11, "text": "memory one"}, {"id": 12, "text": "memory two"}],
        "last user",
        "last assistant",
        user_id="u1",
        chat_id=None,
        character_id="c1",
    )

    assert used == [11]


async def test_usage_judge_sink_on_reports_structured_trace() -> None:
    judge = _judge_with_gateway({"used_episodic_memories": ["memory two"]})
    captured: list[MemoryUsageTrace] = []

    used = await judge.judge_used_memory_ids(
        [{"id": 11, "text": "memory one"}, {"id": 12, "text": "memory two"}],
        "last user",
        "last assistant",
        user_id="u1",
        chat_id=None,
        character_id="c1",
        trace_sink=captured.append,
    )

    assert used == [12]
    assert len(captured) == 1
    trace = captured[0]
    assert trace.retrieved_ids == (11, 12)
    assert trace.used_ids == (12,)
    assert trace.judge_status == "ok"
    assert trace.prompt_hash == memory_usage_prompt_hash()
    assert trace.duration_ms >= 0


async def test_usage_judge_error_emits_unavailable_and_returns_empty() -> None:
    judge = _judge_with_gateway(error=RuntimeError("upstream down"))
    captured: list[MemoryUsageTrace] = []

    used = await judge.judge_used_memory_ids(
        [{"id": 11, "text": "memory one"}],
        "last user",
        "last assistant",
        user_id="u1",
        chat_id=None,
        character_id="c1",
        trace_sink=captured.append,
    )

    assert used == []
    assert len(captured) == 1
    assert captured[0].judge_status == "unavailable"
    assert captured[0].retrieved_ids == (11,)
    assert captured[0].used_ids == ()


async def test_consolidate_stats_logical_counts_and_actions() -> None:
    memory = _memory_with_stubs()
    # episodic query, semantic query
    memory._store.query.side_effect = [
        [
            {"user_id": "u1", "chat_id": "c", "text": "The user likes jazz."},
            {"user_id": "u1", "chat_id": "c", "text": "The user likes jazz!"},
        ],
        [],
    ]
    memory._store.insert.return_value = [501]
    memory._semantic_writer = SimpleNamespace(
        extract=AsyncMock(
            return_value=SimpleNamespace(write_semantic=True, facts=["The user likes jazz."])
        )
    )

    stats = await memory.consolidate_async("u1", "c1")

    # legacy totals preserved
    assert stats.memories_processed == 2
    assert stats.semantic_created == 1
    # structured phase18 stats
    assert stats.physical_before_episodic == 2
    assert stats.physical_after_episodic == 2  # episodic never deleted
    assert stats.physical_before_semantic == 0
    assert stats.physical_after_semantic == 1
    # logical: 2 near-duplicate episodic facts + 0 semantic before;
    # 1 extracted semantic fact after supersession
    assert stats.logical_before_count == 2
    assert stats.logical_after_count == 1
    assert stats.action_counts == {"create": 1, "retain": 2}
    assert stats.duration_ms >= 0
    assert len(stats.batch_fingerprint) == 64


async def test_consolidate_no_episodic_reports_no_op() -> None:
    memory = _memory_with_stubs()
    memory._store.query.side_effect = [[]]

    stats = await memory.consolidate_async("u1", "c1")

    assert stats.memories_processed == 0
    assert stats.action_counts == {"no_op": 1}
    assert stats.logical_before_count == 0
