"""Batch upload system voice avatars to R2.

Reads PNG images from E:\role-avatar, converts to AVIF (96/192/512),
uploads to R2, and prints the avatar_image_key mapping for each voice.

Usage:
    python scripts/upload_system_voice_avatars.py [--dry-run]
"""

from __future__ import annotations

import io
import json
import os
import sys
import uuid
from pathlib import Path

import boto3
from dotenv import load_dotenv
from PIL import Image, ImageOps

SOURCE_DIR = Path(r"E:\role-avatar")
AVATAR_SIZES = [96, 192, 512]
AVIF_QUALITY = 55
SYSTEM_OWNER_SEGMENT = "system"

# provider_voice_id -> image filename (for filenames that differ from voice id)
# Most match directly; only "Eldric Sage" has a space in provider_voice_id.
VOICE_ID_TO_FILENAME: dict[str, str] = {
    "Cherry": "cherry",
    "Serena": "serena",
    "Ethan": "ethan",
    "Chelsie": "chelsie",
    "Momo": "momo",
    "Vivian": "vivian",
    "Moon": "moon",
    "Maia": "maia",
    "Kai": "kai",
    "Nofish": "nofish",
    "Bella": "bella",
    "Eldric Sage": "eldric sage",
    "Mia": "mia",
    "Mochi": "mochi",
    "Bellona": "bellona",
    "Vincent": "vincent",
    "Bunny": "bunny",
    "Neil": "neil",
    "Elias": "elias",
    "Arthur": "arthur",
    "Nini": "nini",
    "Seren": "seren",
    "Pip": "pip",
    "Stella": "stella",
}

load_dotenv()


def convert_to_avif_variants(src_path: Path) -> dict[int, bytes]:
    """Convert a PNG to multiple AVIF size variants."""
    img = Image.open(src_path)
    img = ImageOps.exif_transpose(img)
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    variants: dict[int, bytes] = {}
    for size in AVATAR_SIZES:
        resized = ImageOps.fit(img, (size, size), Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        buf = io.BytesIO()
        resized.save(buf, format="AVIF", quality=AVIF_QUALITY)
        variants[size] = buf.getvalue()
    return variants


def create_r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def upload_to_r2(client, key: str, data: bytes) -> None:
    client.put_object(
        Bucket=os.environ["R2_BUCKET_NAME"],
        Key=key,
        Body=data,
        ContentType="image/avif",
        CacheControl="public, max-age=31536000, immutable",
    )


def main(dry_run: bool = False) -> None:
    mapping: dict[str, str] = {}  # provider_voice_id -> avatar_image_key
    r2_client = create_r2_client() if not dry_run else None

    for voice_id, filename in VOICE_ID_TO_FILENAME.items():
        img_path = SOURCE_DIR / f"{filename}.png"
        if not img_path.exists():
            print(f"  MISSING {img_path}")
            continue

        image_id = uuid.uuid4().hex
        image_key = f"images/avatars/voices/{SYSTEM_OWNER_SEGMENT}/{image_id}"
        mapping[voice_id] = image_key

        print(f"  {voice_id:20s} -> {image_key}")

        if dry_run:
            continue

        variants = convert_to_avif_variants(img_path)
        for size, data in variants.items():
            r2_key = f"{image_key}/{size}.avif"
            upload_to_r2(r2_client, r2_key, data)
            print(f"    Uploaded {r2_key} ({len(data)} bytes)")

    # Print the mapping as JSON for use in code
    print("\n=== avatar_image_key mapping (JSON) ===")
    print(json.dumps(mapping, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("=== DRY RUN ===")
    main(dry_run=dry_run)
