"""phase10_wechat_one_time_payments

Revision ID: ef0c2ae315b0
Revises: 20260410_0024
Create Date: 2026-04-12 23:38:58.418532
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "ef0c2ae315b0"
down_revision = "20260410_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_webhook_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("webhook_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("webhook_timestamp", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("customer_id", sa.Text(), nullable=True),
        sa.Column("payment_id", sa.Text(), nullable=True),
        sa.Column("refund_id", sa.Text(), nullable=True),
        sa.Column("dodo_product_id", sa.Text(), nullable=True),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "processing_status",
            sa.String(length=32),
            server_default="received",
            nullable=False,
        ),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("webhook_id", name="payment_webhook_events_webhook_id_uniq"),
    )
    op.create_index(
        "payment_webhook_events_processing_status_created_at_idx",
        "payment_webhook_events",
        ["processing_status", "created_at"],
        unique=False,
    )

    op.create_table(
        "payment_orders",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(length=32), server_default="dodo", nullable=False),
        sa.Column("channel", sa.String(length=32), server_default="wechat_pay", nullable=False),
        sa.Column(
            "order_kind",
            sa.String(length=32),
            server_default="one_time_pass",
            nullable=False,
        ),
        sa.Column("dodo_product_id", sa.Text(), nullable=False),
        sa.Column("tier", sa.String(length=20), nullable=False),
        sa.Column("duration_days", sa.BigInteger(), nullable=False),
        sa.Column("base_price_minor", sa.BigInteger(), nullable=True),
        sa.Column("base_price_currency", sa.String(length=8), nullable=True),
        sa.Column("billing_currency", sa.String(length=8), server_default="CNY", nullable=False),
        sa.Column("dodo_customer_id", sa.Text(), nullable=True),
        sa.Column("dodo_checkout_session_id", sa.Text(), nullable=True),
        sa.Column("dodo_payment_id", sa.Text(), nullable=True),
        sa.Column("dodo_refund_id", sa.Text(), nullable=True),
        sa.Column("checkout_url", sa.Text(), nullable=True),
        sa.Column("charged_total_minor", sa.BigInteger(), nullable=True),
        sa.Column("charged_currency", sa.String(length=8), nullable=True),
        sa.Column("settlement_total_minor", sa.BigInteger(), nullable=True),
        sa.Column("settlement_currency", sa.String(length=8), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="created", nullable=False),
        sa.Column("paid_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("checkout_payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_event_payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("provider IN ('dodo')", name="payment_orders_provider_check"),
        sa.CheckConstraint("channel IN ('wechat_pay')", name="payment_orders_channel_check"),
        sa.CheckConstraint("order_kind IN ('one_time_pass')", name="payment_orders_kind_check"),
        sa.CheckConstraint("tier IN ('plus', 'pro')", name="payment_orders_tier_check"),
        sa.CheckConstraint(
            "status IN ('created', 'checkout_created', 'pending', 'succeeded', 'failed', "
            "'cancelled', 'refunded', 'expired')",
            name="payment_orders_status_check",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dodo_checkout_session_id", name="payment_orders_dodo_session_uniq"),
        sa.UniqueConstraint("dodo_payment_id", name="payment_orders_dodo_payment_uniq"),
    )
    op.create_index(
        "payment_orders_user_created_at_idx",
        "payment_orders",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "payment_orders_user_status_idx",
        "payment_orders",
        ["user_id", "status"],
        unique=False,
    )

    op.create_table(
        "user_access_passes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("source_order_id", sa.UUID(), nullable=False),
        sa.Column(
            "source_type",
            sa.String(length=32),
            server_default="one_time_payment",
            nullable=False,
        ),
        sa.Column("channel", sa.String(length=32), server_default="wechat_pay", nullable=False),
        sa.Column("dodo_product_id", sa.Text(), nullable=False),
        sa.Column("tier", sa.String(length=20), nullable=False),
        sa.Column("starts_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ends_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_type IN ('one_time_payment')",
            name="user_access_passes_source_type_check",
        ),
        sa.CheckConstraint("channel IN ('wechat_pay')", name="user_access_passes_channel_check"),
        sa.CheckConstraint("tier IN ('plus', 'pro')", name="user_access_passes_tier_check"),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'refunded', 'revoked')",
            name="user_access_passes_status_check",
        ),
        sa.ForeignKeyConstraint(["source_order_id"], ["payment_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_order_id", name="user_access_passes_source_order_uniq"),
    )
    op.create_index(
        "user_access_passes_user_ends_at_idx",
        "user_access_passes",
        ["user_id", "ends_at"],
        unique=False,
    )
    op.create_index(
        "user_access_passes_user_status_idx",
        "user_access_passes",
        ["user_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("user_access_passes_user_status_idx", table_name="user_access_passes")
    op.drop_index("user_access_passes_user_ends_at_idx", table_name="user_access_passes")
    op.drop_table("user_access_passes")

    op.drop_index("payment_orders_user_status_idx", table_name="payment_orders")
    op.drop_index("payment_orders_user_created_at_idx", table_name="payment_orders")
    op.drop_table("payment_orders")

    op.drop_index(
        "payment_webhook_events_processing_status_created_at_idx",
        table_name="payment_webhook_events",
    )
    op.drop_table("payment_webhook_events")
