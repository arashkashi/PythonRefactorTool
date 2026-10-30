"""Extract Function / Extract Method, implemented with LibCST scope analysis.

Pipeline:
    analyze(source, start, end)  -> ExtractPlan  (what would be extracted; why not, if refused)
    extract(source, start, end, name, renames) -> ExtractResult (new source + diff)

Data flow rules (per name local to the enclosing function):
    parameter  = read inside the selection and bound before it (or loop-carried),
                 or conditionally rebound inside it while holding an earlier value
    return     = bound inside the selection and read after it (or loop-carried)
Globals, builtins and imports at module level are left alone: the new function
lives in the same module, so it sees them too.
"""
from __future__ import annotations

import difflib
import keyword
import re
import textwrap
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

import libcst as cst
from libcst.metadata import (
    Assignment,
    MetadataWrapper,
    ParentNodeProvider,
    PositionProvider,
    ScopeProvider,
)

from .slicing import backward_slice
from .typeinfer import TypeGuess, TypeInferrer, describe, returns_type


class RefactorError(Exception):
    """The requested refactoring cannot be performed safely."""


# --------------------------------------------------------------------------- selection


@dataclass
class _FuncCtx:
    func: cst.FunctionDef
    class_node: Optional[cst.ClassDef]
    class_qualname: str
    qualname: str
    nested: bool


class _FuncCollector(cst.CSTVisitor):
    def __init__(self) -> None:
        self.stack: List[cst.CSTNode] = []
        self.found: List[_FuncCtx] = []

    def _qual(self, name: str) -> str:
        return ".".join([n.name.value for n in self.stack] + [name])

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self.stack.append(node)

    def leave_ClassDef(self, original_node: cst.ClassDef) -> None:
        self.stack.pop()

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        parent = self.stack[-1] if self.stack else None
        cls = parent if isinstance(parent, cst.ClassDef) else None
        cls_qual = ".".join(n.name.value for n in self.stack) if cls else ""
        nested = any(isinstance(n, cst.FunctionDef) for n in self.stack)
        self.found.append(_FuncCtx(node, cls, cls_qual, self._qual(node.name.value), nested))
        self.stack.append(node)

    def leave_FunctionDef(self, original_node: cst.FunctionDef) -> None:
        self.stack.pop()


@dataclass
class _Body:
    block: cst.IndentedBlock
    depth: int
    loops: List[cst.CSTNode]


def _function_bodies(func: cst.FunctionDef) -> List[_Body]:
    """Every statement list inside ``func`` (not entering nested defs/classes)."""
    out: List[_Body] = []

    def suites(node: cst.CSTNode):
        for child in node.children:
            if isinstance(child, cst.IndentedBlock):
                yield child
            elif isinstance(child, (cst.FunctionDef, cst.ClassDef, cst.BaseSmallStatement, cst.BaseExpression)):
                continue
            elif isinstance(child, cst.BaseSuite):
                continue
            else:
                yield from suites(child)

    def walk(block: cst.IndentedBlock, depth: int, loops: List[cst.CSTNode]) -> None:
        out.append(_Body(block, depth, loops))
        for stmt in block.body:
            if isinstance(stmt, (cst.FunctionDef, cst.ClassDef)) or not isinstance(stmt, cst.BaseCompoundStatement):
                continue
            inner_loops = loops + [stmt] if isinstance(stmt, (cst.For, cst.While)) else loops
            for suite in suites(stmt):
                walk(suite, depth + 1, inner_loops)

    if isinstance(func.body, cst.IndentedBlock):
        walk(func.body, 0, [])
    return out


# --------------------------------------------------------------------------- plan


@dataclass
class ExtractPlan:
    source_start: int  # actual first/last line of the selected statements
    source_end: int
    requested: Tuple[int, int]
    qualname: str  # enclosing function, e.g. "ReportGenerator.generate"
    class_qualname: str
    kind: str  # "function" | "method" | "classmethod" | "staticmethod"
    self_name: Optional[str]
    params: List[str]
    returns: List[str]
    suggested_name: str
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    param_hints: Dict[str, List[str]] = field(default_factory=dict)
    self_attrs_read: List[str] = field(default_factory=list)
    self_attrs_written: List[str] = field(default_factory=list)
    param_types: Dict[str, TypeGuess] = field(default_factory=dict)
    return_types: Dict[str, TypeGuess] = field(default_factory=dict)
    param_mutations: Dict[str, List[str]] = field(default_factory=dict)  # how the block changes an argument in place
    section: str = ""  # the section comment right above the block, if any
    section_notes: str = ""  # further comment lines below that heading
    derivations: Dict[str, str] = field(default_factory=dict)  # returned name -> the code that computes it
    calls: List[str] = field(default_factory=list)  # methods called on arguments/self that may have side effects
    prints: bool = False
    code: str = ""  # the selected statements, verbatim

    @property
    def returns_type(self) -> str:
        return returns_type([self.return_types[r] for r in self.returns])

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def loc(self) -> int:
        return self.source_end - self.source_start + 1


@dataclass
class ExtractResult:
    plan: ExtractPlan
    name: str
    params: List[str]  # final (possibly renamed) parameter names
    new_source: str
    diff: str


