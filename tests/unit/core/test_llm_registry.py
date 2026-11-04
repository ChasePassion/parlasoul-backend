from __future__ import annotations

from src.core.llm_registry import LLMRegistry
from src.core.llm_settings import LLMSettings


def test_dashscope_bound_routes_can_select_distinct_provider_model_aliases() -> None:
    settings = LLMSettings(
        _env_file=None,
        LLM_DASHSCOPE_API_KEY="dashscope-key",
        LLM_DASHSCOPE_API_BASE="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        LLM_DASHSCOPE_MODEL_ID="qwen-default",
        LLM_DASHSCOPE_TITLE_MODEL_ID="qwen-title",
        LLM_DASHSCOPE_MEMORY_MODEL_ID="qwen-memory",
        LLM_DASHSCOPE_LEARNING_MODEL_ID="qwen-learning",
        LLM_DASHSCOPE_VISION_MODEL_ID="qwen-vision",
    )
    registry = LLMRegistry(settings)

    title_route = registry.resolve_title_route()
    memory_route = registry.resolve_memory_route()
    learning_route = registry.resolve_learning_route()

    assert title_route is not None
    assert title_route.provider == "dashscope"
    assert title_route.model == "qwen-title"
    assert title_route.sdk_kind == "openai_sdk"
    assert title_route.api_base == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert title_route.source == "system_title"
    assert title_route.model_type_label == "dashscope:title:qwen-title"

    assert memory_route is not None
    assert memory_route.provider == "dashscope"
    assert memory_route.model == "qwen-memory"
    assert memory_route.source == "system_memory"
    assert memory_route.model_type_label == "dashscope:memory:qwen-memory"

    assert learning_route is not None
    assert learning_route.provider == "dashscope"
    assert learning_route.model == "qwen-learning"
    assert learning_route.source == "system_learning"
    assert learning_route.model_type_label == "dashscope:learning:qwen-learning"

    assert set(registry.selectable_models("dashscope")) == {
        "qwen-default",
        "qwen-title",
        "qwen-memory",
        "qwen-learning",
        "qwen-vision",
    }


def test_dashscope_aliases_fall_back_to_default_model_when_role_model_is_not_set() -> None:
    settings = LLMSettings(
        _env_file=None,
        LLM_DASHSCOPE_API_KEY="dashscope-key",
        LLM_DASHSCOPE_MODEL_ID="qwen-default",
    )
    registry = LLMRegistry(settings)

    title_route = registry.resolve_bound_route("dashscope:title", source="system_title")

    assert title_route is not None
    assert title_route.provider == "dashscope"
    assert title_route.model == "qwen-default"
    assert title_route.model_type_label == "dashscope:title:qwen-default"


def test_glm_provider_is_not_registered() -> None:
    settings = LLMSettings(_env_file=None)
    registry = LLMRegistry(settings)

    assert registry.get_profile("glm") is None
    assert registry.resolve_bound_route("glm", source="system_title") is None
    assert "glm" not in {profile.provider for profile in registry.enabled_profiles()}
