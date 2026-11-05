# ruff: noqa: E402

import os
import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# python-dotenv treats an active tracer (coverage, debuggers) as a REPL and
# resolves .env from the process cwd. Importing pymilvus calls load_dotenv(),
# which otherwise injects the repository's real .env into os.environ and makes
# test outcomes depend on ambient configuration.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["LANGFUSE_ENABLED"] = "false"
os.environ["OTEL_SDK_DISABLED"] = "true"

from src.api.exceptions import register_exception_handlers
from src.api.response import success_response
from src.core.config import settings
from src.core.exceptions import APIError
from src.core.llm_settings import llm_settings

_LLM_RAW_FIELDS = (
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "OPENAI_MODEL_ID",
    "LLM_DEEPSEEK_API_KEY",
    "LLM_DEEPSEEK_API_BASE",
    "LLM_DEEPSEEK_MODEL_ID",
    "LLM_OPENROUTER_API_KEY",
    "LLM_OPENROUTER_API_BASE",
    "LLM_OPENROUTER_MODEL_ID",
    "LLM_DASHSCOPE_API_KEY",
    "LLM_DASHSCOPE_API_BASE",
    "LLM_DASHSCOPE_MODEL_ID",
    "LLM_DASHSCOPE_CHAT_MODEL_ID",
    "LLM_DASHSCOPE_TITLE_MODEL_ID",
    "LLM_DASHSCOPE_MEMORY_MODEL_ID",
    "LLM_DASHSCOPE_LEARNING_MODEL_ID",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL_ID",
)

_LLM_CACHE_FIELDS = (
    "openai_api_key",
    "openai_api_base",
    "openai_model_id",
    "deepseek_api_key",
    "deepseek_api_base",
    "deepseek_model_id",
    "openrouter_api_key",
    "openrouter_api_base",
    "openrouter_model_id",
    "dashscope_api_key",
    "dashscope_api_base",
    "dashscope_model_id",
    "dashscope_chat_model_id",
    "dashscope_title_model_id",
    "dashscope_memory_model_id",
    "dashscope_learning_model_id",
    "anthropic_api_key",
    "anthropic_api_base",
    "anthropic_model_id",
)


class ValidationPayload(BaseModel):
    name: str


def create_contract_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/health")
    async def health() -> object:
        return success_response({"status": "ok"}, status_code=200)

    @app.get("/boom")
    async def boom() -> object:
        raise APIError(
            status_code=409,
            code="conflict_demo",
            message="resource conflict: demo conflict",
        )

    @app.post("/validate")
    async def validate(payload: ValidationPayload) -> object:
        return success_response(payload.model_dump(), status_code=200)

    return app


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    with TestClient(create_contract_app()) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def isolate_llm_settings() -> Generator[None, None, None]:
    raw_snapshot = {field: getattr(llm_settings, field) for field in _LLM_RAW_FIELDS}
    for field in _LLM_CACHE_FIELDS:
        llm_settings.__dict__.pop(field, None)
    try:
        yield
    finally:
        for field, value in raw_snapshot.items():
            setattr(llm_settings, field, value)
        for field in _LLM_CACHE_FIELDS:
            llm_settings.__dict__.pop(field, None)


@pytest.fixture(autouse=True)
def isolate_observability_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LANGFUSE_ENABLED", False, raising=False)