class _Parsed:
    """One parse of a source plus resolved metadata. Expensive (seconds for a 5k-line file),
    so it is cached per source text and shared by every selection analysed in it."""

    def __init__(self, source: str):
        try:
            self.module = cst.parse_module(source)
        except cst.ParserSyntaxError as exc:
            raise RefactorError(f"File does not parse: {exc}") from exc
        wrapper = MetadataWrapper(self.module, unsafe_skip_copy=True)
        self.pos = wrapper.resolve(PositionProvider)
        self.scopes = wrapper.resolve(ScopeProvider)
        self.parents = wrapper.resolve(ParentNodeProvider)
        collector = _FuncCollector()
        self.module.visit(collector)
        self.functions = collector.found
        # scope -> Name nodes reading a variable that lives in that scope
        self.reads_by_scope: Dict[object, Dict[int, cst.Name]] = {}
        self.super_reads: List[cst.Name] = []
        for scope in {s for s in self.scopes.values() if s is not None}:
            for acc in scope.accesses:
                node = acc.node
                if not isinstance(node, cst.Name):
                    continue
                if node.value == "super":
                    self.super_reads.append(node)
                for ref in acc.referents:
                    if isinstance(ref, Assignment):
                        self.reads_by_scope.setdefault(ref.scope, {})[id(node)] = node


_CACHE: Dict[str, _Parsed] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_SIZE = 4


def _parsed(source: str) -> _Parsed:
    with _CACHE_LOCK:  # the UI warms the cache from a worker thread
        hit = _CACHE.pop(source, None)
        if hit is None:
            hit = _Parsed(source)
        _CACHE[source] = hit  # most recently used last
        while len(_CACHE) > _CACHE_SIZE:  # a few files' worth; metadata is memory-heavy
            del _CACHE[next(iter(_CACHE))]
        return hit


def is_warm(source: str) -> bool:
    return source in _CACHE


def warm_cache(source: str) -> None:
    """Parse and resolve metadata ahead of time (call from a background thread)."""
    _parsed(source)


class _Analysis:
    """One selection within a (cached) parsed source."""

    def __init__(self, source: str, start: int, end: int):
        if start > end:
            start, end = end, start
        self.source = source
        self.requested = (start, end)
        self.parsed = _parsed(source)
        self.module = self.parsed.module
        self.pos = self.parsed.pos
        self.scopes = self.parsed.scopes
        self.parents = self.parsed.parents
        self._select(start, end)

    # ---- helpers
    def line(self, node: cst.CSTNode) -> int:
        return self.pos[node].start.line

    def key(self, node: cst.CSTNode) -> Tuple[int, int]:
        p = self.pos[node].start
        return (p.line, p.column)

    def end_line(self, node: cst.CSTNode) -> int:
        return self.pos[node].end.line

    # ---- selection
    def _select(self, start: int, end: int) -> None:
        best = None  # (coverage, depth, ctx, body, i, j)
        for ctx in self.parsed.functions:
            f_start, f_end = self.line(ctx.func), self.end_line(ctx.func)
            if f_end < start or f_start > end:
                continue
            for body in _function_bodies(ctx.func):
                stmts = body.block.body
                i = 0
                while i < len(stmts):
                    if self.line(stmts[i]) >= start and self.end_line(stmts[i]) <= end:
                        j = i
                        while j + 1 < len(stmts) and self.end_line(stmts[j + 1]) <= end:
                            j += 1
                        coverage = self.end_line(stmts[j]) - self.line(stmts[i]) + 1
                        cand = (coverage, body.depth, ctx, body, i, j)
                        if best is None or cand[:2] > best[:2]:
                            best = cand
                        i = j + 1
                    else:
                        i += 1
        if best is None:
            raise RefactorError(
                f"Lines {start}-{end} do not contain a complete statement inside a function or method."
            )
        _, _, self.ctx, self.body, self.i, self.j = best
        self.stmts: List[cst.BaseStatement] = list(self.body.block.body[self.i : self.j + 1])
        self.sel_start = self.line(self.stmts[0])
        self.sel_end = self.end_line(self.stmts[-1])
        self.func_scope = self.scopes[self.stmts[0]]

    def in_sel(self, node: cst.CSTNode) -> bool:
        return self.sel_start <= self.line(node) <= self.sel_end

    def in_loop_region(self, node: cst.CSTNode) -> bool:
        """Outside the selection but inside a loop that also encloses it (value can flow back)."""
        if not self.body.loops:
            return False
        outer = self.body.loops[0]
        return self.line(outer) <= self.line(node) <= self.end_line(outer) and not self.in_sel(node)


class _ControlFlowChecker(cst.CSTVisitor):
    """Finds constructs whose meaning would change if moved into another function."""

    def __init__(self) -> None:
        self.problems: List[str] = []
        self.loop_depth = 0

    def _add(self, msg: str) -> None:
        if msg not in self.problems:
            self.problems.append(msg)

    def visit_FunctionDef(self, node) -> bool:
        return False  # nested defs have their own control flow

    def visit_Lambda(self, node) -> bool:
        return False

    def visit_ClassDef(self, node) -> bool:
        return False

    def _visit_loop(self, node) -> bool:
        self.loop_depth += 1
        node.body.visit(self)
        self.loop_depth -= 1
        if node.orelse is not None:
            node.orelse.visit(self)  # ``break`` in a loop's else belongs to the outer loop
        for part in ("test", "iter", "target"):
            if hasattr(node, part):
                getattr(node, part).visit(self)
        return False

    def visit_For(self, node) -> bool:
        return self._visit_loop(node)

    def visit_While(self, node) -> bool:
        return self._visit_loop(node)

    def visit_Return(self, node) -> None:
        self._add("contains `return` (an early exit from the enclosing function)")

    def visit_Yield(self, node) -> None:
        self._add("contains `yield` (it is part of a generator)")

    def visit_Await(self, node) -> None:
        self._add("contains `await` (async extraction is not supported)")

    def visit_Global(self, node) -> None:
        self._add("contains a `global` statement")

    def visit_Nonlocal(self, node) -> None:
        self._add("contains a `nonlocal` statement")

    def visit_Break(self, node) -> None:
        if self.loop_depth == 0:
            self._add("contains `break` that targets a loop outside the selection")

    def visit_Continue(self, node) -> None:
        if self.loop_depth == 0:
            self._add("contains `continue` that targets a loop outside the selection")


