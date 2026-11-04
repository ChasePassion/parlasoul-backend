"""AI Memory System - A cognitive psychology-based long-term memory system for AI."""

from .config import MemoryConfig
from .exceptions import LLMCallError, MilvusConnectionError
from .memory import (
    MEMORY_RETRIEVAL_TRACE_VERSION,
    ConsolidationStats,
    Memory,
    MemoryRecord,
    MemoryRetrievalTrace,
)

__all__ = [
    # Main API
    "Memory",
    "MemoryRecord",
    "ConsolidationStats",
    "MemoryConfig",
    "MemoryRetrievalTrace",
    "MEMORY_RETRIEVAL_TRACE_VERSION",
    # Exceptions
    "MilvusConnectionError",
    "LLMCallError",
]


def __getattr__(name: str):
    if name == "EmbeddingClient":
        from .clients.embedding import EmbeddingClient

        return EmbeddingClient
    if name == "LLMClient":
        from .clients.llm import LLMClient

        return LLMClient
    if name == "MilvusStore":
        from .clients.milvus_store import MilvusStore

        return MilvusStore
    raise AttributeError(name)
