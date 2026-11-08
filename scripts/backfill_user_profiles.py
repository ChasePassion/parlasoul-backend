"""Backfill default username and preset avatar for users created before registration defaults.

Registration now assigns a default username and a preset avatar derived from
the email address (see the frontend auth hooks). Existing rows keep NULL in
those columns, so this script applies the same rules to them.

Idempotent: a user is only touched when the username is empty, or when there is
no uploaded avatar and the stored avatar_url is not a preset avatar.
"""

import asyncio
import re
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.db.session import engine  # noqa: E402

PRESET_AVATAR_PREFIX = "/avatars/default/"
PRESET_AVATAR_COUNT = 10
USERNAME_MAX_LENGTH = 45


def resolve_preset_avatar_url(email: str) -> str:
    """Mirror resolvePresetAvatarUrl() in the frontend, including 16-bit code units."""
    code_units = email.strip().lower().encode("utf-16-le")
    hash_value = 0
    for index in range(0, len(code_units), 2):
        code_unit = code_units[index] | (code_units[index + 1] << 8)
        hash_value = (hash_value * 31 + code_unit) & 0xFFFFFFFF
    return (
        f"{PRESET_AVATAR_PREFIX}avatar-{hash_value % PRESET_AVATAR_COUNT + 1:02d}.webp"
    )


def normalize_username_base(email: str) -> str:
    local_part = email.split("@")[0] or ""
    without_tag = local_part.split("+")[0] or ""
    cleaned = re.sub(r"[^a-z0-9._\-\u4e00-\u9fff]", "", without_tag.lower())
    return cleaned[:USERNAME_MAX_LENGTH].ljust(2, "_")


def is_preset_avatar(value: str | None) -> bool:
    return bool(value and value.startswith(PRESET_AVATAR_PREFIX))


async def pick_username(conn, base: str, taken: set[str]) -> str:
    for suffix in ("", "2", "3", "4"):
        candidate = f"{base}{suffix}"
        if candidate in taken:
            continue
        exists = await conn.scalar(
            text("SELECT 1 FROM users WHERE username = :username LIMIT 1"),
            {"username": candidate},
        )
        if not exists:
            return candidate
    for index in range(5, 1000):
        candidate = f"{base}{index}"
        if candidate in taken:
            continue
        exists = await conn.scalar(
            text("SELECT 1 FROM users WHERE username = :username LIMIT 1"),
            {"username": candidate},
        )
        if not exists:
            return candidate
    raise RuntimeError(f"cannot find a free username for base {base!r}")


async def main() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.current_user_id', 'system', true)"))

        rows = (
            await conn.execute(
                text(
                    """
                    SELECT id, email, username, avatar_url, avatar_image_key
                    FROM users
                    ORDER BY created_at
                    """
                )
            )
        ).fetchall()

        taken = {row.username for row in rows if row.username}
        updated = 0

        for row in rows:
            username = (row.username or "").strip() or None
            avatar_url = row.avatar_url
            changes: dict[str, str] = {}

            if username is None:
                username = await pick_username(conn, normalize_username_base(row.email), taken)
                taken.add(username)
                changes["username"] = username

            if not row.avatar_image_key and not is_preset_avatar(avatar_url):
                avatar_url = resolve_preset_avatar_url(row.email)
                changes["avatar_url"] = avatar_url

            if not changes:
                continue

            await conn.execute(
                text(
                    """
                    UPDATE users
                    SET username = :username, avatar_url = :avatar_url, updated_at = now()
                    WHERE id = :user_id
                    """
                ),
                {
                    "username": username,
                    "avatar_url": avatar_url,
                    "user_id": row.id,
                },
            )
            updated += 1
            print(f"updated {row.id}: username={username} avatar={avatar_url}")

    print(f"done: {updated} of {len(rows)} users updated")


if __name__ == "__main__":
    asyncio.run(main())