class _HasGlobalDecl(cst.CSTVisitor):
    def __init__(self) -> None:
        self.found = False

    def visit_Global(self, node) -> None:
        self.found = True

    def visit_Nonlocal(self, node) -> None:
        self.found = True


_SLUG_STOP = {"the", "a", "an", "of", "and", "to", "for", "in", "on", "with", "by", "step", "section"}


def _slug(text: str, max_words: int = 4) -> str:
    words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9]*", text.lower()) if w not in _SLUG_STOP]
    return "_".join(words[:max_words])


def _suggest_name(stmts: Sequence[cst.BaseStatement], header: str, taken: Set[str]) -> str:
    # 1) a section comment directly above the block is the best description of intent
    for line in reversed(list(stmts[0].leading_lines)):
        if line.comment is not None:
            slug = _slug(line.comment.value.lstrip("#"))
            if slug:
                base = slug
                break
    else:
        first = stmts[0]
        if isinstance(first, cst.For):
            base = "process_" + (_slug(cst.Module([]).code_for_node(first.iter), 3) or "items")
        elif isinstance(first, cst.While):
            base = "loop_until_" + (_slug(cst.Module([]).code_for_node(first.test), 3) or "done")
        elif isinstance(first, cst.If):
            base = "handle_" + (_slug(cst.Module([]).code_for_node(first.test), 3) or "case")
        elif isinstance(first, cst.Try):
            base = "try_" + (_slug(header, 3) or "block")
        else:
            base = _slug(header, 3) or "extracted"
    base = re.sub(r"^[0-9_]+", "", base) or "extracted"
    if keyword.iskeyword(base):
        base += "_"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    return name


MUTATING_METHODS = {
    "append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse", "add", "discard", "update",
    "setdefault", "popitem", "appendleft", "extendleft", "popleft", "rotate", "difference_update",
    "intersection_update", "symmetric_difference_update", "write", "writerow", "writerows",
}


def _mutation(an: _Analysis, node: cst.Name) -> Optional[str]:
    """How this use of a name changes the object in place (``x.append()``, ``x[k] = v``), if it does."""
    parent = an.parents.get(node)
    grand = an.parents.get(parent) if parent is not None else None
    if isinstance(parent, cst.Attribute) and parent.value is node:
        if isinstance(grand, cst.Call) and grand.func is parent and parent.attr.value in MUTATING_METHODS:
            return f".{parent.attr.value}()"
        if isinstance(grand, cst.AssignTarget) or (isinstance(grand, cst.AugAssign) and grand.target is parent):
            return f".{parent.attr.value} = ..."
    if isinstance(parent, cst.Subscript) and parent.value is node:
        if isinstance(grand, cst.AssignTarget) or (isinstance(grand, cst.AugAssign) and grand.target is parent):
            return "[...] = ..."
        if isinstance(grand, cst.Del):
            return "del [...]"
        # x[k].append(...) / x[k][j] = ... change what x holds
        outer = grand
        while isinstance(outer, (cst.Subscript, cst.Attribute)):
            nxt = an.parents.get(outer)
            if isinstance(outer, cst.Attribute) and isinstance(nxt, cst.Call) and nxt.func is outer \
                    and outer.attr.value in MUTATING_METHODS:
                return f"[...].{outer.attr.value}()"
            if isinstance(nxt, cst.AssignTarget) or (isinstance(nxt, cst.AugAssign) and nxt.target is outer):
                return "[...][...] = ..."
            outer = nxt
    return None


PURE_METHODS = {
    "get", "items", "keys", "values", "copy", "count", "index", "find", "startswith", "endswith", "format",
    "join", "split", "strip", "lower", "upper", "replace", "isoformat", "strftime", "toordinal", "most_common",
    "isdigit", "isalpha", "weekday", "date", "total_seconds", "snapshot",
}


def _effect_calls(stmts: Sequence[cst.BaseStatement], roots: Set[str], limit: int = 6) -> List[str]:
    """``ctx.inventory.receive()``-style calls on arguments that may change them (not known read-only methods)."""
    found: List[str] = []

    class V(cst.CSTVisitor):
        def visit_Call(self, node: cst.Call) -> None:
            f = node.func
            if not isinstance(f, cst.Attribute) or f.attr.value in PURE_METHODS or f.attr.value in MUTATING_METHODS:
                return
            base = f.value
            while isinstance(base, cst.Attribute):
                base = base.value
            if isinstance(base, cst.Name) and base.value in roots:
                text = cst.Module([]).code_for_node(f) + "()"
                if text not in found and len(text) <= 60:
                    found.append(text)

    for s in stmts:
        s.visit(V())
    return found[:limit]


