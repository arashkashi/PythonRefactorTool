"""Backward slice of a block: only the statements that determine a returned value.

Used to explain *how* an extracted function computes what it returns. Starting from the
statements that bind (or change in place) the returned name, it adds, until nothing changes:

* the statements that bind names those statements read (data dependencies);
* the enclosing ``for``/``while``/``if``/``with``/``try`` headers (control dependencies);
* ``if ...: continue`` / ``break`` guards inside the loops involved, since they decide which
  iterations reach the update.

Everything else (logging, other results, side effects) is dropped; a branch that keeps nothing
becomes ``...``. The result is real Python taken from the block, so it never paraphrases wrongly.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Set

import libcst as cst

MUTATING = {
    "append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse", "add", "discard", "update",
    "setdefault", "popitem", "appendleft", "extendleft", "popleft",
}
_CODE = cst.Module([])
_COMPOUND = (cst.If, cst.For, cst.While, cst.With, cst.Try, cst.Else, cst.ExceptHandler, cst.Finally)


def _target_names(t: cst.CSTNode) -> Set[str]:
    if isinstance(t, cst.Name):
        return {t.value}
    if isinstance(t, (cst.Tuple, cst.List)):
        return set().union(*(_target_names(e.value) for e in t.elements)) if t.elements else set()
    if isinstance(t, cst.StarredElement):
        return _target_names(t.value)
    if isinstance(t, (cst.Subscript, cst.Attribute)):  # x[k] = v / x.a = v change x
        base = t.value
        while isinstance(base, (cst.Subscript, cst.Attribute)):
            base = base.value
        return {base.value} if isinstance(base, cst.Name) else set()
    return set()


class _Reads(cst.CSTVisitor):
    def __init__(self) -> None:
        self.names: Set[str] = set()

    def visit_Name(self, node: cst.Name) -> None:
        self.names.add(node.value)

    def visit_Attribute(self, node: cst.Attribute) -> bool:
        node.value.visit(self)  # `x.attr`: reads x, not a variable called `attr`
        return False


def _reads(node: Optional[cst.CSTNode]) -> Set[str]:
    if node is None:
        return set()
    v = _Reads()
    node.visit(v)
    return v.names


def _effects(line: cst.SimpleStatementLine) -> Set[str]:
    """Names a simple statement binds or changes in place."""
    out: Set[str] = set()
    for small in line.body:
        if isinstance(small, cst.Assign):
            for t in small.targets:
                out |= _target_names(t.target)
        elif isinstance(small, (cst.AugAssign, cst.AnnAssign)):
            out |= _target_names(small.target)
        elif isinstance(small, cst.Expr) and isinstance(small.value, cst.Call):
            f = small.value.func
            if isinstance(f, cst.Attribute) and f.attr.value in MUTATING:
                out |= _target_names(f.value) if isinstance(f.value, (cst.Name, cst.Subscript, cst.Attribute)) else set()
        elif isinstance(small, cst.Del):
            out |= _target_names(small.target)
    return out


def _line_reads(line: cst.SimpleStatementLine) -> Set[str]:
    out: Set[str] = set()
    for small in line.body:
        if isinstance(small, cst.Assign):
            out |= _reads(small.value)
            for t in small.targets:  # x[k] = v reads k (and x)
                if not isinstance(t.target, cst.Name):
                    out |= _reads(t.target)
        elif isinstance(small, cst.AugAssign):
            out |= _reads(small.value) | _reads(small.target)
        elif isinstance(small, cst.AnnAssign):
            out |= _reads(small.value)
        else:
            out |= _reads(small)
    return out


def _header_reads(node: cst.CSTNode) -> Set[str]:
    if isinstance(node, cst.For):
        return _reads(node.iter)
    if isinstance(node, (cst.If, cst.While)):
        return _reads(node.test)
    if isinstance(node, cst.With):
        return set().union(*(_reads(i.item) for i in node.items))
    return set()


def _jumps(line: cst.SimpleStatementLine) -> bool:
    return any(isinstance(s, (cst.Continue, cst.Break)) for s in line.body)


def backward_slice(stmts: Sequence[cst.BaseStatement], parents: Dict, names: Sequence[str],
                   max_lines: int = 20) -> Optional[str]:
    """Code of ``stmts`` reduced to what determines ``names``; None if it would be too long or empty."""
    roots = {id(s) for s in stmts}
    lines: List[cst.SimpleStatementLine] = []
    loops: List[cst.CSTNode] = []

    def walk(node: cst.CSTNode) -> None:
        if isinstance(node, (cst.FunctionDef, cst.ClassDef)):
            return
        if isinstance(node, cst.SimpleStatementLine):
            lines.append(node)
            return
        if isinstance(node, (cst.For, cst.While)):
            loops.append(node)
        for child in node.children:
            walk(child)

    for s in stmts:
        walk(s)

    def ancestors(node: cst.CSTNode) -> List[cst.CSTNode]:
        out = []
        cur = node
        while id(cur) not in roots:
            cur = parents.get(cur)
            if cur is None:
                break
            out.append(cur)
        return out

    needed: Set[str] = set(names)
    keep: Set[int] = set()  # ids of kept simple lines and loop headers that bind a needed name
    changed = True
    while changed:
        changed = False
        for line in lines:
            if id(line) in keep or not (_effects(line) & needed):
                continue
            keep.add(id(line))
            needed |= _line_reads(line)
            for anc in ancestors(line):
                needed |= _header_reads(anc)
            changed = True
        for loop in loops:  # `for x in xs:` binds x
            if id(loop) not in keep and isinstance(loop, cst.For) and _target_names(loop.target) & needed:
                keep.add(id(loop))
                needed |= _reads(loop.iter)
                for anc in ancestors(loop):
                    needed |= _header_reads(anc)
                changed = True
        # continue/break guards in loops that enclose something kept decide which iterations count
        kept_ancestors = {id(a) for line in lines if id(line) in keep for a in ancestors(line)}
        for line in lines:
            if id(line) in keep or not _jumps(line):
                continue
            loop = next((a for a in ancestors(line) if isinstance(a, (cst.For, cst.While))), None)
            if loop is not None and (id(loop) in kept_ancestors or id(loop) in keep):
                keep.add(id(line))
                for anc in ancestors(line):
                    needed |= _header_reads(anc)
                changed = True

    kept_lines = [line for line in lines if id(line) in keep]
    kept_loops = [loop for loop in loops if id(loop) in keep]
    if not kept_lines and not kept_loops:
        return None
    alive: Set[int] = set(keep)
    for node in kept_lines + kept_loops:
        alive |= {id(a) for a in ancestors(node)}

    class Pruner(cst.CSTTransformer):
        def on_leave(self, original, updated):
            removable = isinstance(original, (cst.SimpleStatementLine, cst.BaseCompoundStatement, cst.ExceptHandler))
            if removable and id(original) not in alive:
                return cst.RemoveFromParent()
            result = super().on_leave(original, updated)  # dispatches to leave_If etc.
            if isinstance(result, cst.IndentedBlock) and not result.body:
                return result.with_changes(body=[cst.SimpleStatementLine([cst.Expr(cst.Ellipsis())])])
            if isinstance(result, (cst.SimpleStatementLine, cst.BaseCompoundStatement)):
                return result.with_changes(leading_lines=[])  # comments of dropped context would mislead
            return result

        def leave_If(self, original, updated):
            return self._drop_dead_else(original, updated)

        def leave_For(self, original, updated):
            return self._drop_dead_else(original, updated)

        def leave_While(self, original, updated):
            return self._drop_dead_else(original, updated)

        def leave_Try(self, original, updated):
            updated = self._drop_dead_else(original, updated)
            if original.finalbody is not None and id(original.finalbody) not in alive:
                updated = updated.with_changes(finalbody=None)
            if not updated.handlers and updated.finalbody is None:
                return cst.FlattenSentinel(list(updated.body.body))  # only the try body matters
            return updated

        @staticmethod
        def _drop_dead_else(original, updated):
            if original.orelse is not None and id(original.orelse) not in alive:
                return updated.with_changes(orelse=None)
            return updated

    out: List[str] = []
    for s in stmts:
        if id(s) not in alive:
            continue
        pruned = cst.Module(body=[s]).visit(Pruner())  # a Module lets a top-level node be flattened
        out.append(pruned.code)
    code = "".join(out).rstrip("\n")
    if not code or code.count("\n") + 1 > max_lines:
        return None
    return code
