"""Build a high-level, size-annotated outline of a Python module.

Levels, from coarse to fine:
    module -> class -> function / method -> block (if/for/while/with/try/match)

Blocks are the extraction candidates. They nest (a ``for`` inside an ``if``),
but only down to ``max_block_depth`` levels and only when they are at least
``min_block_lines`` long, so the tree stays at a refactoring-friendly level of
abstraction instead of mirroring every statement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider

COMPOUND_TYPES = tuple(
    t
    for t in (
        getattr(cst, "If", None),
        getattr(cst, "For", None),
        getattr(cst, "While", None),
        getattr(cst, "With", None),
        getattr(cst, "Try", None),
        getattr(cst, "TryStar", None),
        getattr(cst, "Match", None),
    )
    if t is not None
)


@dataclass
class TreeNode:
    kind: str  # "module" | "class" | "function" | "method" | "block" | "code"
    name: str
    qualname: str
    start: int
    end: int
    header: str = ""
    children: List["TreeNode"] = field(default_factory=list)
    parent: Optional["TreeNode"] = field(default=None, repr=False)

    @property
    def loc(self) -> int:
        return self.end - self.start + 1

    @property
    def extractable(self) -> bool:
        return self.kind == "block"

    def walk(self) -> Iterator["TreeNode"]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class SizeScale:
    """Percentile thresholds used to colour nodes relative to *this* file."""

    p50: int
    p75: int
    p90: int
    max_loc: int

    def level(self, loc: int) -> int:
        """0 = small, 1 = medium, 2 = large, 3 = refactor me."""
        if loc >= self.p90:
            return 3
        if loc >= self.p75:
            return 2
        if loc >= self.p50:
            return 1
        return 0


def _percentile(sorted_values: List[int], pct: float) -> int:
    if not sorted_values:
        return 1
    idx = min(len(sorted_values) - 1, int(round(pct * (len(sorted_values) - 1))))
    return sorted_values[idx]


def size_scale(root: TreeNode) -> SizeScale:
    sizes = sorted(n.loc for n in root.walk() if n.kind in ("function", "method"))
    if not sizes:
        sizes = [max(1, root.loc)]
    return SizeScale(
        p50=max(2, _percentile(sizes, 0.50)),
        p75=max(3, _percentile(sizes, 0.75)),
        p90=max(4, _percentile(sizes, 0.90)),
        max_loc=max(sizes),
    )


def _block_bodies(node: cst.CSTNode) -> Iterator[cst.BaseSuite]:
    """Yield every suite owned by a compound statement (body, elif, else, except, finally, case)."""
    for child in node.children:
        if isinstance(child, (cst.IndentedBlock, cst.SimpleStatementSuite)):
            yield child
        elif isinstance(child, (cst.FunctionDef, cst.ClassDef, cst.BaseSmallStatement, cst.BaseExpression)):
            continue
        else:  # Else, ExceptHandler, Finally, nested If (elif), MatchCase, ...
            yield from _block_bodies(child)


def _block_name(stmt: cst.CSTNode) -> str:
    return {
        "If": "if",
        "For": "for",
        "While": "while",
        "With": "with",
        "Try": "try",
        "TryStar": "try",
        "Match": "match",
    }.get(type(stmt).__name__, type(stmt).__name__.lower())


class _Builder:
    def __init__(self, source: str, max_block_depth: int, min_block_lines: int):
        self.lines = source.splitlines()
        self.module = cst.parse_module(source)
        self.wrapper = MetadataWrapper(self.module, unsafe_skip_copy=True)
        self.pos = self.wrapper.resolve(PositionProvider)
        self.max_block_depth = max_block_depth
        self.min_block_lines = min_block_lines

    def span(self, node: cst.CSTNode):
        p = self.pos[node]
        return p.start.line, p.end.line

    def header(self, line: int) -> str:
        if 1 <= line <= len(self.lines):
            return self.lines[line - 1].strip()
        return ""

    def build(self, name: str) -> TreeNode:
        root = TreeNode("module", name, name, 1, max(1, len(self.lines)))
        self._walk(self.module.body, root, prefix="", in_function=False, depth=0)
        return root

    def _add(self, parent: TreeNode, node: TreeNode) -> TreeNode:
        node.parent = parent
        parent.children.append(node)
        return node

    def _walk(self, stmts, parent: TreeNode, prefix: str, in_function: bool, depth: int) -> None:
        for stmt in stmts:
            start, end = self.span(stmt)
            if isinstance(stmt, cst.FunctionDef):
                kind = "method" if parent.kind == "class" else "function"
                qual = prefix + stmt.name.value
                fn = self._add(parent, TreeNode(kind, stmt.name.value, qual, start, end, self.header(start)))
                self._walk(stmt.body.body, fn, qual + ".", in_function=True, depth=0)
            elif isinstance(stmt, cst.ClassDef):
                qual = prefix + stmt.name.value
                cls = self._add(parent, TreeNode("class", stmt.name.value, qual, start, end, self.header(start)))
                self._walk(stmt.body.body, cls, qual + ".", in_function=False, depth=0)
            elif isinstance(stmt, COMPOUND_TYPES):
                if in_function:
                    if end - start + 1 < self.min_block_lines:
                        continue
                    blk = self._add(
                        parent,
                        TreeNode("block", _block_name(stmt), parent.qualname, start, end, self.header(start)),
                    )
                    if depth + 1 < self.max_block_depth:
                        for suite in _block_bodies(stmt):
                            if isinstance(suite, cst.IndentedBlock):
                                self._walk(suite.body, blk, prefix, in_function=True, depth=depth + 1)
                else:
                    # Module/class level control flow (e.g. ``if __name__ == "__main__"``):
                    # shown for orientation, but not an extraction target.
                    code = self._add(parent, TreeNode("code", _block_name(stmt), parent.qualname, start, end, self.header(start)))
                    for suite in _block_bodies(stmt):
                        if isinstance(suite, cst.IndentedBlock):
                            self._walk(suite.body, code, prefix, in_function=False, depth=depth)


def build_tree(source: str, name: str = "<module>", max_block_depth: int = 3, min_block_lines: int = 5) -> TreeNode:
    return _Builder(source, max_block_depth, min_block_lines).build(name)


def largest(root: TreeNode, kinds=("function", "method"), n: int = 10) -> List[TreeNode]:
    return sorted((x for x in root.walk() if x.kind in kinds), key=lambda x: -x.loc)[:n]


def node_index(root: TreeNode) -> Dict[int, TreeNode]:
    return {id(n): n for n in root.walk()}