def _derivations(an: _Analysis, returns: List[str], budget: int = 40) -> Dict[str, str]:
    """For each returned name, the statements of the block that compute it (a backward slice)."""
    out: Dict[str, str] = {}
    for name in returns:
        code = backward_slice(an.stmts, an.parents, [name])
        if code is None or code.count("\n") + 1 > budget:
            continue
        budget -= code.count("\n") + 1
        out[name] = code
    return out


class _RenameVariables(cst.CSTTransformer):
    """Rename variables in a code snippet (not attribute names or keyword arguments)."""

    def __init__(self, renames: Dict[str, str]):
        self.renames = renames
        self.skip: Set[int] = set()

    def visit_Attribute(self, node: cst.Attribute) -> None:
        self.skip.add(id(node.attr))

    def visit_Arg(self, node: cst.Arg) -> None:
        if node.keyword is not None:
            self.skip.add(id(node.keyword))

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> cst.Name:
        new = self.renames.get(original_node.value)
        return updated_node.with_changes(value=new) if new and id(original_node) not in self.skip else updated_node


def _renamed(code: str, renames: Dict[str, str]) -> str:
    if not renames:
        return code
    try:
        return cst.parse_module(code).visit(_RenameVariables(renames)).code
    except cst.ParserSyntaxError:  # pragma: no cover - slices are valid code taken from the block
        return code


def _derivation_lines(plan: ExtractPlan, name: str, renames: Dict[str, str], indent: str) -> List[str]:
    code = plan.derivations.get(name)
    if not code:
        return []
    return [""] + [indent + line if line.strip() else "" for line in _renamed(code, renames).rstrip("\n").splitlines()]


def _comment_block(stmts: Sequence[cst.BaseStatement]) -> List[str]:
    """The comment lines directly above the block (top to bottom), without ``#`` and ``---`` rulers."""
    group: List[str] = []
    for line in reversed(list(stmts[0].leading_lines)):
        if line.comment is None:
            break
        group.append(line.comment.value.lstrip("#").strip().strip("-=*#~ ").strip())
    return [g for g in reversed(group) if g]


def _section_comment(stmts: Sequence[cst.BaseStatement]) -> str:
    block = _comment_block(stmts)
    return block[0] if block else ""


def _calls_print(stmt: cst.CSTNode) -> bool:
    found = []

    class V(cst.CSTVisitor):
        def visit_Call(self, node: cst.Call) -> None:
            if isinstance(node.func, cst.Name) and node.func.value == "print":
                found.append(node)

    stmt.visit(V())
    return bool(found)


def _usage_hint(an: _Analysis, node: cst.Name) -> Optional[str]:
    parent = an.parents.get(node)
    grand = an.parents.get(parent) if parent is not None else None
    if isinstance(parent, cst.Attribute) and parent.value is node:
        if isinstance(grand, cst.Call) and grand.func is parent:
            return f".{parent.attr.value}()"
        return f".{parent.attr.value}"
    if isinstance(parent, cst.Subscript) and parent.value is node:
        sl = parent.slice[0].slice if parent.slice else None
        if isinstance(sl, cst.Index) and isinstance(sl.value, (cst.SimpleString, cst.Integer)):
            return f"[{sl.value.value}]"
        return "[...]"
    if isinstance(parent, (cst.For, cst.CompFor)) and parent.iter is node:
        return "iterated"
    if isinstance(parent, cst.Call) and parent.func is node:
        return "called"
    if isinstance(parent, cst.Arg) and isinstance(grand, cst.Call) and isinstance(grand.func, cst.Name):
        return f"{grand.func.value}(...)"
    if isinstance(parent, (cst.BinaryOperation, cst.AugAssign)):
        return "arithmetic"
    if isinstance(parent, (cst.Comparison, cst.ComparisonTarget)):
        return "compared"
    if isinstance(parent, (cst.If, cst.While, cst.IfExp, cst.BooleanOperation, cst.UnaryOperation)):
        return "truth-tested"
    return None


def _taken_names(an: _Analysis) -> Set[str]:
    """Names the new function must not clash with in its destination namespace."""
    taken: Set[str] = set()
    for stmt in an.module.body:
        if isinstance(stmt, (cst.FunctionDef, cst.ClassDef)):
            taken.add(stmt.name.value)
    gscope = an.scopes.get(an.module.body[0]) if an.module.body else None
    if gscope is not None:
        taken.update(a.name for a in gscope.assignments)
    if an.ctx.class_node is not None:
        for stmt in an.ctx.class_node.body.body:
            if isinstance(stmt, (cst.FunctionDef, cst.ClassDef)):
                taken.add(stmt.name.value)
            elif isinstance(stmt, cst.SimpleStatementLine):
                for small in stmt.body:
                    if isinstance(small, (cst.Assign, cst.AnnAssign)):
                        targets = small.targets if isinstance(small, cst.Assign) else [small]
                        for t in targets:
                            if isinstance(t.target, cst.Name):
                                taken.add(t.target.value)
    return taken


def _method_kind(ctx: _FuncCtx) -> Tuple[str, Optional[str]]:
    """(kind of enclosing function, name of its self/cls parameter)."""
    if ctx.class_node is None:
        return "function", None
    decos = {d.decorator.value for d in ctx.func.decorators if isinstance(d.decorator, cst.Name)}
    if "staticmethod" in decos:
        return "staticmethod", None
    params = ctx.func.params.params or ctx.func.params.posonly_params
    first = params[0].name.value if params else None
    return ("classmethod" if "classmethod" in decos else "method"), first


