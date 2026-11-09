"""Qwen-Image text-to-image / image-to-image client for character avatars."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.core.config import settings
from src.core.exceptions import APIError
from src.core.upstream_errors import parse_upstream_error

logger = logging.getLogger(__name__)


def build_image_payload(
    *,
    model: str,
    size: str,
    prompt: str,
    reference_images: list[str] | None,
) -> dict[str, Any]:
    """Build the DashScope multimodal-generation request body.

    Text-to-image uses Agent Prompt Enhancement; image-to-image must not,
    because ``prompt_extend_mode=agent`` is rejected with 400 when input
    images are present. Reference images are public URLs or inline
    ``data:{mime};base64,...`` strings, ordered before the text instruction.
    """
    content: list[dict[str, str]] = [
        {"image": image} for image in (reference_images or [])
    ]
    content.append({"text": prompt})
    parameters: dict[str, Any] = {
        "size": size,
        "n": 1,
        "prompt_extend": True,
        "watermark": False,
    }
    if not reference_images:
        # Agent Prompt Enhancement and thinking mode are text-to-image only.
        parameters["prompt_extend_mode"] = "agent"
        parameters["enable_thinking"] = True
    return {
        "model": model,
        "input": {
            "messages": [{"role": "user", "content": content}],
        },
        "parameters": parameters,
    }


class ImageGenerationRepository:
    """Generate one square portrait with the highest available quality settings.

    The forge always requests the maximum quality configuration exposed by the
    model: 2048x2048 output, thinking mode and no watermark. Text-to-image
    additionally enables agent prompt rewriting. There is intentionally no
    lower-quality path.
    """

    def __init__(self) -> None:
        self._api_key = settings.DASHSCOPE_API_KEY
        self._url = settings.DASHSCOPE_IMAGE_API_URL
        self._model = settings.DASHSCOPE_IMAGE_MODEL
        self._size = settings.DASHSCOPE_IMAGE_SIZE
        self._timeout = settings.DASHSCOPE_IMAGE_TIMEOUT_SECONDS

    def _require_key(self) -> str:
        if not self._api_key or not self._api_key.strip():
            raise APIError(
                status_code=503,
                code="external_error",
                message="dependency unavailable: DASHSCOPE_API_KEY is not configured",
            )
        return self._api_key.strip()

    async def generate(
        self, *, prompt: str, reference_images: list[str] | None = None
    ) -> bytes:
        payload = build_image_payload(
            model=self._model,
            size=self._size,
            prompt=prompt,
            reference_images=reference_images,
        )

        async with httpx.AsyncClient(
            timeout=self._timeout, trust_env=False
        ) as client:
            try:
                response = await client.post(
                    self._url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self._require_key()}",
                        "Content-Type": "application/json",
                    },
                )
            except httpx.TimeoutException as exc:
                raise APIError(
                    status_code=504,
                    code="external_timeout",
                    message="upstream error: image generation timed out",
                ) from exc
            except httpx.HTTPError as exc:
                logger.exception("image generation request failed: %s", exc)
                raise APIError(
                    status_code=502,
                    code="external_error",
                    message="upstream error: image generation request failed",
                ) from exc

            if response.status_code != 200:
                upstream_code, upstream_message = parse_upstream_error(response.text)
                logger.warning(
                    "image generation failed: status=%s upstream_code=%s body=%s",
                    response.status_code,
                    upstream_code,
                    response.text[:300],
                )
                raise APIError(
                    status_code=502,
                    code="external_error",
                    message=(
                        "upstream error: image generation failed; "
                        f"status={response.status_code}"
                    ),
                    upstream_code=upstream_code,
                    upstream_message=upstream_message,
                )

            try:
                data = response.json()
            except ValueError as exc:
                raise APIError(
                    status_code=502,
                    code="external_error",
                    message="upstream error: image generation returned invalid json",
                ) from exc

            image_url = self._extract_image_url(data)
            if not image_url:
                raise APIError(
                    status_code=502,
                    code="external_error",
                    message="upstream error: image generation returned no image",
                )

            try:
                image_response = await client.get(image_url)
                image_response.raise_for_status()
            except httpx.HTTPError as exc:
                logger.exception("image download failed: %s", exc)
                raise APIError(
                    status_code=502,
                    code="external_error",
                    message="upstream error: image download failed",
                ) from exc
            return image_response.content

    @staticmethod
    def _extract_image_url(data: dict[str, Any]) -> str | None:
        output = data.get("output")
        if not isinstance(output, dict):
            return None
        choices = output.get("choices")
        if not isinstance(choices, list):
            return None
        for choice in choices:
            message = choice.get("message") if isinstance(choice, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("image"), str):
                    return part["image"]
        return None
