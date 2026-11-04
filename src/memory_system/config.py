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
    llm_timeout_seconds: float = settings.LLM_TIMEOUT_SECONDS
    llm_max_retries: int = settings.LLM_MAX_RETRIES
    llm_retry_base_delay_seconds: float = settings.LLM_RETRY_BASE_DELAY_SECONDS

    # Retrieval
    k_semantic: int = settings.K_SEMANTIC
    k_episodic: int = settings.K_EPISODIC
    use_all_semantic: bool = settings.USE_ALL_SEMANTIC

    # Langfuse
    langfuse_secret_key: str | None = settings.LANGFUSE_SECRET_KEY
    langfuse_public_key: str | None = settings.LANGFUSE_PUBLIC_KEY
    langfuse_base_url: str = (
        settings.LANGFUSE_BASE_URL
        or settings.LANGFUSE_HOST
        or "https://cloud.langfuse.com"
    )

    # Narrative memory
    narrative_similarity_threshold: float = settings.NARRATIVE_SIMILARITY_THRESHOLD

    @property
    def llm_model(self) -> str:
        if self.llm_route is None:
            raise ValueError("MemoryConfig.llm_route is required")
        return self.llm_route.model

    @property
    def llm_api_key(self) -> str | None:
        if self.llm_route is None:
            raise ValueError("MemoryConfig.llm_route is required")
        return self.llm_route.api_key

    @property
    def llm_api_base(self) -> str | None:
        if self.llm_route is None:
            raise ValueError("MemoryConfig.llm_route is required")
        return self.llm_route.api_base


Config = MemoryConfig