def _binds(target: cst.CSTNode, name: str) -> bool:
    if isinstance(target, cst.Name):
        return target.value == name
    if isinstance(target, (cst.Tuple, cst.List)):
        return any(_binds(e.value, name) for e in target.elements)
    if isinstance(target, cst.StarredElement):
        return _binds(target.value, name)
    return False


def _always_binds(stmt: cst.CSTNode, name: str) -> bool:
    """Does executing ``stmt`` bind ``name`` on every path (plain assignment, or if/elif/else in all branches)?"""
    if isinstance(stmt, cst.SimpleStatementLine):
        for small in stmt.body:
            if isinstance(small, cst.Assign) and any(_binds(t.target, name) for t in small.targets):
                return True
            if isinstance(small, cst.AnnAssign) and small.value is not None and _binds(small.target, name):
                return True
        return False
    if isinstance(stmt, cst.If):
        if stmt.orelse is None or not isinstance(stmt.body, cst.IndentedBlock):
            return False
        branch_ok = any(_always_binds(s, name) for s in stmt.body.body)
        other = stmt.orelse
        if isinstance(other, cst.If):
            return branch_ok and _always_binds(other, name)
        return branch_ok and isinstance(other.body, cst.IndentedBlock) and any(
            _always_binds(s, name) for s in other.body.body
        )
    return False


def _covered(an: _Analysis, node: cst.CSTNode, name: str, region) -> bool:
    """True if ``node`` (a read of ``name``) always sees a binding made inside ``region``.

    Recognised dominating bindings: an enclosing ``for name in ...`` / ``with ... as name``,
    or an earlier plain assignment in the same or an enclosing statement list.
    ``region(stmt)`` says whether a binding statement counts (e.g. "inside the selection").
    """
    child, cur = node, an.parents.get(node)
    while cur is not None and cur is not an.ctx.func:
        if isinstance(cur, cst.For) and child is cur.body and region(cur) and _binds(cur.target, name):
            return True
        if isinstance(cur, cst.With) and child is cur.body and region(cur):
            if any(item.asname is not None and _binds(item.asname.name, name) for item in cur.items):
                return True
        if isinstance(cur, cst.IndentedBlock):
            for stmt in cur.body:
                if stmt is child:
                    break
                if region(stmt) and _always_binds(stmt, name):
                    return True
        child, cur = cur, an.parents.get(cur)
    return False


