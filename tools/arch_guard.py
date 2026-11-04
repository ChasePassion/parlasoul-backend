"""Architecture guard checks for thin-router rules."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTERS_DIR = ROOT / "src" / "api" / "routers"

FORBIDDEN_PATTERNS = [
    re.compile(r"Depends\(\s*get_db\s*\)"),
    re.compile(r"\bAsyncSession\b"),
    re.compile(r"\brun_blocking\("),
]


def main() -> int:
    violations: list[str] = []
    for path in sorted(ROUTERS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.search(text):
                violations.append(f"{path.relative_to(ROOT)}: forbidden pattern `{pattern.pattern}`")

    if violations:
        print("Architecture guard failed:")
        for item in violations:
            print(f"- {item}")
        return 1

    print("Architecture guard passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

