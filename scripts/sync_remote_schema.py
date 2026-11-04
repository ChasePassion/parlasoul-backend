"""Synchronize the local PostgreSQL schema to a remote PostgreSQL database.

This script implements schema-only migration for deployment:
1. Read the source schema from the local development database.
2. Import the schema into the remote target database without data.
3. Stamp Alembic on the target so application startup checks stay aligned.

The source database is read-only. The optional target reset is destructive and
is guarded behind an explicit flag.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_ENV = ROOT / ".env"
DEFAULT_TARGET_ENV = ROOT / ".env.production"
POSTGRES_BIN_CANDIDATES = (
    Path(r"C:\Program Files\PostgreSQL\18\bin"),
    Path(r"C:\Program Files\PostgreSQL\17\bin"),
    Path(r"C:\Program Files\PostgreSQL\16\bin"),
    Path(r"C:\Program Files\pgAdmin 4\runtime"),
)


def _resolve_binary(binary_name: str) -> str:
    found = shutil.which(binary_name)
    if found:
        return found

    for directory in POSTGRES_BIN_CANDIDATES:
        candidate = directory / f"{binary_name}.exe"
        if candidate.exists():
            return str(candidate)

    raise FileNotFoundError(
        f"Could not find {binary_name}. Install PostgreSQL client tools or add them to PATH."
    )


def _normalize_pg_url(url: str) -> str:
    normalized = url.strip()
    if normalized.startswith("postgresql+asyncpg://"):
        return normalized.replace("postgresql+asyncpg://", "postgresql://", 1)
    if normalized.startswith("postgresql+psycopg://"):
        return normalized.replace("postgresql+psycopg://", "postgresql://", 1)
    return normalized


def _normalize_asyncpg_url(url: str) -> str:
    normalized = url.strip()
    if not normalized.startswith("postgresql+asyncpg://"):
        return normalized

    parts = urlsplit(normalized)
    query_pairs: list[tuple[str, str]] = []
    ssl_value: str | None = None

    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "sslmode":
            ssl_value = value
            continue
        if key == "channel_binding":
            continue
        query_pairs.append((key, value))

    if ssl_value is not None and not any(key == "ssl" for key, _ in query_pairs):
        query_pairs.append(("ssl", ssl_value))

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query_pairs),
            parts.fragment,
        )
    )


def _redact_password(url: str) -> tuple[str, str | None]:
    parts = urlsplit(url)
    password = parts.password
    username = parts.username
    hostname = parts.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"

    userinfo = ""
    if username:
        userinfo = quote(username, safe="")
        userinfo += "@"

    netloc = f"{userinfo}{hostname}"
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"

    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), password


def _database_identity(url: str) -> tuple[str, int | None, str]:
    parts = urlsplit(url)
    return (
        (parts.hostname or "").lower(),
        parts.port or 5432,
        parts.path.removeprefix("/"),
    )


def _assert_distinct_databases(source_url: str, target_url: str) -> None:
    if _database_identity(source_url) == _database_identity(target_url):
        raise ValueError(
            "Source and target databases resolve to the same host/port/database. "
            "Aborting to protect the local development database."
        )


def _load_env_file(env_file: Path) -> dict[str, str]:
    if not env_file.exists():
        raise FileNotFoundError(f"Environment file not found: {env_file}")

    values = dotenv_values(env_file, encoding="utf-8")
    result: dict[str, str] = {}
    for key, value in values.items():
        if value is not None:
            result[key] = value
    return result


def _run_command(
    command: Iterable[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path = ROOT,
) -> None:
    subprocess.run(list(command), check=True, cwd=cwd, env=env)


def _build_pg_env(base_env: dict[str, str], password: str | None) -> dict[str, str]:
    env = os.environ.copy()
    env.update(base_env)
    if password:
        env["PGPASSWORD"] = password
    return env


def _query_single_value(psql_bin: str, target_url: str, target_env: dict[str, str], sql: str) -> str:
    command = [psql_bin, target_url, "-v", "ON_ERROR_STOP=1", "-Atc", sql]
    result = subprocess.run(
        command,
        check=True,
        cwd=ROOT,
        env=target_env,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _stamp_alembic(target_settings: dict[str, str]) -> None:
    env = os.environ.copy()
    env.update(target_settings)
    env.setdefault("ENVIRONMENT", "production")
    if "DATABASE_URL" in env:
        env["DATABASE_URL"] = _normalize_asyncpg_url(env["DATABASE_URL"])
    _run_command([sys.executable, "-m", "alembic", "stamp", "head"], env=env)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync local PostgreSQL schema to remote Neon.")
    parser.add_argument(
        "--source-env-file",
        type=Path,
        default=DEFAULT_SOURCE_ENV,
        help="Path to the source dotenv file. Defaults to .env",
    )
    parser.add_argument(
        "--target-env-file",
        type=Path,
        default=DEFAULT_TARGET_ENV,
        help="Path to the target dotenv file. Defaults to .env.production",
    )
    parser.add_argument(
        "--dump-file",
        type=Path,
        help="Optional output path for the intermediate schema dump.",
    )
    parser.add_argument(
        "--reset-target",
        action="store_true",
        help="Drop and recreate the target public schema before import.",
    )
    parser.add_argument(
        "--skip-stamp",
        action="store_true",
        help="Skip `alembic stamp head` after import.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Skip export/import and only verify the target schema state.",
    )
    args = parser.parse_args()

    source_settings = _load_env_file(args.source_env_file)
    target_settings = _load_env_file(args.target_env_file)

    source_url = _normalize_pg_url(source_settings["DATABASE_URL"])
    target_url = _normalize_pg_url(target_settings["DATABASE_URL"])
    _assert_distinct_databases(source_url, target_url)

    source_conn_url, source_password = _redact_password(source_url)
    target_conn_url, target_password = _redact_password(target_url)
    source_env = _build_pg_env(source_settings, source_password)
    target_env = _build_pg_env(target_settings, target_password)

    pg_dump_bin = _resolve_binary("pg_dump")
    psql_bin = _resolve_binary("psql")

    if args.dump_file is not None:
        dump_path = args.dump_file.resolve()
        dump_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        temp_dir = Path(tempfile.mkdtemp(prefix="parlasoul-schema-sync-"))
        dump_path = temp_dir / "schema.sql"

    if not args.verify_only:
        print(f"Exporting schema from source database using {pg_dump_bin} ...")
        _run_command(
            [
                pg_dump_bin,
                "--schema-only",
                "--no-owner",
                "--no-privileges",
                "--exclude-table=public.alembic_version",
                "--file",
                str(dump_path),
                source_conn_url,
            ],
            env=source_env,
        )

        if args.reset_target:
            print("Resetting target public schema ...")
            _run_command(
                [
                    psql_bin,
                    target_conn_url,
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    "DROP SCHEMA public CASCADE; CREATE SCHEMA public;",
                ],
                env=target_env,
            )

        print("Importing schema into target database ...")
        _run_command(
            [psql_bin, target_conn_url, "-v", "ON_ERROR_STOP=1", "-f", str(dump_path)],
            env=target_env,
        )

        if not args.skip_stamp:
            print("Stamping Alembic head on target database ...")
            _stamp_alembic(target_settings)

    public_table_count = _query_single_value(
        psql_bin,
        target_conn_url,
        target_env,
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE';
        """,
    )
    enum_names = _query_single_value(
        psql_bin,
        target_conn_url,
        target_env,
        """
        SELECT COALESCE(string_agg(t.typname, ',' ORDER BY t.typname), '')
        FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'public' AND t.typtype = 'e';
        """,
    )
    alembic_head = _query_single_value(
        psql_bin,
        target_conn_url,
        target_env,
        "SELECT version_num FROM public.alembic_version;",
    )

    print("Schema sync completed.")
    print(f"Target public table count: {public_table_count}")
    print(f"Target enum types: {enum_names or '(none)'}")
    print(f"Alembic head: {alembic_head}")
    print(f"Schema dump saved at: {dump_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