def _analyze(an: _Analysis) -> Tuple[ExtractPlan, Dict]:
    problems: List[str] = []
    warnings: List[str] = []
    ctx = an.ctx

    if ctx.nested:
        problems.append(f"`{ctx.qualname}` is a nested function (closures are not supported)")
    checker = _ControlFlowChecker()
    for s in an.stmts:
        s.visit(checker)
    problems.extend(checker.problems)
    g = _HasGlobalDecl()
    ctx.func.visit(g)
    if g.found and not any("global" in p or "nonlocal" in p for p in problems):
        problems.append(f"`{ctx.qualname}` uses `global`/`nonlocal`; moving code out could change which variable it binds")

    enclosing_kind, self_name = _method_kind(ctx)
    fs = an.func_scope

    # ---- accesses to names that live in the enclosing function's scope
    accesses: Dict[int, Tuple[str, cst.CSTNode]] = {
        k: (node.value, node) for k, node in an.parsed.reads_by_scope.get(fs, {}).items()
    }
    uses_super = any(an.in_sel(n) for n in an.parsed.super_reads)

    assignments: List[Tuple[str, cst.CSTNode]] = []
    for a in fs.assignments:
        if isinstance(a, Assignment) and a.scope is fs:
            assignments.append((a.name, a.node))
            # ``x += 1`` is recorded only as an assignment, but it also reads ``x``
            parent = an.parents.get(a.node)
            if isinstance(parent, cst.AugAssign) and parent.target is a.node:
                accesses[id(a.node)] = (a.name, a.node)

    in_selection = an.in_sel
    after_selection = lambda stmt: an.line(stmt) > an.sel_end  # noqa: E731

    reads_in: Dict[str, Tuple[int, int]] = {}  # name -> first read that may see an outside value
    seen_in: Dict[str, Tuple[int, int]] = {}  # name -> first read at all (for ordering/hints)
    reads_after: Set[str] = set()
    for name, node in accesses.values():
        k = an.key(node)
        if an.in_sel(node):
            seen_in[name] = min(seen_in.get(name, k), k)
            if not _covered(an, node, name, in_selection):
                reads_in[name] = min(reads_in.get(name, k), k)
        elif an.in_loop_region(node):
            reads_after.add(name)
        elif an.line(node) > an.sel_end and not _covered(an, node, name, after_selection):
            reads_after.add(name)

    writes_in: Dict[str, Tuple[int, int]] = {}
    writes_before: Set[str] = set()
    for name, node in assignments:
        if an.in_sel(node):
            k = an.key(node)
            if name not in writes_in or k < writes_in[name]:
                writes_in[name] = k
        elif an.line(node) < an.sel_start or an.in_loop_region(node):
            writes_before.add(name)

    # names the selection binds on every path (plain assignment, or if/elif/else assigning in all branches);
    # such a name must not also be passed in: before the first loop iteration it may not exist yet
    unconditional: Set[str] = {n for n in writes_in if any(_always_binds(s, n) for s in an.stmts)}

    returns = sorted((n for n in writes_in if n in reads_after and n != self_name), key=lambda n: writes_in[n])
    params_set = {n for n in reads_in if n in writes_before}
    params_set |= {n for n in returns if n in writes_before and n not in unconditional}
    params_set.discard(self_name)
    first_seen = {**writes_in, **reads_in}
    params = sorted(params_set, key=lambda n: first_seen.get(n, (0, 0)))

    for n in returns:
        if n not in params_set and n not in unconditional:
            warnings.append(
                f"`{n}` is only bound on some paths inside the block; the new `return` may raise UnboundLocalError"
            )

    uses_self = self_name is not None and (self_name in reads_in or uses_super)
    if enclosing_kind == "function":
        kind = "function"
    elif uses_self:
        kind = "classmethod" if enclosing_kind == "classmethod" else "method"
    else:
        kind = "staticmethod"
    if uses_super and kind == "staticmethod":
        problems.append("uses zero-argument `super()` outside a method with `self`")

    # ---- hints for the unit-test draft
    hints: Dict[str, List[str]] = {p: [] for p in params}
    mutations: Dict[str, List[str]] = {}
    self_read: List[str] = []
    self_written: List[str] = []
    for name, node in sorted(accesses.values(), key=lambda t: an.key(t[1])):
        if not an.in_sel(node):
            continue
        if name in hints:
            h = _usage_hint(an, node)
            if h and h not in hints[name]:
                hints[name].append(h)
            m = _mutation(an, node)
            if m and m not in mutations.setdefault(name, []):
                mutations[name].append(m)
        if uses_self and name == self_name:
            parent = an.parents.get(node)
            if isinstance(parent, cst.Attribute) and parent.value is node:
                attr = parent.attr.value
                grand = an.parents.get(parent)
                is_store = isinstance(grand, cst.AssignTarget) or (
                    isinstance(grand, cst.AugAssign) and grand.target is parent
                )
                bucket = self_written if is_store else self_read
                if attr not in bucket:
                    bucket.append(attr)

    # ---- suggested types (Python has none; see typeinfer for where each one comes from)
    inferrer = TypeInferrer(an.module, ctx.func, ctx.class_node, self_name)
    param_types = {p: describe(inferrer, p, hints[p]) for p in params}
    return_types = {r: describe(inferrer, r, []) for r in returns}

    lines = an.source.splitlines()
    header = lines[an.sel_start - 1].strip() if an.sel_start <= len(lines) else ""
    taken = _taken_names(an)
    plan = ExtractPlan(
        source_start=an.sel_start,
        source_end=an.sel_end,
        requested=an.requested,
        qualname=ctx.qualname,
        class_qualname=ctx.class_qualname,
        kind=kind,
        self_name=self_name,
        params=params,
        returns=returns,
        suggested_name=_suggest_name(an.stmts, header, taken),
        problems=problems,
        warnings=warnings,
        param_hints={k: v[:6] for k, v in hints.items()},
        self_attrs_read=self_read,
        self_attrs_written=self_written,
        param_types=param_types,
        return_types=return_types,
        param_mutations=mutations,
        section=_section_comment(an.stmts),
        section_notes=" ".join(_comment_block(an.stmts)[1:]),
        derivations=_derivations(an, returns),
        calls=_effect_calls(an.stmts, set(params) | ({self_name} if self_name else set())),
        prints=any(_calls_print(s) for s in an.stmts),
        code="\n".join(lines[an.sel_start - 1 : an.sel_end]),
    )
    extra = {"accesses": accesses, "assignments": assignments, "taken": taken}
    return plan, extra


def analyze(source: str, start: int, end: int) -> ExtractPlan:
    """Describe what extracting lines ``start``-``end`` would do (never modifies anything)."""
    return _analyze(_Analysis(source, start, end))[0]


# --------------------------------------------------------------------------- rewrite


def _is_identifier(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name)


class _Renamer(cst.CSTTransformer):
    def __init__(self, mapping: Dict[int, str]):
        self.mapping = mapping

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> cst.Name:
        new = self.mapping.get(id(original_node))
        return updated_node.with_changes(value=new) if new else updated_node


class _NameCollector(cst.CSTVisitor):
    def __init__(self) -> None:
        self.names: Set[str] = set()

    def visit_Name(self, node: cst.Name) -> None:
        self.names.add(node.value)


class _Rewriter(cst.CSTTransformer):
    def __init__(self, owner, i, j, call_stmt, container, anchor, new_def):
        self.owner, self.i, self.j, self.call_stmt = owner, i, j, call_stmt
        self.container, self.anchor, self.new_def = container, anchor, new_def

    def _insert(self, original_body, updated_body):
        body = list(updated_body)
        for idx, stmt in enumerate(original_body):
            if stmt is self.anchor:
                body.insert(idx + 1, self.new_def)
                return body
        raise RefactorError("internal error: insertion point not found")

    def leave_IndentedBlock(self, original_node, updated_node):
        body = list(updated_node.body)
        if original_node is self.owner:
            body[self.i : self.j + 1] = [self.call_stmt]
        if original_node is self.container:
            body = self._insert(original_node.body, body)
        return updated_node.with_changes(body=body)

    def leave_Module(self, original_node, updated_node):
        if self.container is original_node:
            return updated_node.with_changes(body=self._insert(original_node.body, updated_node.body))
        return updated_node


