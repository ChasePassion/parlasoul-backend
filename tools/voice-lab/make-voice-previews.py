"""Generate the system-voice preview MP3s with the live Bailian TTS.

The voice picker serves one static MP3 per system voice.  The previous assets
were MiniMax samples; after moving system synthesis to Bailian the previews
must come from the same provider so the sample matches the voice the user
hears in a call.

    LANGFUSE_ENABLED=false python tools/voice-lab/make-voice-previews.py

Existing files are kept unless ``--force`` is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.tts_voice_catalog import dashscope_legacy_voice_catalog  # noqa: E402
from src.services.tts_gateway_service import TTSGatewayService  # noqa: E402

PREVIEW_TEXT = "你好呀，我是你的专属声音。今天过得怎么样，想聊点什么？"
PREVIEW_INSTRUCTIONS = "用自然、口语化、亲切的语气说话，像面对面聊天。"


async def _synthesize(*, voice_id: str, output: Path) -> None:
    service = TTSGatewayService()
    chunks: list[bytes] = []
    async for audio_b64, mime_type in service.stream_tts_audio_base64(
        text=PREVIEW_TEXT,
        audio_format="mp3",
        instructions=PREVIEW_INSTRUCTIONS,
        provider="dashscope",
        provider_voice_id=voice_id,
        provider_model=None,
        voice_source_type="system",
    ):
        if mime_type != "audio/mpeg":
            raise RuntimeError(f"unexpected mime type for {voice_id}: {mime_type}")
        chunks.append(base64.b64decode(audio_b64))
    payload = b"".join(chunks)
    if not payload:
        raise RuntimeError(f"empty preview audio for {voice_id}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)
    print(f"wrote {output} ({len(payload)} bytes)")


async def _run(*, force: bool) -> int:
    output_dir = REPO_ROOT / "static" / "media" / "voices" / "preview" / "dashscope"
    for item in dashscope_legacy_voice_catalog():
        output = output_dir / f"{item.provider_voice_id}.mp3"
        if output.exists() and not force:
            print(f"skip {output}")
            continue
        await _synthesize(voice_id=item.provider_voice_id, output=output)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="regenerate existing files")
    args = parser.parse_args()
    return asyncio.run(_run(force=args.force))


if __name__ == "__main__":
    raise SystemExit(main())
