"""Add evaluation harness tables eval_runs / eval_results with system-only RLS.

Revision ID: 20260814_0044
Revises: 20260717_0043
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260814_0044"
down_revision = "20260717_0043"
branch_labels = None
depends_on = None


_TABLES = ("eval_runs", "eval_results")


def _enable_system_only_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {table}_system_only ON {table}
          FOR ALL
          USING (current_setting('app.current_user_id', true) = 'system')
          WITH CHECK (current_setting('app.current_user_id', true) = 'system')
        """
    )


def upgrade() -> None:
    op.create_table(
        "eval_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("dataset_id", sa.String(length=120), nullable=False),
        sa.Column("dataset_version", sa.String(length=40), nullable=False),
        sa.Column("dataset_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("dataset_path", sa.Text(), nullable=True),
        sa.Column("runner_version", sa.String(length=80), nullable=False),
        sa.Column("git_revision", sa.String(length=64), nullable=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("run_kind", sa.String(length=10), nullable=False),
        sa.Column("judge_route", sa.String(length=8), nullable=False),
        sa.Column("requested_judge_provider", sa.String(length=40), nullable=True),
        sa.Column("requested_judge_model", sa.String(length=120), nullable=True),
        sa.Column("effective_judge_provider", sa.String(length=40), nullable=True),
        sa.Column("effective_judge_model", sa.String(length=120), nullable=True),
        sa.Column("rubric_version", sa.String(length=80), nullable=True),
        sa.Column("prompt_template_version", sa.String(length=80), nullable=True),
        sa.Column("prompt_hash", sa.String(length=64), nullable=True),
        sa.Column("content_mode", sa.String(length=12), nullable=False),
        sa.Column("artifact_dir", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("case_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completed_case_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_case_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cancelled_case_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("metric_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("metric_error_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cancelled_metric_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retention_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("seed", sa.BigInteger(), nullable=True),
        sa.Column(
            "generation_params", postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}", nullable=False,
        ),
        sa.Column(
            "selection_json", postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}", nullable=False,
        ),
        sa.Column(
            "config_json", postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}", nullable=False,
        ),
        sa.Column(
            "summary_json", postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}", nullable=False,
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "content_mode IN ('none', 'redacted', 'full')",
            name="eval_runs_content_mode_check",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'failed', 'cancelled', 'no_data')",
            name="eval_runs_status_check",
        ),
        sa.CheckConstraint(
            "run_kind IN ('case', 'aggregate')", name="eval_runs_run_kind_check"
        ),
        sa.CheckConstraint(
            "judge_route IN ('locked', 'none')", name="eval_runs_judge_route_check"
        ),
        sa.CheckConstraint("case_count >= 0", name="eval_runs_case_count_check"),
        sa.CheckConstraint(
            "completed_case_count >= 0", name="eval_runs_completed_case_count_check"
        ),
        sa.CheckConstraint(
            "failed_case_count >= 0", name="eval_runs_failed_case_count_check"
        ),
        sa.CheckConstraint(
            "cancelled_case_count >= 0", name="eval_runs_cancelled_case_count_check"
        ),
        sa.CheckConstraint("metric_count >= 0", name="eval_runs_metric_count_check"),
        sa.CheckConstraint(
            "metric_error_count >= 0", name="eval_runs_metric_error_count_check"
        ),
        sa.CheckConstraint(
            "cancelled_metric_count >= 0", name="eval_runs_cancelled_metric_count_check"
        ),
        sa.CheckConstraint(
            "run_kind <> 'aggregate' OR (case_count = 0 AND completed_case_count = 0 "
            "AND failed_case_count = 0 AND cancelled_case_count = 0)",
            name="eval_runs_aggregate_no_case_counts_check",
        ),
        sa.CheckConstraint(
            "run_kind <> 'case' OR (metric_count = 0 AND metric_error_count = 0 "
            "AND cancelled_metric_count = 0)",
            name="eval_runs_case_no_metric_counts_check",
        ),
        sa.CheckConstraint(
            "judge_route <> 'locked' OR (effective_judge_provider IS NOT NULL "
            "AND effective_judge_model IS NOT NULL AND rubric_version IS NOT NULL "
            "AND prompt_hash IS NOT NULL)",
            name="eval_runs_locked_route_fields_check",
        ),
        sa.CheckConstraint(
            "judge_route <> 'none' OR (effective_judge_provider IS NULL "
            "AND effective_judge_model IS NULL)",
            name="eval_runs_no_route_fields_check",
        ),
        sa.CheckConstraint(
            "dataset_fingerprint ~ '^[0-9a-f]{64}$'",
            name="eval_runs_dataset_fingerprint_check",
        ),
        sa.CheckConstraint(
            "prompt_hash IS NULL OR prompt_hash ~ '^[0-9a-f]{64}$'",
            name="eval_runs_prompt_hash_check",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(selection_json) = 'object' AND jsonb_typeof(config_json) = 'object' "
            "AND jsonb_typeof(summary_json) = 'object' "
            "AND jsonb_typeof(generation_params) = 'object'",
            name="eval_runs_json_object_check",
        ),
    )
    op.create_index(
        "eval_runs_status_created_idx",
        "eval_runs",
        ["status", sa.text("created_at DESC")],
    )
    op.create_index(
        "eval_runs_compare_idx",
        "eval_runs",
        [
            "dataset_fingerprint",
            "effective_judge_provider",
            "effective_judge_model",
            "rubric_version",
            sa.text("created_at DESC"),
        ],
    )

    op.create_table(
        "eval_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", sa.String(length=120), nullable=False),
        sa.Column("runner", sa.String(length=40), nullable=False),
        sa.Column("source_candidate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("character_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("response_status", sa.String(length=24), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("content_mode", sa.String(length=12), nullable=False),
        sa.Column("input_ref", sa.String(length=200), nullable=False),
        sa.Column("output_ref", sa.String(length=200), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=True),
        sa.Column("actual_output_text", sa.Text(), nullable=True),
        sa.Column(
            "dimension_scores_json", postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}", nullable=False,
        ),
        sa.Column(
            "rule_metrics_json", postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}", nullable=False,
        ),
        sa.Column("judge_rationale", sa.Text(), nullable=True),
        sa.Column("judge_provider", sa.String(length=40), nullable=True),
        sa.Column("judge_model", sa.String(length=120), nullable=True),
        sa.Column("rubric_version", sa.String(length=80), nullable=True),
        sa.Column("prompt_hash", sa.String(length=64), nullable=True),
        sa.Column("generation_provider", sa.String(length=40), nullable=True),
        sa.Column("generation_model", sa.String(length=120), nullable=True),
        sa.Column("generation_model_type", sa.String(length=120), nullable=True),
        sa.Column("pairwise_winner", sa.String(length=4), nullable=True),
        sa.Column("swap_consistent", sa.Boolean(), nullable=True),
        sa.Column("variant_a_ref", sa.String(length=200), nullable=True),
        sa.Column("variant_b_ref", sa.String(length=200), nullable=True),
        sa.Column("variant_a_generation_provider", sa.String(length=40), nullable=True),
        sa.Column("variant_a_generation_model", sa.String(length=120), nullable=True),
        sa.Column("variant_a_generation_model_type", sa.String(length=120), nullable=True),
        sa.Column("variant_b_generation_provider", sa.String(length=40), nullable=True),
        sa.Column("variant_b_generation_model", sa.String(length=120), nullable=True),
        sa.Column("variant_b_generation_model_type", sa.String(length=120), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail_safe", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["eval_runs.id"], name="eval_results_run_fkey", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_candidate_id"],
            ["candidates.id"],
            name="eval_results_source_candidate_fkey",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["character_id"],
            ["characters.id"],
            name="eval_results_character_fkey",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("run_id", "case_id", "runner", name="eval_results_run_case_runner_uniq"),
        sa.CheckConstraint(
            "runner IN ('chat_replay', 'candidate_replay', 'pairwise_replay', "
            "'realtime_functional')",
            name="eval_results_runner_check",
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'failed', 'skipped', 'no_data', 'cancelled')",
            name="eval_results_status_check",
        ),
        sa.CheckConstraint(
            "response_status IN ('valid', 'runner_error', 'judge_timeout', 'judge_error', "
            "'judge_parse_failed', 'judge_inconsistency', 'skipped', 'no_data', 'cancelled')",
            name="eval_results_response_status_check",
        ),
        sa.CheckConstraint(
            "content_mode IN ('none', 'redacted', 'full')",
            name="eval_results_content_mode_check",
        ),
        sa.CheckConstraint(
            "pairwise_winner IS NULL OR pairwise_winner IN ('a', 'b', 'tie')",
            name="eval_results_pairwise_winner_check",
        ),
        sa.CheckConstraint("attempt >= 1", name="eval_results_attempt_check"),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0", name="eval_results_duration_check"
        ),
        sa.CheckConstraint(
            "char_length(judge_rationale) <= 4000", name="eval_results_rationale_len_check"
        ),
        sa.CheckConstraint(
            "char_length(error_detail_safe) <= 1000",
            name="eval_results_error_detail_len_check",
        ),
        sa.CheckConstraint(
            "content_mode <> 'none' OR (input_text IS NULL AND actual_output_text IS NULL)",
            name="eval_results_none_mode_no_text_check",
        ),
        sa.CheckConstraint(
            "runner = 'pairwise_replay' OR (pairwise_winner IS NULL AND swap_consistent IS NULL "
            "AND variant_a_ref IS NULL AND variant_b_ref IS NULL "
            "AND variant_a_generation_provider IS NULL AND variant_b_generation_provider IS NULL)",
            name="eval_results_pairwise_fields_runner_check",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(dimension_scores_json) = 'object' "
            "AND jsonb_typeof(rule_metrics_json) = 'object'",
            name="eval_results_json_object_check",
        ),
    )
    op.create_index("eval_results_run_case_idx", "eval_results", ["run_id", "case_id"])
    op.create_index(
        "eval_results_status_created_idx",
        "eval_results",
        ["status", sa.text("created_at DESC")],
    )
    op.create_index(
        "eval_results_character_created_idx",
        "eval_results",
        ["character_id", sa.text("created_at DESC")],
    )

    for table in _TABLES:
        _enable_system_only_rls(table)


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_system_only ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_index("eval_results_character_created_idx", table_name="eval_results")
    op.drop_index("eval_results_status_created_idx", table_name="eval_results")
    op.drop_index("eval_results_run_case_idx", table_name="eval_results")
    op.drop_table("eval_results")
    op.drop_index("eval_runs_compare_idx", table_name="eval_runs")
    op.drop_index("eval_runs_status_created_idx", table_name="eval_runs")
    op.drop_table("eval_runs")
