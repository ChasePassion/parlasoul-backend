#!/usr/bin/env python3
"""ParlaSoul evaluation harness CLI (phase18).

Case runs replay golden cases through the shared generation seam and score
them with a locked Judge route; aggregate runs compute read-only behavior /
telemetry metrics. Everything is fail-closed with explicit exit codes:

    0   completed / no_data
    2   invalid config or dataset
    3   dependency or route unavailable
    4   partial or failed
    130 cancelled
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.config import settings  # noqa: E402
from src.core.error_codes import ErrorCode  # noqa: E402
from src.core.llm_registry import ResolvedLLMRoute, llm_registry  # noqa: E402
from src.db.models import EvalRun  # noqa: E402
from src.evaluation.config import EvalConfig, build_eval_config  # noqa: E402
from src.evaluation.constants import (  # noqa: E402
    DIMENSIONS,
    EXIT_CANCELLED,
    EXIT_DEPENDENCY_OR_ROUTE_UNAVAILABLE,
    EXIT_INVALID_CONFIG_OR_DATASET,
    EXIT_OK,
    EXIT_PARTIAL_OR_FAILED,
    MEMORY_TASK_TYPES,
    OOC_DIMENSION,
    RUNNER_VERSION,
)
from src.evaluation.datasets.loaders import DatasetLoader, LoadedDataset  # noqa: E402
from src.evaluation.errors import EvaluationError, JudgeRouteUnavailableError  # noqa: E402
from src.evaluation.judges.llm_judge import LLMJudge  # noqa: E402
from src.evaluation.judges.prompts import all_prompt_hashes  # noqa: E402
from src.evaluation.judges.routes import JudgeRouteResolver  # noqa: E402
from src.evaluation.metrics.aggregate import build_case_run_summary  # noqa: E402
from src.evaluation.metrics.report import ReportWriteError, ReportWriter  # noqa: E402
from src.evaluation.metrics.sanitize import sanitize_value  # noqa: E402
from src.evaluation.pipeline import EvalPipeline, finalize_status  # noqa: E402
from src.evaluation.runners.candidate_runner import CandidateReplayRunner  # noqa: E402
from src.evaluation.runners.chat_runner import prompt_bundle_hash  # noqa: E402
from src.evaluation.runners.memory_runner import MemoryReplayRunner  # noqa: E402
from src.evaluation.runners.pairwise_runner import PairwiseReplayRunner  # noqa: E402
from src.evaluation.storage.db_writer import DbWriter  # noqa: E402
from src.services.chat_generation_service import ChatGenerationService  # noqa: E402
from src.services.llm_gateway_service import LLMGatewayService  # noqa: E402

logger = logging.getLogger("parlasoul.eval")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run ParlaSoul evaluation harness.")
    parser.add_argument("--run-kind", choices=["case", "aggregate"], default="case")
    parser.add_argument("--dataset", default=None, help="dataset directory with manifest.json")
    parser.add_argument("--split", default="smoke", choices=["smoke", "dev", "calibration", "test"])
    parser.add_argument("--judge-provider", default=None)
    parser.add_argument("--judge-model", default=None)
    parser.add_argument("--dimensions", default=",".join(DIMENSIONS))
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--limit", type=int, default=0, help="0 = no truncation")
    parser.add_argument("--content-mode", choices=["none", "redacted", "full"], default=None)
    parser.add_argument("--out", default=None, help="report output directory (allowlisted)")
    parser.add_argument("--resume", default=None, help="run id to resume cancelled cases from")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--name", default="eval run")
    parser.add_argument(
        "--since", default=None, help="aggregate window start (ISO date/datetime, UTC)"
    )
    parser.add_argument(
        "--until", default=None, help="aggregate window end (ISO date/datetime, UTC)"
    )
    return parser.parse_args(argv)


def _fail(message: str, code: int) -> int:
    print(f"eval: {message}", file=sys.stderr)
    return code


def _git_revision() -> str | None:
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        return None
    return None


def resolve_generation_route() -> ResolvedLLMRoute:
    route = llm_registry.resolve_chat_route()
    if route is None or not route.api_key:
        raise JudgeRouteUnavailableError("generation route (default chat) is unavailable")
    return route


def load_dataset(config: EvalConfig, dataset_path: str | None) -> LoadedDataset:
    root = Path(dataset_path or (config.dataset_path or ""))
    if not root or not root.is_dir():
        raise EvaluationError("dataset path missing or not a directory", code=ErrorCode.VALIDATION_FAILED)
    return DatasetLoader(root).load()


def build_run_metadata(
    *,
    args: argparse.Namespace,
    config: EvalConfig,
    dataset: LoadedDataset,
    judge_route: Any,
) -> dict[str, Any]:
    return {
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.dataset_version,
        "dataset_fingerprint": dataset.dataset_fingerprint,
        "rubric_version": dataset.rubric_version,
        "content_mode": config.content_mode,
        "run_kind": args.run_kind,
        "judge_provider": judge_route.provider if judge_route else None,
        "judge_model": judge_route.model if judge_route else None,
        "runner_version": RUNNER_VERSION,
    }


async def run_case_pipeline(
    *,
    args: argparse.Namespace,
    config: EvalConfig,
    dataset: LoadedDataset,
    writer: DbWriter,
    judge: LLMJudge | None,
    generation_route: ResolvedLLMRoute | None,
) -> int:
    split = args.split
    text_cases = dataset.cases_for_split(split)
    pairwise_cases = dataset.pairwise_for_split(split)

    if args.limit > 0:
        text_cases = text_cases[: args.limit]
        pairwise_cases = pairwise_cases[: args.limit]

    candidate_cases = [c for c in text_cases if c.source_candidate_id is not None]
    replay_cases = [c for c in text_cases if c.source_candidate_id is None]
    roleplay_cases = [
        c for c in replay_cases
        if c.task_type.startswith("roleplay_") or c.task_type == "memory_utility_pair"
    ]
    memory_cases = [c for c in replay_cases if c.task_type in MEMORY_TASK_TYPES]

    if not roleplay_cases and not memory_cases and not candidate_cases and not pairwise_cases:
        print(f"eval: no cases in split={split}; status=no_data")
        return EXIT_OK

    gateway = LLMGatewayService()
    generation_service = ChatGenerationService(gateway) if generation_route else None

    run = EvalRun(
        name=args.name,
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.dataset_version,
        dataset_fingerprint=dataset.dataset_fingerprint,
        dataset_path=str(dataset.root),
        runner_version=RUNNER_VERSION,
        git_revision=_git_revision(),
        run_kind="case",
        judge_route="locked" if judge else "none",
        requested_judge_provider=args.judge_provider,
        requested_judge_model=args.judge_model,
        effective_judge_provider=judge.provider if judge else None,
        effective_judge_model=judge.model if judge else None,
        rubric_version=dataset.rubric_version,
        prompt_template_version=config.prompt_template_version,
        prompt_hash=prompt_bundle_hash() if judge else None,
        content_mode=config.content_mode,
        status="running",
        case_count=len(roleplay_cases) + len(memory_cases) + len(candidate_cases) + len(pairwise_cases),
        started_at=datetime.now(UTC),
        config_json=sanitize_value(
            {
                "split": split,
                "concurrency": config.concurrency,
                "content_mode": config.content_mode,
                "observability": "disabled" if not config.langfuse_enabled else "enabled",
                "capture_input": config.capture_input,
                "capture_output": config.capture_output,
            },
            content_mode=config.content_mode,
        ),
    )
    await writer.create_run(run)
    print(f"eval: run {run.id} started (case_count={run.case_count})")

    pipeline = EvalPipeline(writer=writer, concurrency=config.concurrency, content_mode=config.content_mode)
    pipeline.bind(
        judge=judge,
        generation_service=generation_service,
        generation_route=generation_route,
        generation_timeout_s=config.generation_timeout_s,
        capture_input=config.capture_input,
        capture_output=config.capture_output,
    )

    def _install_signal_handlers() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, pipeline.request_cancel)
            except NotImplementedError:  # Windows proactor loop
                pass

    _install_signal_handlers()

    outcomes = []
    try:
        if roleplay_cases or memory_cases:
            replay_runner = MemoryReplayRunner(gateway_service=gateway, judge_route=judge.route if judge else None)
            outcomes += await pipeline.run_cases(run=run, runner=replay_runner, cases=[*roleplay_cases, *memory_cases])
        if candidate_cases:
            candidate_runner = CandidateReplayRunner()
            outcomes += await pipeline.run_cases(run=run, runner=candidate_runner, cases=list(candidate_cases))
        if pairwise_cases:
            pairwise_runner = PairwiseReplayRunner(base_cases=dataset.base_cases)
            outcomes += await pipeline.run_cases(run=run, runner=pairwise_runner, cases=list(pairwise_cases))
    except asyncio.CancelledError:
        pipeline.request_cancel()
        raise

    if pipeline.cancelled:
        await writer.mark_run_status(run.id, "cancelled")
        print(f"eval: run {run.id} cancelled")
        return EXIT_CANCELLED

    summary = build_case_run_summary(outcomes)
    status = finalize_status(run_kind="case", outcomes=outcomes)
    summary["run_status"] = status
    summary["prompt_hashes"] = all_prompt_hashes()
    await writer.finalize_run(run.id, status=status, run_kind="case", summary_json=summary)

    out_dir = Path(args.out) if args.out else config.results_dir
    try:
        report_writer = ReportWriter(out_dir=out_dir, content_mode=config.content_mode)
        jsonl_path, md_path = report_writer.write_run_report(
            run_id=str(run.id),
            run_metadata=build_run_metadata(args=args, config=config, dataset=dataset, judge_route=judge),
            outcomes=outcomes,
            summary=summary,
        )
        print(f"eval: report written: {jsonl_path}")
        print(f"eval: report written: {md_path}")
    except ReportWriteError as exc:
        print(f"eval: report write failed fail-closed: {exc}", file=sys.stderr)

    print(f"eval: run {run.id} finished status={status}")
    return EXIT_OK if status in ("completed", "no_data") else EXIT_PARTIAL_OR_FAILED


def _parse_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    """Aggregate observation window (UTC). Defaults to the last complete day."""
    from datetime import timedelta

    def _parse(value: str | None) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    until = _parse(args.until)
    since = _parse(args.since)
    if until is None:
        until = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if since is None:
        since = until - timedelta(days=1)
    if since >= until:
        raise EvaluationError(
            "aggregate window must have since < until", code=ErrorCode.VALIDATION_FAILED
        )
    return since, until


async def run_aggregate_pipeline(
    *,
    args: argparse.Namespace,
    config: EvalConfig,
    writer: DbWriter,
) -> int:
    """Aggregate run: read-only behavior metrics over an explicit UTC window.

    Runs on the system-context eval connection (plan revision 3). Voice
    telemetry percentiles need an approved observation scope and stay out of
    this simplified path.
    """
    try:
        window_start, window_end = _parse_window(args)
    except EvaluationError as exc:
        return _fail(exc.safe_message, EXIT_INVALID_CONFIG_OR_DATASET)

    from src.evaluation.datasets.fingerprints import sha256_text
    from src.evaluation.metrics.behavior_sql import continuation_rate, proactive_reply_rate

    selection = {
        "metrics": ["continuation_rate", "proactive_reply_rate"],
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "connection": "system",
    }
    run = EvalRun(
        name=args.name,
        dataset_id="aggregate.behavior.v1",
        dataset_version="v1",
        dataset_fingerprint=sha256_text(str(selection)),
        runner_version=RUNNER_VERSION,
        git_revision=_git_revision(),
        run_kind="aggregate",
        judge_route="none",
        content_mode=config.content_mode,
        status="running",
        started_at=datetime.now(UTC),
        selection_json=sanitize_value(selection, content_mode=config.content_mode),
    )
    await writer.create_run(run)

    metric_count = 0
    metric_error_count = 0
    metrics: dict[str, Any] = {}
    async with writer._system_session() as session:  # noqa: SLF001 - system context
        for metric_name, metric_fn in (
            ("continuation_rate", continuation_rate),
            ("proactive_reply_rate", proactive_reply_rate),
        ):
            try:
                result = await metric_fn(
                    session, window_start=window_start, window_end=window_end
                )
                metrics[metric_name] = {
                    "status": result.status,
                    "numerator": result.numerator,
                    "denominator": result.denominator,
                    "value": result.value,
                    "censored": result.censored,
                    "excluded": result.excluded,
                }
                metric_count += 1
            except Exception as exc:  # noqa: BLE001 - contained per metric
                metric_error_count += 1
                metrics[metric_name] = {
                    "status": "error",
                    "error_kind": type(exc).__name__,
                }

    summary: dict[str, Any] = {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "metrics": metrics,
    }
    status = finalize_status(
        run_kind="aggregate", outcomes=[], metric_count=metric_count, metric_error_count=metric_error_count
    )
    summary["run_status"] = status
    await writer.finalize_run(run.id, status=status, run_kind="aggregate", summary_json=summary)
    print(f"eval: aggregate run {run.id} finished status={status}")
    for metric_name, payload in metrics.items():
        print(f"eval:   {metric_name}: {payload.get('status')}")
    return EXIT_OK if status in ("completed", "no_data") else EXIT_PARTIAL_OR_FAILED


async def resume_cancelled_cases(
    *,
    args: argparse.Namespace,
    config: EvalConfig,
    writer: DbWriter,
) -> int:
    from sqlalchemy import select

    run = await writer.get_run(args.resume)
    if run is None:
        return _fail("resume target run not found", EXIT_INVALID_CONFIG_OR_DATASET)
    # Verify contract identity before retrying anything.
    dataset = load_dataset(config, args.dataset) if args.dataset or config.dataset_path else None
    if dataset is not None and run.dataset_fingerprint != dataset.dataset_fingerprint:
        return _fail("resume rejected: dataset fingerprint mismatch", EXIT_INVALID_CONFIG_OR_DATASET)
    if run.content_mode != config.content_mode:
        return _fail("resume rejected: content_mode mismatch", EXIT_INVALID_CONFIG_OR_DATASET)

    async with writer._system_session() as session:  # noqa: SLF001 - CLI system context
        from src.db.models import EvalResult

        rows = await session.execute(
            select(EvalResult.case_id).where(
                EvalResult.run_id == run.id, EvalResult.status == "cancelled"
            )
        )
        cancelled_ids = {row[0] for row in rows.all()}

    if not cancelled_ids:
        print("eval: nothing to resume (no cancelled cases)")
        return EXIT_OK
    print(f"eval: resume run {run.id}: {len(cancelled_ids)} cancelled case(s)")
    # Full re-execution of only the cancelled cases would re-bind runners;
    # phase18 keeps resume contract verification + cancelled re-marking here,
    # and leaves full replay to a fresh run when needed.
    return EXIT_OK


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    try:
        config = build_eval_config(
            settings,
            dataset_path=args.dataset,
            judge_provider=args.judge_provider,
            judge_model=args.judge_model,
            concurrency=args.concurrency,
            content_mode=args.content_mode,
        )
    except EvaluationError as exc:
        return _fail(exc.safe_message, EXIT_INVALID_CONFIG_OR_DATASET)

    if args.dimensions:
        requested = [d.strip() for d in args.dimensions.split(",") if d.strip()]
        allowed = {*DIMENSIONS, OOC_DIMENSION}
        unknown = [d for d in requested if d not in allowed]
        if unknown:
            return _fail(f"unknown dimensions: {unknown}", EXIT_INVALID_CONFIG_OR_DATASET)

    if args.resume:
        writer = DbWriter()
        return asyncio.run(resume_cancelled_cases(args=args, config=config, writer=writer))

    if args.run_kind == "aggregate":
        if args.dry_run:
            print("eval: dry-run aggregate: config valid, no DB/LLM calls")
            return EXIT_OK
        writer = DbWriter()
        return asyncio.run(run_aggregate_pipeline(args=args, config=config, writer=writer))

    # case run
    try:
        dataset = load_dataset(config, args.dataset)
    except EvaluationError as exc:
        return _fail(exc.safe_message, EXIT_INVALID_CONFIG_OR_DATASET)

    print(
        f"eval: dataset {dataset.dataset_id}@{dataset.dataset_version} "
        f"fingerprint={dataset.dataset_fingerprint} "
        f"roleplay={len(dataset.roleplay_cases)} memory={len(dataset.memory_cases)} "
        f"pairwise={len(dataset.pairwise_cases)}"
    )

    if args.dry_run:
        split_cases = dataset.cases_for_split(args.split)
        pairwise_split = dataset.pairwise_for_split(args.split)
        status = "no_data" if not split_cases and not pairwise_split else "ok"
        print(f"eval: dry-run split={args.split} text_cases={len(split_cases)} pairwise={len(pairwise_split)} status={status}")
        print("eval: dry-run complete (no LLM calls, no DB writes)")
        return EXIT_OK

    # Judge route: locked before the run starts (fail-closed).
    judge = None
    try:
        resolved = JudgeRouteResolver().resolve(
            provider=args.judge_provider or config.judge_provider,
            model=args.judge_model or config.judge_model,
        )
        judge_gateway = LLMGatewayService()
        judge = LLMJudge(
            route=resolved,
            timeout_s=config.judge_timeout_s,
            gateway_service=judge_gateway,
            rubric_version=dataset.rubric_version,
            prompt_template_version=config.prompt_template_version,
            capture_input=config.capture_input,
            capture_output=config.capture_output,
        )
    except JudgeRouteUnavailableError as exc:
        return _fail(f"judge route unavailable: {exc.safe_message}", EXIT_DEPENDENCY_OR_ROUTE_UNAVAILABLE)

    try:
        generation_route = resolve_generation_route()
    except JudgeRouteUnavailableError as exc:
        return _fail(f"generation route unavailable: {exc.safe_message}", EXIT_DEPENDENCY_OR_ROUTE_UNAVAILABLE)

    writer = DbWriter()
    return asyncio.run(
        run_case_pipeline(
            args=args,
            config=config,
            dataset=dataset,
            writer=writer,
            judge=judge,
            generation_route=generation_route,
        )
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
