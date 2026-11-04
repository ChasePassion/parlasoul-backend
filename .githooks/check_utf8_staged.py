#!/usr/bin/env python3
"""Fail commit when staged text files are not valid UTF-8."""

from __future__ import annotations

import os
import subprocess
import sys

TEXT_EXTENSIONS = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".cfg",
    ".env",
    ".sh",
    ".ps1",
    ".sql",
}

TEXT_FILENAMES = {
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
    "agents.md",
}


def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        check=False,
        text=True,
        capture_output=True,
    )


def is_text_candidate(path: str) -> bool:
    lower = path.lower()
    filename = os.path.basename(lower)
    if filename in TEXT_FILENAMES:
        return True
    _, ext = os.path.splitext(filename)
    return ext in TEXT_EXTENSIONS


def staged_paths() -> list[str]:
    result = run_git(["diff", "--cached", "--name-only", "--diff-filter=ACMR"])
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def staged_blob_bytes(path: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f":{path}"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return b""
    return result.stdout


def main() -> int:
    invalid_encoding: list[str] = []
    invalid_bom: list[str] = []
    for path in staged_paths():
        if not is_text_candidate(path):
            continue
        blob = staged_blob_bytes(path)
        if b"\x00" in blob:
            continue
        if blob.startswith(b"\xef\xbb\xbf"):
            invalid_bom.append(path)
        try:
            blob.decode("utf-8")
        except UnicodeDecodeError:
            invalid_encoding.append(path)

    if not invalid_encoding and not invalid_bom:
        return 0

    print("Commit blocked: staged text files must be UTF-8 without BOM.", file=sys.stderr)
    if invalid_encoding:
        print("Invalid UTF-8 encoding:", file=sys.stderr)
        for path in invalid_encoding:
            print(f"  - {path}", file=sys.stderr)
    if invalid_bom:
        print("UTF-8 BOM is not allowed:", file=sys.stderr)
        for path in invalid_bom:
            print(f"  - {path}", file=sys.stderr)
    print("Please convert files to UTF-8 (no BOM), then stage again.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
