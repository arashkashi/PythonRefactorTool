"""Find the Python files of a project folder."""
from __future__ import annotations

from pathlib import Path
from typing import List

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", ".env", "__pycache__", "node_modules",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "build", "dist",
    "site-packages", ".eggs", ".idea", ".vscode",
}


def discover(root: Path) -> List[Path]:
    """All ``*.py`` files under ``root`` (or ``[root]`` for a file), skipping tooling/vendor dirs."""
    if root.is_file():
        return [root]
    found: List[Path] = []
    for path in sorted(root.rglob("*.py")):
        rel_parts = path.relative_to(root).parts[:-1]
        if any(p in SKIP_DIRS or p.endswith(".egg-info") or (p.startswith(".") and p != ".") for p in rel_parts):
            continue
        found.append(path)
    return found


def line_count(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0
