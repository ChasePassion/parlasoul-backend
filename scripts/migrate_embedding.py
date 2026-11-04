"""Embedding provider migration: re-embed all memories with SiliconFlow.

Usage:
    python scripts/migrate_embedding.py              # full migration
    python scripts/migrate_embedding.py --dry-run     # export + embed only, no write-back
    python scripts/migrate_embedding.py --scope user_id char_id  # single scope
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path so src.* imports work.
# Try script-relative first (works when script is inside the project),
# then fall back to CWD (works when run from project root via Docker).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _PROJECT_ROOT / "src"
if not _SRC_DIR.is_dir():
    _PROJECT_ROOT = Path.cwd()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from openai import OpenAI
from pymilvus import MilvusClient

# Trigger pydantic-settings to load .env
from src.core.config import settings  # noqa: E402

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("migrate_embedding")

# ── Target embedding provider ──
# Reuse the standard embedding settings so the migration always writes vectors
# into the same embedding space the application reads at runtime.
NEW_API_KEY = settings.EMBEDDING_API_KEY
NEW_BASE_URL = settings.EMBEDDING_BASE_URL
NEW_MODEL = settings.EMBEDDING_MODEL
DIMS = settings.MEMORY_EMBEDDING_DIM

# ── Batch sizes ──
EMBEDDING_BATCH_SIZE = 20
INSERT_BATCH_SIZE = 100
EXPORT_LIMIT = 10000
RATE_LIMIT_DELAY = 0.1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate embeddings to SiliconFlow")
    parser.add_argument("--dry-run", action="store_true", help="Export and embed only, skip write-back")
    parser.add_argument("--scope", nargs=2, metavar=("USER_ID", "CHAR_ID"), help="Migrate a single scope only")
    return parser.parse_args()


def get_milvus_client() -> MilvusClient:
    uri = settings.MILVUS_URL
    token = settings.MILVUS_TOKEN
    logger.info("Connecting to Milvus: %s", uri)
    return MilvusClient(uri=uri, token=token)


def get_collection_names() -> tuple[str, str]:
    return settings.MEMORY_COLLECTION_NAME, settings.MEMORY_GROUP_COLLECTION_NAME


def export_memories(
    client: MilvusClient,
    collection: str,
    scope: tuple[str, str] | None,
) -> list[dict[str, Any]]:
    """Export all memory records, excluding the vector field.

    Exports per-scope to avoid pymilvus offset+limit ceiling (16384).
    """
    output_fields = [
        "id", "user_id", "character_id", "memory_type",
        "ts", "chat_id", "text", "group_id",
    ]

    if scope:
        scopes = [scope]
    else:
        # Discover all distinct (user_id, character_id) pairs
        raw = client.query(
            collection_name=collection,
            filter="id >= 0",
            output_fields=["user_id", "character_id"],
            limit=EXPORT_LIMIT,
        )
        seen: set[tuple[str, str]] = set()
        for r in raw:
            uid = str(r.get("user_id", ""))
            cid = str(r.get("character_id", ""))
            if uid and cid:
                seen.add((uid, cid))
        scopes = sorted(seen)
        logger.info("Discovered %d scope(s): %s", len(scopes), scopes)

    all_records: list[dict[str, Any]] = []
    for user_id, char_id in scopes:
        filter_expr = f'user_id == "{user_id}" && character_id == "{char_id}"'
        batch = client.query(
            collection_name=collection,
            filter=filter_expr,
            output_fields=output_fields,
            limit=EXPORT_LIMIT,
        )
        all_records.extend(batch)
        logger.info("Scope (%s, %s): %d records", user_id, char_id, len(batch))

    logger.info("Export complete: %d total records across %d scope(s)", len(all_records), len(scopes))
    return all_records


def backup_records(records: list[dict[str, Any]], tag: str = "") -> str:
    backup_dir = "/tmp" if os.access("/tmp", os.W_OK) else "tmp"
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    name = f"{backup_dir}/memory_backup_{ts}{'_' + tag if tag else ''}.json"
    serializable = []
    for r in records:
        item = {}
        for k, v in r.items():
            if hasattr(v, "item"):  # numpy types
                item[k] = v.item()
            else:
                item[k] = v
        serializable.append(item)
    with open(name, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, default=str)
    logger.info("Backup saved: %s (%d records)", name, len(serializable))
    return name


def re_embed(
    client: OpenAI,
    records: list[dict[str, Any]],
) -> list[list[float]]:
    """Re-embed all records using the new provider."""
    texts = [r["text"] for r in records]
    new_vectors: list[list[float]] = []
    total = len(texts)

    for i in range(0, total, EMBEDDING_BATCH_SIZE):
        batch = texts[i : i + EMBEDDING_BATCH_SIZE]
        try:
            resp = client.embeddings.create(
                model=NEW_MODEL,
                input=batch,
                dimensions=DIMS,
            )
            new_vectors.extend([item.embedding for item in resp.data])
        except Exception:
            logger.exception("Embedding batch %d-%d failed", i, min(i + EMBEDDING_BATCH_SIZE, total))
            raise

        done = min(i + EMBEDDING_BATCH_SIZE, total)
        if done % (EMBEDDING_BATCH_SIZE * 5) == 0 or done == total:
            logger.info("Embedded %d/%d", done, total)
        time.sleep(RATE_LIMIT_DELAY)

    logger.info("Re-embedding complete: %d vectors", len(new_vectors))
    return new_vectors


def clear_collection_data(
    client: MilvusClient,
    collection: str,
    scopes: set[tuple[str, str]],
) -> None:
    """Delete all records for the given scopes from a collection."""
    for user_id, char_id in scopes:
        filter_expr = f'user_id == "{user_id}" && character_id == "{char_id}"'
        try:
            result = client.delete(collection_name=collection, filter=filter_expr)
            count = result.get("delete_count", 0) if isinstance(result, dict) else 0
            logger.info("Deleted %s records from '%s' for scope (%s, %s)", count, collection, user_id, char_id)
        except Exception:
            logger.exception("Failed to delete from '%s' for scope (%s, %s)", collection, user_id, char_id)
            raise


def insert_records(
    client: MilvusClient,
    collection: str,
    records: list[dict[str, Any]],
) -> None:
    """Batch insert records with new vectors."""
    total = len(records)
    for i in range(0, total, INSERT_BATCH_SIZE):
        batch = records[i : i + INSERT_BATCH_SIZE]
        try:
            client.insert(collection_name=collection, data=batch)
        except Exception:
            logger.exception("Insert batch %d-%d failed", i, min(i + INSERT_BATCH_SIZE, total))
            raise
        done = min(i + INSERT_BATCH_SIZE, total)
        if done % (INSERT_BATCH_SIZE * 10) == 0 or done == total:
            logger.info("Inserted %d/%d", done, total)

    client.flush(collection_name=collection)
    logger.info("Insert and flush complete: %d records", total)


def verify(
    client: MilvusClient,
    collection: str,
    expected_count: int,
    scopes: set[tuple[str, str]],
) -> bool:
    """Verify record count and vector dimension after migration."""
    ok = True
    for user_id, char_id in scopes:
        filter_expr = f'user_id == "{user_id}" && character_id == "{char_id}"'
        results = client.query(
            collection_name=collection,
            filter=filter_expr,
            output_fields=["id", "vector"],
            limit=1,
        )
        if not results:
            logger.error("VERIFY FAIL: no records found for scope (%s, %s)", user_id, char_id)
            ok = False
            continue
        sample = results[0]
        vec = sample.get("vector", [])
        if len(vec) != DIMS:
            logger.error(
                "VERIFY FAIL: vector dim mismatch for record %s: got %d, expected %d",
                sample["id"],
                len(vec),
                DIMS,
            )
            ok = False
        else:
            logger.info(
                "VERIFY OK: scope (%s, %s) sample id=%s dim=%d",
                user_id, char_id, sample["id"], len(vec),
            )

    # Quick count check
    for user_id, char_id in scopes:
        filter_expr = f'user_id == "{user_id}" && character_id == "{char_id}"'
        results = client.query(
            collection_name=collection,
            filter=filter_expr,
            output_fields=["id"],
            limit=16384,
        )
        actual = len(results)
        logger.info("Scope (%s, %s): %d records", user_id, char_id, actual)

    return ok


def main() -> None:
    args = parse_args()

    # 1. Connect
    milvus_client = get_milvus_client()
    embedding_client = OpenAI(
        api_key=NEW_API_KEY,
        base_url=NEW_BASE_URL,
        timeout=30.0,
    )
    mem_collection, grp_collection = get_collection_names()
    logger.info("Memories collection: %s", mem_collection)
    logger.info("Groups collection: %s", grp_collection)

    # Verify collection exists
    if not milvus_client.has_collection(mem_collection):
        logger.error("Collection '%s' does not exist", mem_collection)
        sys.exit(1)

    # 2. Export
    scope = tuple(args.scope) if args.scope else None
    records = export_memories(milvus_client, mem_collection, scope)
    if not records:
        logger.warning("No records to migrate")
        return

    # 3. Backup
    backup_records(records)

    # 4. Re-embed
    logger.info("Starting re-embedding with %s (%d dimensions)", NEW_MODEL, DIMS)
    new_vectors = re_embed(embedding_client, records)

    if len(new_vectors) != len(records):
        logger.error("Vector count mismatch: %d vs %d records", len(new_vectors), len(records))
        sys.exit(1)

    # Assign new vectors to records
    for i, r in enumerate(records):
        r["vector"] = new_vectors[i]
        r.pop("id", None)

    if args.dry_run:
        logger.info("DRY RUN complete - %d records embedded, no data modified", len(records))
        return

    # 5. Collect unique scopes
    scopes: set[tuple[str, str]] = set()
    for r in records:
        uid = str(r.get("user_id", ""))
        cid = str(r.get("character_id", ""))
        if uid and cid:
            scopes.add((uid, cid))
    logger.info("Scopes to migrate: %d", len(scopes))

    # 6. Clear old data
    logger.info("Clearing old data from '%s'...", mem_collection)
    clear_collection_data(milvus_client, mem_collection, scopes)

    # 7. Clear groups (centroids invalidated by new embedding space)
    if milvus_client.has_collection(grp_collection):
        logger.info("Clearing groups collection '%s'...", grp_collection)
        for user_id, char_id in scopes:
            filter_expr = f'user_id == "{user_id}" && character_id == "{char_id}"'
            try:
                milvus_client.delete(collection_name=grp_collection, filter=filter_expr)
            except Exception:
                logger.warning("Failed to clear groups for scope (%s, %s), continuing", user_id, char_id)

    # 8. Re-insert
    logger.info("Inserting %d records with new vectors...", len(records))
    insert_records(milvus_client, mem_collection, records)

    # 9. Verify
    logger.info("Verifying...")
    ok = verify(milvus_client, mem_collection, len(records), scopes)
    if ok:
        logger.info("Migration completed successfully")
    else:
        logger.error("Migration completed with verification errors - check logs")
        sys.exit(1)


if __name__ == "__main__":
    main()
