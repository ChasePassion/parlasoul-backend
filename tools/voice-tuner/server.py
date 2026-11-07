"""Local voice tuning console for the Qwen3-TTS realtime voices.

    python tools/voice-tuner/server.py --port 8099

浏览器打开 http://127.0.0.1:8099 即可调参数试听。页面覆盖百炼实时语音合成
``session.update`` 的全部字段，合成结果直接来自线上模型，和角色在通话里听到的
是同一条链路（``VoiceGatewayRepository``）。

工具只读：不修改角色、不写数据库。调完之后把页面输出的 JSON 拿去更新角色即可。
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import asyncpg  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from voice_catalog import VOICE_CATALOG  # noqa: E402

from src.core.config import settings  # noqa: E402
from src.core.exceptions import APIError  # noqa: E402
from src.core.tts_voice_catalog import dashscope_legacy_voice_catalog  # noqa: E402
from src.repositories.voice_gateway_repository import VoiceGatewayRepository  # noqa: E402

PAGE_PATH = Path(__file__).resolve().parent / "page.html"

DEFAULT_MODEL = settings.DASHSCOPE_TTS_MODEL or "qwen3-tts-flash-realtime"

# 只有这一个模型在当前 API Key 下可用；vc / instruct 两个型号连上去直接返回
# Model not exist，所以不放进选项，避免页面给人一种"能调"的错觉。
MODEL_OPTIONS: tuple[dict[str, str], ...] = (
    {
        "id": "qwen3-tts-flash-realtime",
        "label": "qwen3-tts-flash-realtime",
        "note": "线上正在用的模型。只认音色 + 数值参数，不支持自然语言指令。",
    },
)

LANGUAGE_OPTIONS: tuple[str, ...] = (
    "Auto",
    "Chinese",
    "English",
    "Japanese",
    "Korean",
    "French",
    "German",
    "Italian",
    "Portuguese",
    "Spanish",
    "Russian",
)

FORMAT_OPTIONS: tuple[dict[str, Any], ...] = (
    {"id": "mp3", "label": "mp3（推荐，浏览器直放）", "sample_rates": [24000]},
    {"id": "wav", "label": "wav（无损，体积大）", "sample_rates": [8000, 16000, 24000, 48000]},
    {"id": "pcm", "label": "pcm（裸流，需要手动封装）", "sample_rates": [8000, 16000, 24000, 48000]},
    {"id": "opus", "label": "opus（可调码率）", "sample_rates": [8000, 16000, 24000, 48000]},
)

DEFAULT_SAMPLE_TEXT = (
    "You finally came. I was starting to think I would have to find another reason "
    "to wait one more day."
)


def _database_url() -> str:
    """asyncpg 不认 SQLAlchemy 的 ``+asyncpg`` 驱动后缀。"""
    return settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


class PreviewRequest(BaseModel):
    text: str = Field(..., min_length=1)
    voice: str = Field(..., min_length=1)
    model: str | None = None
    speech_rate: float | None = Field(None, ge=0.5, le=2.0)
    volume: int | None = Field(None, ge=0, le=100)
    pitch_rate: float | None = Field(None, ge=0.5, le=2.0)
    language_type: str | None = None
    response_format: str = "mp3"
    sample_rate: int | None = None
    bit_rate: int | None = Field(None, ge=6, le=510)


def _session_overrides(req: PreviewRequest) -> dict[str, Any]:
    """把页面上的参数翻译成 ``session.update`` 里的 session 字段。

    ``sample_rate`` 必须显式下发：实测 pcm 格式不带该字段会被上游拒绝。
    """
    overrides: dict[str, Any] = {
        "sample_rate": req.sample_rate or 24000,
    }
    if req.speech_rate is not None:
        overrides["speech_rate"] = req.speech_rate
    if req.volume is not None:
        overrides["volume"] = req.volume
    if req.pitch_rate is not None:
        overrides["pitch_rate"] = req.pitch_rate
    if req.language_type and req.language_type != "Auto":
        overrides["language_type"] = req.language_type
    if req.response_format == "opus" and req.bit_rate is not None:
        overrides["bit_rate"] = req.bit_rate
    return overrides


def _media_type(audio_format: str, sample_rate: int | None) -> str:
    if audio_format == "mp3":
        return "audio/mpeg"
    if audio_format == "wav":
        return "audio/wav"
    if audio_format == "opus":
        return "audio/ogg; codecs=opus"
    return f"audio/pcm;rate={sample_rate or 24000}"


def create_app() -> FastAPI:
    gateway = VoiceGatewayRepository()
    pool_holder: dict[str, asyncpg.Pool | None] = {"pool": None}

    async def get_pool() -> asyncpg.Pool:
        pool = pool_holder["pool"]
        if pool is None:
            pool = await asyncpg.create_pool(_database_url(), min_size=1, max_size=4)
            pool_holder["pool"] = pool
        return pool

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if pool_holder["pool"] is not None:
            await pool_holder["pool"].close()

    app = FastAPI(
        title="ParlaSoul 音色调试台", docs_url=None, redoc_url=None, lifespan=lifespan
    )

    @app.get("/")
    async def page() -> FileResponse:
        return FileResponse(PAGE_PATH, media_type="text/html; charset=utf-8")

    @app.get("/api/meta")
    async def meta() -> dict[str, Any]:
        return {
            "default_model": DEFAULT_MODEL,
            "models": list(MODEL_OPTIONS),
            "languages": list(LANGUAGE_OPTIONS),
            "formats": list(FORMAT_OPTIONS),
            "default_text": DEFAULT_SAMPLE_TEXT,
            "api_key_configured": bool(settings.DASHSCOPE_API_KEY),
        }

    @app.get("/api/voices")
    async def voices() -> dict[str, Any]:
        """官方 48 个系统音色 + 项目历史音色 + 数据库里的克隆音色。"""
        catalog: list[dict[str, Any]] = []
        seen: set[str] = set()

        project_meta = {
            item.provider_voice_id: item for item in dashscope_legacy_voice_catalog()
        }
        for item in VOICE_CATALOG:
            project = project_meta.get(item.provider_voice_id)
            catalog.append(
                {
                    "voice_id": item.provider_voice_id,
                    "name": item.display_name,
                    "hint": (project.usage_hint if project and project.usage_hint else item.hint),
                    "language": item.language,
                    "source": "system",
                }
            )
            seen.add(item.provider_voice_id)

        for voice_id, item in project_meta.items():
            if voice_id in seen:
                continue
            catalog.append(
                {
                    "voice_id": voice_id,
                    "name": item.display_name,
                    "hint": item.usage_hint or "",
                    "language": item.language or "项目历史音色",
                    "source": "system",
                }
            )

        clones: list[dict[str, Any]] = []
        clone_error: str | None = None
        try:
            db = await get_pool()
            rows = await db.fetch(
                """
                select provider, provider_voice_id, display_name, provider_model, source_type
                from voice_profiles
                where status = 'ready'
                order by created_at desc
                limit 200
                """
            )
            clones = [
                {
                    "voice_id": row["provider_voice_id"],
                    "name": row["display_name"] or row["provider_voice_id"],
                    "hint": f"克隆音色 · {row['source_type']}",
                    "language": row["provider"] or "dashboard",
                    "model": row["provider_model"],
                    "source": "clone",
                }
                for row in rows
            ]
        except Exception as exc:  # 数据库不可用时仍允许纯手动调参
            clone_error = f"{type(exc).__name__}: {exc}"

        return {"catalog": catalog, "clones": clones, "clone_error": clone_error}

    @app.get("/api/characters")
    async def characters() -> dict[str, Any]:
        try:
            db = await get_pool()
            rows = await db.fetch(
                """
                select id::text as id, name, identifier,
                       voice_provider, voice_model, voice_provider_voice_id,
                       voice_source_type, voice_profile, updated_at
                from characters
                order by updated_at desc
                limit 200
                """
            )
        except Exception as exc:
            return {"characters": [], "error": f"{type(exc).__name__}: {exc}"}

        result: list[dict[str, Any]] = []
        for row in rows:
            profile = row["voice_profile"]
            if isinstance(profile, str):
                try:
                    profile = json.loads(profile)
                except json.JSONDecodeError:
                    profile = {}
            result.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "identifier": row["identifier"],
                    "voice_provider": row["voice_provider"],
                    "voice_model": row["voice_model"],
                    "voice_provider_voice_id": row["voice_provider_voice_id"],
                    "voice_source_type": row["voice_source_type"],
                    "voice_profile": profile or {},
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                }
            )
        return {"characters": result, "error": None}

    @app.post("/api/preview")
    async def preview(req: PreviewRequest) -> StreamingResponse:
        audio_format = req.response_format.strip().lower()
        if audio_format not in {"mp3", "wav", "pcm", "opus"}:
            raise HTTPException(status_code=422, detail=f"不支持的音频格式：{audio_format}")

        stream = gateway.stream_tts_audio_base64(
            text=req.text,
            audio_format=audio_format,
            provider_voice_id=req.voice,
            provider_model=req.model,
            voice_profile=None,
            session_overrides=_session_overrides(req),
        )

        try:
            first = await anext(stream)
        except StopAsyncIteration:
            raise HTTPException(status_code=502, detail="上游返回了空音频")
        except APIError as exc:
            return JSONResponse(
                {"code": exc.code, "message": exc.message}, status_code=exc.status_code
            )

        async def body():
            try:
                yield base64.b64decode(first[0])
                async for chunk, _ in stream:
                    yield base64.b64decode(chunk)
            except APIError as exc:
                # 响应头已经发出，只能在流里静默结束，错误信息由 /api/preview 的日志体现。
                print(f"[voice-tuner] 音频流中断: {exc.message}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"[voice-tuner] 音频流异常: {exc!r}", file=sys.stderr)

        return StreamingResponse(
            body(), media_type=_media_type(audio_format, req.sample_rate)
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="ParlaSoul 音色调试台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
