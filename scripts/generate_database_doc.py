"""Generate a Markdown database reference from the live PostgreSQL schema."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import asyncpg
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_REPO_ROOT = REPO_ROOT.parent / "parlasoul-frontend"
DEFAULT_SCHEMA = "public"
DEFAULT_SEMANTICS_SOURCE = REPO_ROOT / "docs" / "database" / "database-semantics.md"
DEFAULT_OUTPUTS = [
    REPO_ROOT / ".agent" / "dataschema.md",
    FRONTEND_REPO_ROOT / ".agent" / "dataschema.md",
]

TABLE_OVERVIEW_SQL = """
SELECT
    cls.relname AS table_name,
    CASE cls.relkind
        WHEN 'r' THEN 'BASE TABLE'
        WHEN 'p' THEN 'PARTITIONED TABLE'
        ELSE cls.relkind::text
    END AS table_kind
FROM pg_class AS cls
JOIN pg_namespace AS ns
  ON ns.oid = cls.relnamespace
WHERE ns.nspname = $1
  AND cls.relkind IN ('r', 'p')
ORDER BY cls.relname;
"""

COLUMN_SQL = """
SELECT
    attr.attnum AS ordinal_position,
    attr.attname AS column_name,
    pg_catalog.format_type(attr.atttypid, attr.atttypmod) AS data_type,
    NOT attr.attnotnull AS is_nullable,
    pg_get_expr(def.adbin, def.adrelid) AS column_default,
    col_description(cls.oid, attr.attnum) AS column_comment,
    CASE attr.attidentity
        WHEN 'a' THEN 'ALWAYS'
        WHEN 'd' THEN 'BY DEFAULT'
        ELSE NULL
    END AS identity_kind,
    CASE attr.attgenerated
        WHEN 's' THEN 'STORED'
        ELSE NULL
    END AS generated_kind
FROM pg_attribute AS attr
JOIN pg_class AS cls
  ON cls.oid = attr.attrelid
JOIN pg_namespace AS ns
  ON ns.oid = cls.relnamespace
LEFT JOIN pg_attrdef AS def
  ON def.adrelid = attr.attrelid
 AND def.adnum = attr.attnum
WHERE ns.nspname = $1
  AND cls.relname = $2
  AND attr.attnum > 0
  AND NOT attr.attisdropped
ORDER BY attr.attnum;
"""

CONSTRAINT_SQL = """
SELECT
    con.conname AS constraint_name,
    CASE con.contype
        WHEN 'p' THEN 'PRIMARY KEY'
        WHEN 'f' THEN 'FOREIGN KEY'
        WHEN 'u' THEN 'UNIQUE'
        WHEN 'c' THEN 'CHECK'
        WHEN 'x' THEN 'EXCLUDE'
        ELSE con.contype::text
    END AS constraint_type,
    pg_get_constraintdef(con.oid, true) AS definition,
    COALESCE(
        ARRAY_AGG(attr.attname ORDER BY cols.ordinality)
            FILTER (WHERE attr.attname IS NOT NULL),
        ARRAY[]::text[]
    ) AS column_names,
    ref_ns.nspname AS referenced_schema,
    ref_cls.relname AS referenced_table
FROM pg_constraint AS con
JOIN pg_class AS cls
  ON cls.oid = con.conrelid
JOIN pg_namespace AS ns
  ON ns.oid = cls.relnamespace
LEFT JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS cols(attnum, ordinality)
  ON true
LEFT JOIN pg_attribute AS attr
  ON attr.attrelid = cls.oid
 AND attr.attnum = cols.attnum
LEFT JOIN pg_class AS ref_cls
  ON ref_cls.oid = con.confrelid
LEFT JOIN pg_namespace AS ref_ns
  ON ref_ns.oid = ref_cls.relnamespace
WHERE ns.nspname = $1
  AND cls.relname = $2
  AND con.contype IN ('p', 'f', 'u', 'c', 'x')
GROUP BY
    con.oid,
    con.conname,
    con.contype,
    ref_ns.nspname,
    ref_cls.relname
ORDER BY
    CASE con.contype
        WHEN 'p' THEN 1
        WHEN 'u' THEN 2
        WHEN 'f' THEN 3
        WHEN 'c' THEN 4
        WHEN 'x' THEN 5
        ELSE 6
    END,
    con.conname;
"""

INDEX_SQL = """
SELECT
    idx_cls.relname AS index_name,
    idx.indisprimary AS is_primary,
    idx.indisunique AS is_unique,
    pg_size_pretty(pg_relation_size(idx_cls.oid)) AS index_size,
    pg_get_indexdef(idx.indexrelid) AS definition,
    pg_get_expr(idx.indpred, idx.indrelid, true) AS predicate
