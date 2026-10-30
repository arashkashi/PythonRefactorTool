"""Rich labels for tree nodes: icon, name, size bar and line count, coloured by size.

A terminal cannot change font size per line, so "bigger" is shown three ways:
a heat colour (relative to this file's percentiles), a proportional bar, and
bold/reverse emphasis for the top 10%.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from rich.text import Text

from .analyzer import SizeScale, TreeNode

HEAT = ["green", "yellow", "dark_orange", "bold red"]
ICONS = {"module": "◆", "class": "class", "function": "def", "method": "def", "block": "▸", "code": "·"}
KIND_STYLE = {"class": "bold magenta", "function": "bold cyan", "method": "cyan", "block": "", "code": "dim"}
BAR_WIDTH = 24


def size_bar(loc: int, scale: SizeScale) -> str:
    filled = max(1, round(BAR_WIDTH * min(loc, scale.max_loc) / max(1, scale.max_loc)))
    return "█" * filled


def file_label(name: str, loc: int, scale: SizeScale, rank: Optional[int] = None, note: str = "") -> Text:
    """Label for a file in project mode, scaled against the other files."""
    heat = HEAT[scale.level(loc)]
    t = Text()
    t.append("▤ ", style="bold blue")
    t.append(name, style="bold reverse" if scale.level(loc) == 3 else "bold")
    t.append("  ")
    t.append(size_bar(loc, scale), style=heat)
    t.append(f" {loc}", style=heat)
    if rank is not None:
        t.append(f"  ◀ #{rank}", style="bold red")
    if note:
        t.append(f"  {note}", style="dim italic")
    return t


def node_label(node: TreeNode, scale: SizeScale, show_bar: bool = True, rank: Optional[int] = None) -> Text:
    level = scale.level(node.loc)
    heat = HEAT[level]
    t = Text()
    t.append(f"{ICONS.get(node.kind, '?')} ", style=KIND_STYLE.get(node.kind, ""))
    if node.kind == "module":
        t.append(node.name, style="bold")
        t.append(f"  {node.loc} lines", style="dim")
        return t
    if node.kind in ("block", "code"):
        header = node.header if len(node.header) <= 60 else node.header[:57] + "..."
        t.append(header, style="italic" if node.kind == "block" else "dim italic")
    else:
        name_style = KIND_STYLE.get(node.kind, "")
        if level == 3:
            name_style += " reverse"
        t.append(node.name, style=name_style)
    t.append("  ")
    if show_bar:
        t.append(size_bar(node.loc, scale), style=heat)
        t.append(" ")
    t.append(f"{node.loc}", style=heat)
    t.append(f" L{node.start}-{node.end}", style="dim")
    if rank is not None:
        t.append(f"  ◀ #{rank} largest", style="bold red")
    return t


def legend(scale: SizeScale) -> Text:
    t = Text("size: ")
    t.append(f"■ <{scale.p50} ", style=HEAT[0])
    t.append(f"■ {scale.p50}+ ", style=HEAT[1])
    t.append(f"■ {scale.p75}+ ", style=HEAT[2])
    t.append(f"■ {scale.p90}+ lines (top 10%)", style=HEAT[3])
    return t


def apply_to_disk(path: Path, new_source: str) -> Path:
    """Write ``new_source`` to ``path`` after saving a numbered backup. Returns the backup path."""
    backup: Optional[Path] = None
    n = 1
    while backup is None or backup.exists():
        backup = path.with_name(f"{path.name}.bak{n if n > 1 else ''}")
        n += 1
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(new_source, encoding="utf-8")
    return backup


def parse_range(text: str) -> Tuple[int, int]:
    """'120-180' or '120:180' or '120' -> (120, 180)."""
    text = text.strip().replace(":", "-")
    parts = [p for p in text.split("-") if p.strip()]
    if not parts or len(parts) > 2:
        raise ValueError(f"expected a line range like 120-180, got {text!r}")
    a = int(parts[0])
    b = int(parts[1]) if len(parts) == 2 else a
    return (a, b) if a <= b else (b, a)
