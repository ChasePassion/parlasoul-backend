from __future__ import annotations

from src.core.config import (
    Settings,
    _normalize_asyncpg_database_url,
    _normalize_milvus_url,
    _resolve_env_file,
)
from src.core.llm_settings import LLMSettings


def test_cors_origins_list_parsing() -> None:
    s = Settings(
        DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
        SECRET_KEY="secret",
        SMTP_HOST="smtp.example.com",
        SMTP_USER="user",
        SMTP_PASSWORD="pass",
        MILVUS_URL="http://localhost:19530",
        SILICONFLOW_API_KEY="token",
        CORS_ORIGINS="http://a.com, http://b.com ,,",
    )

    assert s.cors_origins_list == ["http://a.com", "http://b.com"]


def test_resolve_env_file_matches_repo_convention() -> None:
    assert _resolve_env_file("development") == ".env"
    assert _resolve_env_file("production") == ".env.production"
    assert _resolve_env_file("staging") == ".env.staging"




def test_normalize_asyncpg_database_url_rewrites_neon_ssl_query() -> None:
    normalized = _normalize_asyncpg_database_url(
        "postgresql+asyncpg://u:p@localhost/db?sslmode=require&channel_binding=require"
    )

    assert normalized == "postgresql+asyncpg://u:p@localhost/db?ssl=require"


def test_database_connect_args_force_public_search_path_for_asyncpg() -> None:
    s = Settings(
        DATABASE_URL="postgresql+asyncpg://u:p@localhost/db?ssl=require",
        SECRET_KEY="secret",
        SMTP_HOST="smtp.example.com",
        SMTP_USER="user",
        SMTP_PASSWORD="pass",
        MILVUS_URL="http://localhost:19530",
        SILICONFLOW_API_KEY="token",
    )

    assert s.database_connect_args == {"server_settings": {"search_path": "public"}}


def test_normalize_milvus_url_adds_port_443_for_https_without_port() -> None:
    normalized = _normalize_milvus_url("https://zilliz.example.com")

    assert normalized == "https://zilliz.example.com:443"


def test_normalize_milvus_url_preserves_explicit_port() -> None:
    normalized = _normalize_milvus_url("https://zilliz.example.com:19530")

    assert normalized == "https://zilliz.example.com:19530"


def test_llm_settings_resolve_provider_local_variables() -> None:
    s = LLMSettings(
        OPENAI_API_KEY="openai-key",
        OPENAI_API_BASE="https://api.openai.com/v1",
        OPENAI_MODEL_ID="gpt-5.1-mini",
        LLM_DEEPSEEK_API_KEY="deepseek-key",
        LLM_DEEPSEEK_API_BASE="https://api.deepseek.com",
        LLM_DEEPSEEK_MODEL_ID="deepseek-chat",
        LLM_OPENROUTER_API_KEY="openrouter-key",
        LLM_OPENROUTER_API_BASE="https://openrouter.ai/api/v1",
        LLM_OPENROUTER_MODEL_ID="openai/gpt-4.1-mini",
        LLM_DASHSCOPE_API_KEY="dashscope-key",
        LLM_DASHSCOPE_API_BASE="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        LLM_DASHSCOPE_MODEL_ID="qwen-default",
        LLM_DASHSCOPE_CHAT_MODEL_ID="qwen-chat",
        LLM_DASHSCOPE_TITLE_MODEL_ID="qwen-title",
        LLM_DASHSCOPE_MEMORY_MODEL_ID="qwen-memory",
        LLM_DASHSCOPE_LEARNING_MODEL_ID="qwen-learning",
    )

    assert s.openai_api_key == "openai-key"
    assert s.openai_api_base == "https://api.openai.com/v1"
    assert s.openai_model_id == "gpt-5.1-mini"
    assert s.deepseek_api_key == "deepseek-key"
    assert s.deepseek_api_base == "https://api.deepseek.com"
    assert s.deepseek_model_id == "deepseek-chat"
    assert s.openrouter_api_key == "openrouter-key"
    assert s.openrouter_api_base == "https://openrouter.ai/api/v1"
    assert s.openrouter_model_id == "openai/gpt-4.1-mini"
    assert s.dashscope_api_key == "dashscope-key"
    assert s.dashscope_api_base == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert s.dashscope_model_id == "qwen-default"
    assert s.dashscope_chat_model_id == "qwen-chat"
    assert s.dashscope_title_model_id == "qwen-title"
    assert s.dashscope_memory_model_id == "qwen-memory"
    assert s.dashscope_learning_model_id == "qwen-learning"
