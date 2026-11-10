import httpx
import openai
import pytest
from pydantic_ai.usage import RequestUsage

from src.memory_system.clients.llm import LLMClient, _ModelRoute
from src.memory_system.exceptions import LLMAuthenticationError


def test_chat_completion_request_kwargs_disable_reasoning_for_openrouter() -> None:
    route = _ModelRoute(model="qwen/qwen3.5-flash-02-23", base_url="https://openrouter.ai/api/v1")
    messages = [{"role": "user", "content": "ping"}]

    kwargs = LLMClient._chat_completion_request_kwargs(route, messages, stream=True)

    assert kwargs["model"] == route.model
    assert kwargs["messages"] == messages
    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}
    assert kwargs["extra_body"] == {"reasoning": {"effort": "none"}}


def test_chat_completion_request_kwargs_leave_non_openrouter_unchanged() -> None:
    route = _ModelRoute(model="deepseek-chat", base_url="https://api.deepseek.com")
    messages = [{"role": "user", "content": "ping"}]

    kwargs = LLMClient._chat_completion_request_kwargs(route, messages, stream=False)

    assert kwargs["model"] == route.model
    assert kwargs["messages"] == messages
    assert kwargs["stream"] is False
    assert "stream_options" not in kwargs
    assert "extra_body" not in kwargs


def test_chat_completion_request_kwargs_disable_thinking_for_dashscope() -> None:
    route = _ModelRoute(
        model="qwen3.5-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    messages = [{"role": "user", "content": "ping"}]

    kwargs = LLMClient._chat_completion_request_kwargs(route, messages, stream=False)

    assert kwargs["model"] == route.model
    assert kwargs["messages"] == messages
    assert kwargs["stream"] is False
    assert kwargs["extra_body"] == {"enable_thinking": False}


def test_normalize_terminal_error_wraps_dashscope_authentication_error() -> None:
    route = _ModelRoute(
        model="qwen3.5-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    response = httpx.Response(
        status_code=401,
        request=httpx.Request(
            "POST",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        ),
    )
    error = openai.AuthenticationError(
        "Error code: 401",
        response=response,
        body={
            "error": {
                "type": "authorized_error",
                "message": "login fail: missing token",
            }
        },
    )

    normalized = LLMClient._normalize_terminal_error(error, route)

    assert isinstance(normalized, LLMAuthenticationError)
    assert normalized.provider == "dashscope"
    assert normalized.model == "qwen3.5-flash"
    assert normalized.detail == "login fail: missing token"


def test_extract_usage_details_supports_pydantic_ai_usage_names() -> None:
    usage = RequestUsage(
        input_tokens=12,
        output_tokens=5,
        cache_read_tokens=3,
        cache_write_tokens=2,
    )

    assert LLMClient._extract_usage_details(usage) == {
        "input": 12,
        "output": 5,
        "total": 20,
        "cache_read_input_tokens": 3,
        "cache_write_input_tokens": 2,
    }


@pytest.mark.parametrize(
    ("response", "default", "expected"),
    [
        (
            '<think>\nI should think first.\n</think>\n\n'
            '{"feedback_card":{"surface":"Hello","zh":"你好","key_phrases":[]}}',
            {"feedback_card": {}},
            {"feedback_card": {"surface": "Hello", "zh": "你好", "key_phrases": []}},
        ),
        (
            'Here is the JSON you asked for:\n'
            '{"reply_card":{"surface":"Hi","zh":"你好","key_phrases":[]}}\n'
            "Have a nice day.",
            {"reply_card": {}},
            {"reply_card": {"surface": "Hi", "zh": "你好", "key_phrases": []}},
        ),
    ],
    ids=["after_think_block", "from_prose_response"],
)
def test_safe_parse_json_extracts_object(
    response: str, default: dict, expected: dict
) -> None:
    client = LLMClient(primary_model="demo-model", primary_api_key="test-key")

    assert client._safe_parse_json(response, default=default) == expected