def _type_note(guess: TypeGuess) -> str:
    if guess.how == "annotated":
        return "type from an annotation"
    if guess.how == "inferred":
        return "type inferred from assignments"
    if guess.how.startswith("guessed"):
        return "type GUESSED from usage (" + guess.how.split(": ", 1)[-1] + ")"
    return "type unknown"


def _param_role(plan: ExtractPlan, orig: str, final: str) -> str:
    """What the block does with an argument, as far as static analysis can tell."""
    changes = plan.param_mutations.get(orig, [])
    own_calls = [c for c in plan.calls if c.startswith(orig + ".")]
    hints = [h for h in plan.param_hints.get(orig, [])
             if h not in changes and not any(c.endswith(h) or c.startswith(orig + h) for c in own_calls)]
    if orig in plan.returns:
        role = "starting value; the updated value is returned"
    elif changes:
        role = "updated in place (" + ", ".join(f"`{final}{c}`" for c in changes[:3]) + ")"
    elif any(c.startswith(orig + ".") for c in plan.calls):
        calls = [f"`{final}{c[len(orig):]}`" for c in plan.calls if c.startswith(orig + ".")]
        role = "may be changed by " + ", ".join(calls[:3])
    else:
        role = "read only"
    if hints:
        role += "; used as: " + ", ".join(hints[:4])
    return role[0].upper() + role[1:] + "."


def _summary(plan: ExtractPlan, final_params: List[str], renames: Dict[str, str]) -> List[str]:
    head = f"{plan.section[0].upper() + plan.section[1:]}: one step" if plan.section else "One step"
    lines = [f"{head} of `{plan.qualname}` (originally lines {plan.source_start}-{plan.source_end})."]
    if plan.section_notes:
        lines.append(plan.section_notes)
    effects = []
    changed = [f"`{final}`" for orig, final in zip(plan.params, final_params)
               if plan.param_mutations.get(orig) and orig not in plan.returns]
    if changed:
        effects.append("updates " + ", ".join(changed) + " in place")
    if plan.self_attrs_written:
        effects.append("sets " + ", ".join(f"`self.{a}`" for a in plan.self_attrs_written))
    if plan.calls:
        effects.append("calls " + ", ".join(f"`{c}`" for c in plan.calls))
    if plan.prints:
        effects.append("prints to stdout")
    if plan.returns:
        effects.append("returns " + ", ".join(f"`{renames.get(r, r)}`" for r in plan.returns))
    if effects:
        text = "; ".join(effects)
        lines.append(text[0].upper() + text[1:] + ".")
    return lines


def _doc_safe(text: str) -> str:
    """Comment text and hints end up inside a triple-quoted string."""
    return text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def _docstring(plan: ExtractPlan, final_params: List[str], renames: Dict[str, str], indent: str) -> str:
    """Google-style docstring: what the block does, and each argument's role and suggested type."""
    summary = _summary(plan, final_params, renames)
    width = max(60, 96 - len(indent))
    body = [w for line in summary[1:] for w in textwrap.wrap(line, width)]
    out = [f'"""{summary[0]}', ""] + body + ([""] if body else [])
    if final_params:
        out.append("Args:")
        for orig, final in zip(plan.params, final_params):
            g = plan.param_types.get(orig) or TypeGuess("Any", "unknown")
            out.append(f"    {final} ({g.label}): {_param_role(plan, orig, final)}  [{_type_note(g)}]")
        out.append("")
    out.append("Returns:")
    if not plan.returns:
        out.append("    None: works only through the side effects above.")
    elif len(plan.returns) == 1:
        r = plan.returns[0]
        g = plan.return_types[r]
        how = ", computed by:" if plan.derivations.get(r) else "."
        out.append(f"    {g.label}: the new value of `{renames.get(r, r)}`{how}  [{_type_note(g)}]")
        out.extend(_derivation_lines(plan, r, renames, "        "))
    else:
        out.append(f"    {plan.returns_type}: a tuple of")
        for r in plan.returns:
            g = plan.return_types[r]
            how = ", computed by:" if plan.derivations.get(r) else ""
            out.append(f"        {renames.get(r, r)} ({g.label}){how}  [{_type_note(g)}]")
            lines = _derivation_lines(plan, r, renames, "            ")
            out.extend(lines[1:] + [""] if lines else [])
        if out[-1] == "":
            out.pop()
    out.append("")
    out.append("Types are suggestions from pyrefactor's static analysis; Python does not enforce them.")
    out.append("`?` marks a guess - check it before relying on it in tests.")
    out = [out[0][:3] + _doc_safe(out[0][3:])] + [_doc_safe(line) for line in out[1:]]
    text = "\n".join((indent + line) if line and i else line for i, line in enumerate(out))
    return text + "\n" + indent + '"""'


def _names_expr(names: Sequence[str]) -> cst.BaseExpression:
    if len(names) == 1:
        return cst.Name(names[0])
    return cst.Tuple([cst.Element(cst.Name(n)) for n in names], lpar=[], rpar=[])