FROM pg_class AS tbl_cls
JOIN pg_namespace AS ns
  ON ns.oid = tbl_cls.relnamespace
JOIN pg_index AS idx
  ON idx.indrelid = tbl_cls.oid
JOIN pg_class AS idx_cls
  ON idx_cls.oid = idx.indexrelid
WHERE ns.nspname = $1
  AND tbl_cls.relname = $2
ORDER BY idx_cls.relname;
"""

RLS_SQL = """
SELECT
    cls.relname AS table_name,
    cls.relrowsecurity AS row_security_enabled,
    cls.relforcerowsecurity AS row_security_enforced
FROM pg_class AS cls
JOIN pg_namespace AS ns
  ON ns.oid = cls.relnamespace
WHERE ns.nspname = $1
  AND cls.relkind IN ('r', 'p')
ORDER BY cls.relname;
"""

INCOMING_FOREIGN_KEY_SQL = """
SELECT
    src_ns.nspname AS source_schema,
    src_cls.relname AS source_table,
    con.conname AS constraint_name,
    pg_get_constraintdef(con.oid, true) AS definition
FROM pg_constraint AS con
JOIN pg_class AS src_cls
  ON src_cls.oid = con.conrelid
JOIN pg_namespace AS src_ns
  ON src_ns.oid = src_cls.relnamespace
JOIN pg_class AS target_cls
  ON target_cls.oid = con.confrelid
JOIN pg_namespace AS target_ns
  ON target_ns.oid = target_cls.relnamespace
WHERE con.contype = 'f'
  AND target_ns.nspname = $1
  AND target_cls.relname = $2
ORDER BY src_ns.nspname, src_cls.relname, con.conname;
"""

ENUM_SQL = """
SELECT
    typ.typname AS enum_name,
    enum.enumlabel AS enum_label,
    enum.enumsortorder AS enum_sort_order
FROM pg_type AS typ
JOIN pg_namespace AS ns
  ON ns.oid = typ.typnamespace
JOIN pg_enum AS enum
  ON enum.enumtypid = typ.oid
