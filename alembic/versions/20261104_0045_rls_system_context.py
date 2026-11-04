"""Make user-scoped RLS policies tolerate the system execution context.

Background jobs set app.current_user_id to 'system' to read across users.
Several policies compared that value directly against a uuid column, so
PostgreSQL evaluated a cast of 'system' and raised invalid_text_representation.
The realtime telemetry policies already guard the cast; this migration applies
the same expression to every remaining policy.
"""

from __future__ import annotations

from alembic import op

revision = "20261104_0045"
down_revision = "20260814_0044"
branch_labels = None
depends_on = None

_SAFE_USER_ID = (
    "NULLIF(NULLIF(current_setting('app.current_user_id', true), ''), 'system')::uuid"
)
_IS_SYSTEM = "current_setting('app.current_user_id', true) = 'system'"

_LEGACY_USER_ID = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"


def _replace_policy(table: str, name: str, statement: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS {name} ON {table}")
    op.execute(statement)


def upgrade() -> None:
    _replace_policy(
        "characters",
        "characters_select_scope",
        f"""
        CREATE POLICY characters_select_scope ON characters
          FOR SELECT
          USING (
            {_IS_SYSTEM}
            OR visibility IN ('PUBLIC', 'PRIVATE')
            OR creator_id = {_SAFE_USER_ID}
          )
        """,
    )

    _replace_policy(
        "characters",
        "characters_modify_scope",
        f"""
        CREATE POLICY characters_modify_scope ON characters
          FOR ALL
          USING ({_IS_SYSTEM} OR creator_id = {_SAFE_USER_ID})
          WITH CHECK ({_IS_SYSTEM} OR creator_id = {_SAFE_USER_ID})
        """,
    )

    _replace_policy(
        "chats",
        "chats_scope",
        f"""
        CREATE POLICY chats_scope ON chats
          FOR ALL
          USING ({_IS_SYSTEM} OR user_id = {_SAFE_USER_ID})
          WITH CHECK ({_IS_SYSTEM} OR user_id = {_SAFE_USER_ID})
        """,
    )

    _replace_policy(
        "turns",
        "turns_scope",
        f"""
        CREATE POLICY turns_scope ON turns
          FOR ALL
          USING (
            {_IS_SYSTEM}
            OR EXISTS (
              SELECT 1 FROM chats c
              WHERE c.id = turns.chat_id
                AND c.user_id = {_SAFE_USER_ID}
            )
          )
          WITH CHECK (
            {_IS_SYSTEM}
            OR EXISTS (
              SELECT 1 FROM chats c
              WHERE c.id = turns.chat_id
                AND c.user_id = {_SAFE_USER_ID}
            )
          )
        """,
    )

    _replace_policy(
        "candidates",
        "candidates_scope",
        f"""
        CREATE POLICY candidates_scope ON candidates
          FOR ALL
          USING (
            {_IS_SYSTEM}
            OR EXISTS (
              SELECT 1 FROM turns t
              JOIN chats c ON c.id = t.chat_id
              WHERE t.id = candidates.turn_id
                AND c.user_id = {_SAFE_USER_ID}
            )
          )
          WITH CHECK (
            {_IS_SYSTEM}
            OR EXISTS (
              SELECT 1 FROM turns t
              JOIN chats c ON c.id = t.chat_id
              WHERE t.id = candidates.turn_id
                AND c.user_id = {_SAFE_USER_ID}
            )
          )
        """,
    )

    _replace_policy(
        "user_settings",
        "user_settings_scope",
        f"""
        CREATE POLICY user_settings_scope ON user_settings
          FOR ALL
          USING ({_IS_SYSTEM} OR user_id = {_SAFE_USER_ID})
          WITH CHECK ({_IS_SYSTEM} OR user_id = {_SAFE_USER_ID})
        """,
    )

    _replace_policy(
        "saved_items",
        "saved_items_scope",
        f"""
        CREATE POLICY saved_items_scope ON saved_items
          FOR ALL
          USING ({_IS_SYSTEM} OR user_id = {_SAFE_USER_ID})
          WITH CHECK ({_IS_SYSTEM} OR user_id = {_SAFE_USER_ID})
        """,
    )


def downgrade() -> None:
    _replace_policy(
        "characters",
        "characters_select_scope",
        f"""
        CREATE POLICY characters_select_scope ON characters
          FOR SELECT
          USING (
            {_IS_SYSTEM}
            OR visibility IN ('PUBLIC', 'PRIVATE')
            OR creator_id = {_LEGACY_USER_ID}
          )
        """,
    )

    _replace_policy(
        "characters",
        "characters_modify_scope",
        f"""
        CREATE POLICY characters_modify_scope ON characters
          FOR ALL
          USING ({_IS_SYSTEM} OR creator_id = {_LEGACY_USER_ID})
          WITH CHECK ({_IS_SYSTEM} OR creator_id = {_LEGACY_USER_ID})
        """,
    )

    _replace_policy(
        "chats",
        "chats_scope",
        f"""
        CREATE POLICY chats_scope ON chats
          FOR ALL
          USING ({_IS_SYSTEM} OR user_id = {_LEGACY_USER_ID})
          WITH CHECK ({_IS_SYSTEM} OR user_id = {_LEGACY_USER_ID})
        """,
    )

    _replace_policy(
        "turns",
        "turns_scope",
        f"""
        CREATE POLICY turns_scope ON turns
          FOR ALL
          USING (
            {_IS_SYSTEM}
            OR EXISTS (
              SELECT 1 FROM chats c
              WHERE c.id = turns.chat_id
                AND c.user_id = {_LEGACY_USER_ID}
            )
          )
          WITH CHECK (
            {_IS_SYSTEM}
            OR EXISTS (
              SELECT 1 FROM chats c
              WHERE c.id = turns.chat_id
                AND c.user_id = {_LEGACY_USER_ID}
            )
          )
        """,
    )

    _replace_policy(
        "candidates",
        "candidates_scope",
        f"""
        CREATE POLICY candidates_scope ON candidates
          FOR ALL
          USING (
            {_IS_SYSTEM}
            OR EXISTS (
              SELECT 1 FROM turns t
              JOIN chats c ON c.id = t.chat_id
              WHERE t.id = candidates.turn_id
                AND c.user_id = {_LEGACY_USER_ID}
            )
          )
          WITH CHECK (
            {_IS_SYSTEM}
            OR EXISTS (
              SELECT 1 FROM turns t
              JOIN chats c ON c.id = t.chat_id
              WHERE t.id = candidates.turn_id
                AND c.user_id = {_LEGACY_USER_ID}
            )
          )
        """,
    )

    _replace_policy(
        "user_settings",
        "user_settings_scope",
        f"""
        CREATE POLICY user_settings_scope ON user_settings
          FOR ALL
          USING ({_IS_SYSTEM} OR user_id = {_LEGACY_USER_ID})
          WITH CHECK ({_IS_SYSTEM} OR user_id = {_LEGACY_USER_ID})
        """,
    )

    _replace_policy(
        "saved_items",
        "saved_items_scope",
        f"""
        CREATE POLICY saved_items_scope ON saved_items
          FOR ALL
          USING ({_IS_SYSTEM} OR user_id = {_LEGACY_USER_ID})
          WITH CHECK ({_IS_SYSTEM} OR user_id = {_LEGACY_USER_ID})
        """,
    )
