"""Infrastructure layer clients for AI Memory System."""

from ..exceptions import LLMCallError, MilvusConnectionError

__all__ = [
    "EmbeddingClient",
    "LLMClient",
    "MilvusStore",
    "MilvusConnectionError",
    "LLMCallError",
]


def __getattr__(name: str):
    if name == "EmbeddingClient":
        from .embedding import EmbeddingClient

        return EmbeddingClient
    if name == "LLMClient":
        from .llm import LLMClient

        return LLMClient
    if name == "MilvusStore":
        from .milvus_store import MilvusStore

        return MilvusStore
    raise AttributeError(name)