WHERE ns.nspname = $1
ORDER BY typ.typname, enum.enumsortorder;
"""


def _resolve_env_file(environment: str | None = None) -> Path:
    env_name = (environment or os.getenv("ENVIRONMENT", "")).strip().lower()
    if not env_name or env_name in {"development", "dev", "local"}:
        return REPO_ROOT / ".env"
    if env_name == "production":
        return REPO_ROOT / ".env.production"
    return REPO_ROOT / f".env.{env_name}"


def _load_database_url() -> str:
    direct_value = (os.getenv("DATABASE_URL") or "").strip()
    if direct_value:
        return direct_value

    env_file = _resolve_env_file()
    if env_file.exists():
        env_values = dotenv_values(env_file)
        file_value = str(env_values.get("DATABASE_URL") or "").strip()
        if file_value:
            return file_value

    raise RuntimeError(
        "DATABASE_URL is not set. Export DATABASE_URL or add it to the resolved env file."
    )


def _to_asyncpg_dsn(database_url: str) -> str:
    normalized = database_url.strip()
    if normalized.startswith("postgresql+asyncpg://"):
        normalized = f"postgresql://{normalized.split('://', 1)[1]}"

    parts = urlsplit(normalized)
    filtered_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "channel_binding"
    ]

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(filtered_pairs),
            parts.fragment,
        )
    )


def _database_target_summary(database_url: str) -> str:
    parts = urlsplit(database_url)
    database_name = parts.path.lstrip("/") or "(unknown)"
    host = parts.hostname or "(local socket)"
    port = parts.port
    host_label = f"{host}:{port}" if port else host
    return f"{host_label}/{database_name}"


def _markdown_escape(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _render_list(items: list[str]) -> str:
    if not items:
        return "-"
    return ", ".join(f"`{_markdown_escape(item)}`" for item in items)


def _render_optional_code(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return f"`{_markdown_escape(value)}`"


def _load_semantics_markdown(source_path: Path) -> str:
    if not source_path.exists():
        return ""
    return source_path.read_text(encoding="utf-8").strip()


def _render_plain(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return _markdown_escape(value)


async def _fetch_table_payload(
    conn: asyncpg.Connection,
    schema: str,
    table_name: str,
) -> dict[str, Any]:
    columns = await conn.fetch(COLUMN_SQL, schema, table_name)
    constraints = await conn.fetch(CONSTRAINT_SQL, schema, table_name)
    indexes = await conn.fetch(INDEX_SQL, schema, table_name)
    incoming_foreign_keys = await conn.fetch(INCOMING_FOREIGN_KEY_SQL, schema, table_name)
    return {
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "incoming_foreign_keys": incoming_foreign_keys,
    }


async def _fetch_rls_status(conn: asyncpg.Connection, schema: str) -> dict[str, dict[str, bool]]:
    """Fetch RLS status for all tables in schema, returns dict keyed by table_name."""
    rows = await conn.fetch(RLS_SQL, schema)
    return {str(row["table_name"]): {
        "row_security_enabled": bool(row["row_security_enabled"]),
        "row_security_enforced": bool(row["row_security_enforced"]),
    } for row in rows}


def _build_markdown(
    *,
    schema: str,
    database_url: str,
    tables: list[asyncpg.Record],
    table_payloads: dict[str, dict[str, Any]],
    table_rls_status: dict[str, dict[str, bool]],
    enum_rows: list[asyncpg.Record],
    semantics_markdown: str,
) -> str:
    generated_at = (
        datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S") + " 北京时间"
    )
    target_summary = _database_target_summary(database_url)

    lines: list[str] = [
        "# ParlaSoul 数据库文档",
        "",
        "本文档由脚本直接连接 PostgreSQL 实例并基于实时元数据生成。",
        "",
        f"- 生成时间: `{generated_at}`",
        f"- 目标数据库: `{_markdown_escape(target_summary)}`",
        f"- Schema: `{_markdown_escape(schema)}`",
        f"- 表数量: `{len(tables)}`",
        "",
    ]

    if semantics_markdown:
        lines.extend(
            [
                semantics_markdown,
                "",
            ]
        )

    lines.extend(
        [
            "## 实时结构快照",
            "",
        "## 表目录",
        "",
        "| 表名 | 类型 |",
        "| --- | --- |",
        ]
    )

    for table in tables:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"[`{_markdown_escape(table['table_name'])}`](#table-{_markdown_escape(table['table_name']).lower()})",
                    _render_plain(table["table_kind"]),
                ]
            )
            + " |"
        )

    for table in tables:
        table_name = str(table["table_name"])
        payload = table_payloads[table_name]
        columns = payload["columns"]
        constraints = payload["constraints"]
        indexes = payload["indexes"]
        incoming_foreign_keys = payload["incoming_foreign_keys"]
        outgoing_foreign_keys = [
            row for row in constraints if str(row["constraint_type"]) == "FOREIGN KEY"
        ]

        lines.extend(
            [
                "",
                f"## Table `{_markdown_escape(table_name)}`",
                "",
                f'<a id="table-{_markdown_escape(table_name).lower()}"></a>',
                "",
                f"- 类型: `{_markdown_escape(table['table_kind'])}`",
            ]
        )

        rls = table_rls_status.get(table_name)
        if rls:
            lines.append(f"- 行级安全: `{'已启用' if rls['row_security_enabled'] else '未启用'}`{'（强制执行）' if rls['row_security_enforced'] else ''}")

        lines.extend(["", "### 列", ""])
        lines.append("| 字段 | 类型 | 可空 | 默认值 | 额外属性 | 注释 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")

        for column in columns:
            extra_flags: list[str] = []
            if column["identity_kind"]:
                extra_flags.append(f"IDENTITY {column['identity_kind']}")
            if column["generated_kind"]:
                extra_flags.append(f"GENERATED {column['generated_kind']}")

            lines.append(
                "| "
                + " | ".join(
                    [
                        f"`{_markdown_escape(column['column_name'])}`",
                        f"`{_markdown_escape(column['data_type'])}`",
                        "NULL" if column["is_nullable"] else "NOT NULL",
                        _render_plain(column["column_default"]),
                        _render_plain(", ".join(extra_flags)),
                        _render_plain(column["column_comment"]),
                    ]
                )
                + " |"
            )

        lines.extend(["", "### 约束", ""])
        if constraints:
            for constraint in constraints:
                lines.extend(
                    [
                        f"- `{_markdown_escape(constraint['constraint_name'])}` [{_markdown_escape(constraint['constraint_type'])}]",
                        f"  定义: `{_markdown_escape(constraint['definition'])}`",
                    ]
                )
        else:
            lines.append("- 无")

        lines.extend(["", "### 外键出站引用", ""])
        if outgoing_foreign_keys:
            for constraint in outgoing_foreign_keys:
                referenced_target = (
                    f"{constraint['referenced_schema']}.{constraint['referenced_table']}"
                    if constraint["referenced_schema"] and constraint["referenced_table"]
                    else "unknown"
                )
                lines.extend(
                    [
                        f"- `{_markdown_escape(constraint['constraint_name'])}` -> `{_markdown_escape(referenced_target)}`",
                        f"  定义: `{_markdown_escape(constraint['definition'])}`",
                    ]
                )
        else:
            lines.append("- 无")

        lines.extend(["", "### 被其他表引用", ""])
        if incoming_foreign_keys:
            for reference in incoming_foreign_keys:
                source_table = f"{reference['source_schema']}.{reference['source_table']}"
                lines.extend(
                    [
                        f"- `{_markdown_escape(source_table)}` 通过 `{_markdown_escape(reference['constraint_name'])}` 引用本表",
                        f"  定义: `{_markdown_escape(reference['definition'])}`",
                    ]
                )
        else:
            lines.append("- 无")

        lines.extend(["", "### 索引", ""])
        if indexes:
            for index_row in indexes:
                index_tags: list[str] = []
                if index_row["is_primary"]:
                    index_tags.append("PRIMARY")
                if index_row["is_unique"]:
                    index_tags.append("UNIQUE")
                tag_text = f" [{' / '.join(index_tags)}]" if index_tags else ""
                lines.extend(
                    [
                        f"- `{_markdown_escape(index_row['index_name'])}`{tag_text}",
                        f"  大小: `{_markdown_escape(index_row['index_size'])}`",
                        f"  定义: `{_markdown_escape(index_row['definition'])}`",
                    ]
                )
                if index_row["predicate"]:
                    lines.append(f"  谓词: `{_markdown_escape(index_row['predicate'])}`")
        else:
            lines.append("- 无")

    enum_map: dict[str, list[str]] = {}
    for row in enum_rows:
        enum_map.setdefault(str(row["enum_name"]), []).append(str(row["enum_label"]))

    lines.extend(["", "## 枚举类型", ""])
    if enum_map:
        for enum_name in sorted(enum_map):
            labels = ", ".join(f"`{_markdown_escape(label)}`" for label in enum_map[enum_name])
            lines.append(f"- `{_markdown_escape(enum_name)}`: {labels}")
    else:
        lines.append("- 当前 schema 下无自定义枚举类型")

    lines.append("")
    return "\n".join(lines)


async def generate_database_doc(schema: str) -> str:
    database_url = _load_database_url()
    dsn = _to_asyncpg_dsn(database_url)
    semantics_markdown = _load_semantics_markdown(DEFAULT_SEMANTICS_SOURCE)

    conn = await asyncpg.connect(dsn)
    try:
        tables = await conn.fetch(TABLE_OVERVIEW_SQL, schema)
        if not tables:
            raise RuntimeError(f"No tables found in schema '{schema}'.")

        table_payloads: dict[str, dict[str, Any]] = {}
        for table in tables:
            table_name = str(table["table_name"])
            table_payloads[table_name] = await _fetch_table_payload(conn, schema, table_name)

        enum_rows = await conn.fetch(ENUM_SQL, schema)
        table_rls_status = await _fetch_rls_status(conn, schema)
        markdown = _build_markdown(
            schema=schema,
            database_url=database_url,
            tables=tables,
            table_payloads=table_payloads,
            table_rls_status=table_rls_status,
            enum_rows=enum_rows,
            semantics_markdown=semantics_markdown,
        )
    finally:
        await conn.close()

    return markdown


def _normalize_output_paths(raw_outputs: list[str] | None) -> list[Path]:
    if raw_outputs:
        candidates = raw_outputs
    else:
        candidates = [str(path) for path in DEFAULT_OUTPUTS]

    resolved_paths: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        output_path = Path(candidate).expanduser()
        if not output_path.is_absolute():
            output_path = (REPO_ROOT / output_path).resolve()
        if output_path in seen:
            continue
        seen.add(output_path)
        resolved_paths.append(output_path)
    return resolved_paths


def _write_outputs(markdown: str, output_paths: list[Path]) -> list[Path]:
    for output_path in output_paths:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
    return output_paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a Markdown database reference from a live PostgreSQL database."
    )
    parser.add_argument(
        "--schema",
        default=DEFAULT_SCHEMA,
        help=f"Schema name to inspect. Defaults to {DEFAULT_SCHEMA!r}.",
    )
    parser.add_argument(
        "--output",
        action="append",
        help=(
            "Output Markdown path. Can be provided multiple times. "
            "Defaults to backend/ frontend .agent/dataschema.md."
        ),
    )
    return parser.parse_args()


async def _main() -> int:
    args = parse_args()
    output_paths = _normalize_output_paths(args.output)
    markdown = await generate_database_doc(schema=args.schema)
    generated_paths = _write_outputs(markdown, output_paths)
    for generated_path in generated_paths:
        print(f"Database document generated at: {generated_path}")
    return 0


def main() -> int:
    try:
        return asyncio.run(_main())
    except Exception as exc:  # pragma: no cover - CLI error path
        print(f"Failed to generate database document: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
