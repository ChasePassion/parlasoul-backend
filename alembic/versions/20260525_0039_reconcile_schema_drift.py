"""Reconcile schema drift: enum cleanup, legacy column drop, RLS bypass.

Revision ID: 20260525_0039
Revises: 20260515_0038
"""

from __future__ import annotations

from alembic import op

revision = "20260525_0039"
down_revision = "20260515_0038"
branch_labels = None
depends_on = None


def _drop_all_policies() -> None:
    """Drop all existing RLS policies so enum types can be altered."""
    policies = [
        ("characters_select_scope", "characters"),
        ("characters_modify_scope", "characters"),
        ("chats_scope", "chats"),
        ("turns_scope", "turns"),
        ("candidates_scope", "candidates"),
        ("user_settings_scope", "user_settings"),
        ("saved_items_scope", "saved_items"),
    ]
    for policy, table in policies:
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")


def upgrade() -> None:
    # ---- 1. Drop ALL RLS policies first (enum change requires it) ----
    _drop_all_policies()

    # ---- 2. Drop legacy column ----
    op.drop_column("characters", "interaction_count")

    # ---- 3. Recreate visibility_t without UNLISTED ----
    op.execute("ALTER TYPE visibility_t RENAME TO visibility_t_old")
    op.execute("CREATE TYPE visibility_t AS ENUM ('PUBLIC', 'PRIVATE')")
    op.execute(
        "ALTER TABLE characters ALTER COLUMN visibility DROP DEFAULT"
    )
    op.execute(
        "ALTER TABLE characters ALTER COLUMN visibility "
        "TYPE visibility_t USING visibility::text::visibility_t"
    )
    op.execute(
        "ALTER TABLE characters ALTER COLUMN visibility "
        "SET DEFAULT 'PRIVATE'::visibility_t"
    )
    op.execute("DROP TYPE visibility_t_old")

    # ---- 4. Recreate chat_visibility_t without UNLISTED ----
    op.execute("ALTER TYPE chat_visibility_t RENAME TO chat_visibility_t_old")
    op.execute("CREATE TYPE chat_visibility_t AS ENUM ('PUBLIC', 'PRIVATE')")
    op.execute(
        "ALTER TABLE chats ALTER COLUMN visibility DROP DEFAULT"
    )
    op.execute(
        "ALTER TABLE chats ALTER COLUMN visibility "
        "TYPE chat_visibility_t USING visibility::text::chat_visibility_t"
    )
    op.execute(
        "ALTER TABLE chats ALTER COLUMN visibility "
        "SET DEFAULT 'PRIVATE'::chat_visibility_t"
    )
    op.execute("DROP TYPE chat_visibility_t_old")

    # ---- 5. Re-create RLS policies with system-role bypass ----
    _apply_rls_bypass()


def downgrade() -> None:
    # Not safely reversible.
    pass


def _apply_rls_bypass() -> None:
    """Re-create RLS policies with system-role bypass.

    The system role (set_config('app.current_user_id', 'system', true))
    is used by background jobs that need cross-user data access.
    """

    # -- characters: SELECT --
    op.execute("DROP POLICY IF EXISTS characters_select_scope ON characters")
    op.execute("""
        CREATE POLICY characters_select_scope ON characters
          FOR SELECT
          USING (
            current_setting('app.current_user_id', true) = 'system'
            OR visibility IN ('PUBLIC', 'PRIVATE')
            OR creator_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
          )
    """)

    # -- characters: ALL (modify) --
    op.execute("DROP POLICY IF EXISTS characters_modify_scope ON characters")
    op.execute("""
        CREATE POLICY characters_modify_scope ON characters
          FOR ALL
          USING (
            current_setting('app.current_user_id', true) = 'system'
            OR creator_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
          )
          WITH CHECK (
            current_setting('app.current_user_id', true) = 'system'
            OR creator_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
          )
    """)

    # -- chats --
    op.execute("DROP POLICY IF EXISTS chats_scope ON chats")
    op.execute("""
        CREATE POLICY chats_scope ON chats
          FOR ALL
          USING (
            current_setting('app.current_user_id', true) = 'system'
            OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
          )
          WITH CHECK (
            current_setting('app.current_user_id', true) = 'system'
            OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
          )
    """)

    # -- turns --
    op.execute("DROP POLICY IF EXISTS turns_scope ON turns")
    op.execute("""
        CREATE POLICY turns_scope ON turns
          FOR ALL
          USING (
            current_setting('app.current_user_id', true) = 'system'
            OR EXISTS (
              SELECT 1 FROM chats c
              WHERE c.id = turns.chat_id
                AND c.user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
            )
          )
          WITH CHECK (
            current_setting('app.current_user_id', true) = 'system'
            OR EXISTS (
              SELECT 1 FROM chats c
              WHERE c.id = turns.chat_id
                AND c.user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
            )
          )
    """)

    # -- candidates --
    op.execute("DROP POLICY IF EXISTS candidates_scope ON candidates")
    op.execute("""
        CREATE POLICY candidates_scope ON candidates
          FOR ALL
          USING (
            current_setting('app.current_user_id', true) = 'system'
            OR EXISTS (
              SELECT 1 FROM turns t
              JOIN chats c ON c.id = t.chat_id
              WHERE t.id = candidates.turn_id
                AND c.user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
            )
          )
          WITH CHECK (
            current_setting('app.current_user_id', true) = 'system'
            OR EXISTS (
              SELECT 1 FROM turns t
              JOIN chats c ON c.id = t.chat_id
              WHERE t.id = candidates.turn_id
                AND c.user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
            )
          )
    """)

    # -- user_settings --
    op.execute("DROP POLICY IF EXISTS user_settings_scope ON user_settings")
    op.execute("""
        CREATE POLICY user_settings_scope ON user_settings
          FOR ALL
          USING (
            current_setting('app.current_user_id', true) = 'system'
            OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
          )
          WITH CHECK (
            current_setting('app.current_user_id', true) = 'system'
            OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
          )
    """)

    # -- saved_items --
    op.execute("DROP POLICY IF EXISTS saved_items_scope ON saved_items")
    op.execute("""
        CREATE POLICY saved_items_scope ON saved_items
          FOR ALL
          USING (
            current_setting('app.current_user_id', true) = 'system'
            OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
          )
          WITH CHECK (
            current_setting('app.current_user_id', true) = 'system'
            OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
          )
    """)
