"""Run report artifacts: JSONL (machine) + Markdown (human).

Both artifacts pass through the same recursive allowlist sanitizer; a
violation aborts the write (fail-closed). Under ``content_mode=none`` only
numbers, statuses, counts, lengths/hashes and controlled identifiers survive.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from src.evaluation.constants import DIMENSIONS
from src.evaluation.metrics.sanitize import SanitizerViolationError, sanitize_value
from src.evaluation.runners.base import CaseOutcome

logger = logging.getLogger(__name__)


class ReportWriteError(Exception):
    """Raised when an artifact cannot be written safely."""


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _fmt_pct(value: object) -> str:
    if value is None or not isinstance(value, (int, float)):
        return "—"
    return f"{value:.2%}"


def _fmt_float(value: object, spec: str) -> str:
    if value is None or not isinstance(value, (int, float)):
        return "—"
    return f"{value:{spec}}"


def _fmt_signed(value: object) -> str:
    if value is None or not isinstance(value, (int, float)):
        return "—"
    return f"{value:+.2f}"


def outcome_to_report_dict(outcome: CaseOutcome, *, content_mode: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "case_id": outcome.case_id,
        "runner": outcome.runner,
        "status": outcome.status,
        "response_status": outcome.response_status,
        "duration_ms": outcome.duration_ms,
        "dimension_scores": outcome.dimension_scores,
        "rule_metrics": outcome.rule_metrics,
        "input_ref": outcome.input_ref,
        "output_ref": outcome.output_ref,
        "error_code": outcome.error_code,
        "error_detail_safe": outcome.error_detail_safe,
        "pairwise_winner": outcome.pairwise_winner,
        "swap_consistent": outcome.swap_consistent,
        "generation_provider": outcome.generation_provider,
        "generation_model": outcome.generation_model,
    }
    if content_mode in ("redacted", "full"):
        report["input_text_length"] = len(outcome.input_text or "")
        report["output_text_length"] = len(outcome.actual_output_text or "")
    return sanitize_value(report, content_mode=content_mode)


def summarize_outcomes(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    response_counts: dict[str, int] = {}
    dimension_totals: dict[str, int] = {}
    dimension_counts: dict[str, int] = {}
    for outcome in outcomes:
        status_counts[outcome.status] = status_counts.get(outcome.status, 0) + 1
        response_counts[outcome.response_status] = (
            response_counts.get(outcome.response_status, 0) + 1
        )
        for dimension, score in outcome.dimension_scores.items():
            dimension_totals[dimension] = dimension_totals.get(dimension, 0) + score
            dimension_counts[dimension] = dimension_counts.get(dimension, 0) + 1
    dimension_means = {
        dimension: (dimension_totals[dimension] / dimension_counts[dimension])
        if dimension_counts[dimension]
        else None
        for dimension in DIMENSIONS
    }
    return {
        "case_total": len(outcomes),
        "status_counts": status_counts,
        "response_status_counts": response_counts,
        "dimension_case_counts": dimension_counts,
        "dimension_mean_scores": dimension_means,
    }


class ReportWriter:
    def __init__(self, *, out_dir: Path, content_mode: str) -> None:
        self._out_dir = out_dir
        self._content_mode = content_mode

    def write_run_report(
        self,
        *,
        run_id: str,
        run_metadata: dict[str, Any],
        outcomes: list[CaseOutcome],
        summary: dict[str, Any],
    ) -> tuple[Path, Path]:
        try:
            meta = sanitize_value(run_metadata, content_mode=self._content_mode)
            sanitized_summary = sanitize_value(summary, content_mode=self._content_mode)
            rows = [
                outcome_to_report_dict(outcome, content_mode=self._content_mode)
                for outcome in outcomes
            ]
        except SanitizerViolationError as exc:
            raise ReportWriteError(f"report sanitizer violation: {exc}") from exc

        jsonl_path = self._out_dir / f"{run_id}.jsonl"
        markdown_path = self._out_dir / f"{run_id}.md"

        lines = [json.dumps({"report": "meta", **meta}, ensure_ascii=False)]
        lines.append(
            json.dumps({"report": "summary", **sanitized_summary}, ensure_ascii=False)
        )
        for row in rows:
            lines.append(json.dumps({"report": "result", **row}, ensure_ascii=False))
        _atomic_write_text(jsonl_path, "\n".join(lines) + "\n")

        _atomic_write_text(
            markdown_path,
            self._render_markdown(meta, sanitized_summary, rows),
        )
        return jsonl_path, markdown_path

    def _render_markdown(
        self,
        meta: dict[str, Any],
        summary: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> str:
        parts: list[str] = []

        # Title + metadata table -----------------------------------------------
        dataset_id = meta.get("dataset_id", "unknown")
        dataset_version = meta.get("dataset_version", "unknown")
        judge_label = f"{meta.get('judge_provider', '?')}/{meta.get('judge_model', '?')}"
        parts.append(f"# Evaluation Run — {dataset_id} @ {dataset_version}")
        parts.append("")
        parts.append(
            f"Judge: **{judge_label}** · Status: **{meta.get('run_status', summary.get('run_status', '?'))}**"
        )
        parts.append("")
        parts.append("## Run metadata")
        parts.append("")
        parts.append("| key | value |")
        parts.append("| --- | --- |")
        for key in sorted(meta):
            parts.append(f"| `{key}` | {meta[key]} |")
        parts.append("")

        # Summary (parsed) -----------------------------------------------------
        parts.append("## Summary")
        parts.append("")

        run_status = summary.get("run_status", "?")
        if run_status == "completed":
            status_badge = "✅ completed"
        elif run_status == "partial":
            status_badge = "⚠️  partial"
        elif run_status == "failed":
            status_badge = "❌ failed"
        else:
            status_badge = f"❔ {run_status}"
        parts.append(f"**Run status:** {status_badge}")
        parts.append("")

        # Dimension scores (table)
        dims = summary.get("dimensions", {})
        if dims:
            parts.append("### Dimension scores")
            parts.append("")
            parts.append("| dimension | status | n | mean | distribution |")
            parts.append("| --- | --- | --- | --- | --- |")
            for dimension in sorted(dims):
                v = dims[dimension]
                status = v.get("status", "?")
                n = v.get("sample_count", 0)
                mean = v.get("mean")
                dist = v.get("distribution") or {}
                mean_str = _fmt_float(mean, ".2f")
                dist_str = (
                    " · ".join(f"**{k}**: {dist[k]}" for k in sorted(dist)) if dist else "—"
                )
                parts.append(
                    f"| `{dimension}` | {status} | {n} | {mean_str} | {dist_str} |"
                )
            parts.append("")

        # Memory metrics
        mem = summary.get("memory", {})
        if mem:
            parts.append("### Memory metrics")
            parts.append("")
            parts.append("| metric | status | n | value | note |")
            parts.append("| --- | --- | --- | --- | --- |")
            for metric in sorted(mem):
                v = mem[metric]
                status = v.get("status", "?")
                n = v.get("sample_count", 0)
                value: object | str = "—"
                if "mean_rate" in v:
                    value = _fmt_pct(v["mean_rate"])
                elif "accuracy" in v:
                    value = _fmt_pct(v["accuracy"])
                elif "correct" in v and v.get("accuracy") is not None:
                    value = f"{v['correct']}/{v.get('gold_count', '?')}"
                note = v.get("reason", "")
                parts.append(
                    f"| `{metric}` | {status} | {n} | {value} | {note} |"
                )
            parts.append("")

        # OOC
        ooc = summary.get("ooc", {})
        if ooc:
            parts.append("### OOC accuracy")
            parts.append("")
            parts.append(f"Status: **{ooc.get('status', '?')}** · ")
            if ooc.get("status") == "available":
                correct = ooc.get("correct", 0)
                gold = ooc.get("gold_count", 0)
                predicted = ooc.get("predicted_count", 0)
                accuracy = ooc.get("accuracy")
                acc_str = _fmt_pct(accuracy)
                parts.append(
                    f"correct: **{correct}/{gold}** · predicted_count: {predicted} · "
                    f"accuracy: **{acc_str}**"
                )
            parts.append("")

        # Pairwise
        pw = summary.get("pairwise", {})
        if pw:
            parts.append("### Pairwise win rate")
            parts.append("")
            parts.append(f"Status: **{pw.get('status', '?')}** · ")
            if pw.get("status") == "available":
                wins_a = pw.get("wins_a", 0)
                wins_b = pw.get("wins_b", 0)
                ties = pw.get("ties", 0)
                n = pw.get("valid_pair_count", 0)
                ra = pw.get("win_rate_a")
                rb = pw.get("win_rate_b")
                rt = pw.get("tie_rate")
                ra_str = _fmt_pct(ra)
                rb_str = _fmt_pct(rb)
                rt_str = _fmt_pct(rt)
                parts.append(
                    f"valid_pairs: **{n}** · A wins: **{wins_a}** ({ra_str}) · "
                    f"B wins: **{wins_b}** ({rb_str}) · ties: **{ties}** ({rt_str}) · "
                    f"errors: {pw.get('error_count', 0)} · inconsistencies: {pw.get('inconsistency_count', 0)}"
                )
            parts.append("")

        # Repetition
        rep = summary.get("repetition", {})
        if rep:
            parts.append("### Repetition 3-gram")
            parts.append("")
            rate = rep.get("mean_rate")
            rate_str = _fmt_float(rate, ".4f")
            parts.append(
                f"Status: **{rep.get('status', '?')}** · "
                f"n: {rep.get('sample_count', 0)} · mean_rate: **{rate_str}** · "
                f"no_data: {rep.get('no_data_count', 0)}"
            )
            parts.append("")

        # Memory utility delta
        mut = summary.get("memory_utility_delta", {})
        if mut:
            parts.append("### Memory Utility Δ (on vs off)")
            parts.append("")
            parts.append(f"Status: **{mut.get('status', '?')}** · valid_pairs: {mut.get('valid_pair_count', 0)}")
            parts.append("")
            parts.append("| dimension | n | mean Δ | positive_rate |")
            parts.append("| --- | --- | --- | --- |")
            for dim_name in sorted(mut.get("dimensions", {})):
                v = mut["dimensions"][dim_name]
                mean_d = v.get("mean_delta")
                pos = v.get("positive_rate")
                mean_str = _fmt_signed(mean_d)
                pos_str = _fmt_pct(pos)
                parts.append(
                    f"| `{dim_name}` | {v.get('sample_count', 0)} | {mean_str} | {pos_str} |"
                )
            parts.append("")

        # Results table ---------------------------------------------------------
        parts.append("## Results")
        parts.append("")

        failed = [r for r in rows if r.get("status") != "completed"]
        if failed:
            parts.append(f"**{len(failed)} failed / {len(rows)} total**")
        else:
            parts.append(f"**All {len(rows)} cases completed successfully**")
        parts.append("")

        parts.append("| case_id | runner | status | response | duration_ms | dim scores |")
        parts.append("| --- | --- | --- | --- | --- | --- |")
        for row in sorted(rows, key=lambda r: r.get("case_id", "")):
            dim_str = "—"
            scores = row.get("dimension_scores") or {}
            if scores:
                parts_dim = [
                    f"{k.replace('_', '')[:3]}={v}" for k, v in sorted(scores.items())
                ]
                dim_str = ", ".join(parts_dim)
            error_code = row.get("error_code")
            response = row.get("response_status", "?")
            if error_code:
                response = f"{response} (`{error_code}`)"
            parts.append(
                "| {case_id} | {runner} | {status} | {response} | {duration_ms} | {scores} |".format(
                    case_id=row.get("case_id", ""),
                    runner=row.get("runner", ""),
                    status=row.get("status", ""),
                    response=response,
                    duration_ms=row.get("duration_ms", ""),
                    scores=dim_str,
                )
            )
        parts.append("")
        return "\n".join(parts)
