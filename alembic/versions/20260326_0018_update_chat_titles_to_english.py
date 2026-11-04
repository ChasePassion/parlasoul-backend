from __future__ import annotations

from alembic import op

revision = "20260326_0018"
down_revision = "20260324_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "UPDATE chats SET title = 'New Chat' WHERE title = '新聊天';"
    )
    bind.exec_driver_sql(
        "UPDATE chats SET title = 'New Chat' WHERE title IS NULL OR btrim(title) = '';"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "UPDATE chats SET title = '新聊天' WHERE title = 'New Chat';"
    )
