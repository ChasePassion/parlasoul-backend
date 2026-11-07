"""Configuration module for AI Memory System."""

from dataclasses import dataclass

from src.core.config import settings
from src.core.llm_registry import ResolvedLLMRoute


@dataclass
class MemoryConfig:
    """Configuration for the AI memory system."""

    # Milvus vector database
    milvus_uri: str = settings.MILVUS_URL
    milvus_token: str | None = settings.MILVUS_TOKEN
    collection_name: str = settings.MEMORY_COLLECTION_NAME
    group_collection_name: str = settings.MEMORY_GROUP_COLLECTION_NAME

    # Embedding model
    embedding_api_key: str = settings.EMBEDDING_API_KEY
    embedding_base_url: str = settings.EMBEDDING_BASE_URL
    embedding_model: str = settings.EMBEDDING_MODEL
    embedding_dim: int = settings.MEMORY_EMBEDDING_DIM

    # Chat LLM
    llm_route: ResolvedLLMRoute | None = None

    # Retrieval
    k_semantic: int = settings.K_SEMANTIC
    k_episodic: int = settings.K_EPISODIC
    use_all_semantic: bool = settings.USE_ALL_SEMANTIC

    # Narrative memory
    narrative_similarity_threshold: float = settings.NARRATIVE_SIMILARITY_THRESHOLD