def extract(
    source: str,
    start: int,
    end: int,
    name: str,
    renames: Optional[Dict[str, str]] = None,
    filename: str = "<string>",
) -> ExtractResult:
    """Extract lines ``start``-``end`` into a new function called ``name``.

    ``renames`` maps inferred parameter names to the names the new function should use.
    Raises RefactorError when the extraction is refused or the inputs are invalid.
    """
    an = _Analysis(source, start, end)
    plan, extra = _analyze(an)
    if plan.problems:
        raise RefactorError(
            f"Cannot extract lines {plan.source_start}-{plan.source_end} of `{plan.qualname}`:\n  - "
            + "\n  - ".join(plan.problems)
        )
    renames = {k: v for k, v in (renames or {}).items() if k != v}

    # ---- validate the user's choices
    if not _is_identifier(name):
        raise RefactorError(f"`{name}` is not a valid Python identifier")
    if name in extra["taken"]:
        where = f"class `{plan.class_qualname}`" if plan.kind != "function" else "the module"
        raise RefactorError(f"`{name}` is already defined in {where}")
    unknown = set(renames) - set(plan.params)
    if unknown:
        raise RefactorError(f"Not parameters of the extracted block: {', '.join(sorted(unknown))}")
    final_params = [renames.get(p, p) for p in plan.params]
    for p in final_params:
        if not _is_identifier(p):
            raise RefactorError(f"Parameter `{p}` is not a valid Python identifier")
    if len(set(final_params)) != len(final_params):
        raise RefactorError("Parameter names must be unique")
    if plan.self_name and plan.self_name in final_params:
        raise RefactorError(f"`{plan.self_name}` is reserved for the instance/class parameter")
    used = _NameCollector()
    for s in an.stmts:
        s.visit(used)
    for old, new in renames.items():
        if new in used.names:
            raise RefactorError(f"Cannot rename `{old}` to `{new}`: `{new}` is already used inside the block")

    # ---- build the new function body (with parameter renames applied by scope identity)
    rename_map: Dict[int, str] = {}
    if renames:
        for nm, node in extra["accesses"].values():
            if nm in renames and an.in_sel(node):
                rename_map[id(node)] = renames[nm]
        for nm, node in extra["assignments"]:
            if nm in renames and an.in_sel(node) and isinstance(node, cst.Name):
                rename_map[id(node)] = renames[nm]
    renamer = _Renamer(rename_map)
    body_stmts = [s.visit(renamer) for s in an.stmts]
    body_stmts[0] = body_stmts[0].with_changes(leading_lines=[])

    indent = " " * an.pos[an.ctx.func].start.column + an.module.default_indent
    doc = cst.SimpleStatementLine([cst.Expr(cst.SimpleString(_docstring(plan, final_params, renames, indent)))])
    new_body: List[cst.BaseStatement] = [doc] + body_stmts
    if plan.returns:
        new_body.append(cst.SimpleStatementLine([cst.Return(_names_expr([renames.get(r, r) for r in plan.returns]))]))

    def_params = [cst.Param(cst.Name(p)) for p in final_params]
    decorators: List[cst.Decorator] = []
    if plan.kind in ("method", "classmethod"):
        def_params.insert(0, cst.Param(cst.Name(plan.self_name)))
        if plan.kind == "classmethod":
            decorators.append(cst.Decorator(cst.Name("classmethod")))
    elif plan.kind == "staticmethod":
        decorators.append(cst.Decorator(cst.Name("staticmethod")))

    parent = an.parents[an.ctx.func]
    container = parent if isinstance(parent, (cst.Module, cst.IndentedBlock)) else None
    if container is None:
        raise RefactorError("internal error: cannot locate where to place the new function")
    blank = [cst.EmptyLine(indent=False)] * (2 if isinstance(container, cst.Module) else 1)
    new_def = cst.FunctionDef(
        name=cst.Name(name),
        params=cst.Parameters(params=def_params),
        body=cst.IndentedBlock(body=new_body),
        decorators=decorators,
        leading_lines=blank,
    )

    # ---- the call that replaces the selection
    if plan.kind in ("method", "classmethod"):
        callee: cst.BaseExpression = cst.Attribute(cst.Name(plan.self_name), cst.Name(name))
    elif plan.kind == "staticmethod":
        _, enclosing_self = _method_kind(an.ctx)
        owner_expr = enclosing_self or plan.class_qualname
        callee = cst.parse_expression(f"{owner_expr}.{name}")
    else:
        callee = cst.Name(name)
    call = cst.Call(callee, args=[cst.Arg(cst.Name(p)) for p in plan.params])
    if plan.returns:
        small: cst.BaseSmallStatement = cst.Assign([cst.AssignTarget(_names_expr(plan.returns))], call)
    else:
        small = cst.Expr(call)
    call_stmt = cst.SimpleStatementLine([small], leading_lines=an.stmts[0].leading_lines)

    new_module = an.module.visit(
        _Rewriter(an.body.block, an.i, an.j, call_stmt, container, an.ctx.func, new_def)
    )
    new_source = new_module.code
    try:
        cst.parse_module(new_source)
        compile(new_source, filename, "exec")
    except (cst.ParserSyntaxError, SyntaxError) as exc:  # pragma: no cover - safety net
        raise RefactorError(f"internal error: generated code does not parse: {exc}") from exc

    diff = "".join(
        difflib.unified_diff(
            source.splitlines(keepends=True),
            new_source.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        )
    )
    return ExtractResult(plan=plan, name=name, params=final_params, new_source=new_source, diff=diff)
