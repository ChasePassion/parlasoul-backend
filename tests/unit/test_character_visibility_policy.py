from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _assert_public_and_private_are_selectable(sql: str) -> None:
    assert re.search(
        r"visibility\s+IN\s*\(\s*'PUBLIC'\s*,\s*'PRIVATE'\s*\)",
        sql,
    ), "characters_select_scope must allow direct-link reads for PUBLIC and PRIVATE characters"


def test_current_character_rls_policy_allows_shareable_private_characters() -> None:
    migration_sql = (
        REPO_ROOT / "alembic" / "sql" / "2026-02-18_row_level_security.sql"
    ).read_text(encoding="utf-8")

    _assert_public_and_private_are_selectable(migration_sql)


def test_unlisted_visibility_migration_updates_character_rls_policy_for_sharing() -> None:
    revision_source = (
        REPO_ROOT / "alembic" / "versions" / "20260501_0034_remove_unlisted_visibility.py"
    ).read_text(encoding="utf-8")
    upgrade_source = revision_source.split("def downgrade", maxsplit=1)[0]

    _assert_public_and_private_are_selectable(upgrade_source)
