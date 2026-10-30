"""Best-effort static type inference for the arguments and results of an extracted block.

Python has no static types, so this only *suggests* types, and says where each one came from:

    annotated  the enclosing function's signature or an annotated assignment says so
    inferred   derived from the values assigned to the name (literals, constructors,
               calls to annotated functions, ``for x in <list[T]>``, ...)
    guessed    nothing was assigned that we understand; derived from how the block uses it
               (``.append()`` -> list, arithmetic -> float, ...)

The result is used in the new function's docstring and in the unit-test draft.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

import libcst as cst

_CODE = cst.Module([])

BUILTIN_SAME = {"int", "float", "str", "bool", "list", "dict", "set", "tuple", "frozenset", "bytes", "complex"}
BUILTIN_RETURNS = {
    "len": "int", "ord": "int", "hash": "int", "id": "int",
    "repr": "str", "chr": "str", "format": "str", "input": "str", "ascii": "str",
    "isinstance": "bool", "issubclass": "bool", "callable": "bool", "any": "bool", "all": "bool",
    "range": "range",
}
KNOWN_CLASSES = {"Counter", "OrderedDict", "deque", "Decimal", "Fraction", "date", "datetime", "timedelta", "Path"}
STR_METHODS = {
    "lower", "upper", "strip", "lstrip", "rstrip", "title", "capitalize", "replace", "format", "join",
    "ljust", "rjust", "center", "zfill", "casefold", "removeprefix", "removesuffix", "isoformat", "strftime",
}
STR_PREDICATES = {"startswith", "endswith", "isdigit", "isalpha", "isalnum", "isspace", "isupper", "islower"}
SEQ_GENERICS = {"list", "List", "Sequence", "Iterable", "Iterator", "set", "Set", "frozenset", "FrozenSet",
                "Collection", "MutableSequence", "deque", "Deque", "Generator", "AbstractSet"}
MAP_GENERICS = {"dict", "Dict", "Mapping", "MutableMapping", "defaultdict", "DefaultDict", "OrderedDict", "Counter"}


@dataclass
class TypeGuess:
    type: str  # e.g. "list[Order]"; "Any" when nothing could be inferred
    how: str  # "annotated" | "inferred" | "guessed: <usage>" | "unknown"

    @property
    def label(self) -> str:
        return self.type + ("?" if self.how.startswith("guessed") else "")


# --------------------------------------------------------------------------- type strings


def split_generic(t: str) -> Tuple[str, List[str]]:
    """"dict[str, list[int]]" -> ("dict", ["str", "list[int]"])."""
    t = t.strip()
    if not t.endswith("]") or "[" not in t:
        return t, []
    base, inner = t.split("[", 1)
    args, depth, cur = [], 0, ""
    for ch in inner[:-1]:
        if ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
            continue
        depth += ch in "[("
        depth -= ch in "])"
        cur += ch
    if cur.strip():
        args.append(cur.strip())
    return base.strip().split(".")[-1], args


def element_type(t: Optional[str]) -> Optional[str]:
    """What iterating over a value of type ``t`` yields."""
    if not t:
        return None
    base, args = split_generic(t)
    if base in ("str",):
        return "str"
    if base == "range":
        return "int"
    if base in ("bytes",):
        return "int"
    if base in SEQ_GENERICS and args:
        return args[0]
    if base in MAP_GENERICS and args:
        return args[0] if base != "Counter" else args[0]
    if base in ("tuple", "Tuple") and args:
        if len(args) == 2 and args[1] == "...":
            return args[0]
        return union(args)
    return None


def union(types: Iterable[Optional[str]]) -> Optional[str]:
    seen: List[str] = []
    for t in types:
        if t is None:
            return None  # one unknown makes the whole union unknown
        for part in t.split(" | "):
            if part not in seen:
                seen.append(part)
    if not seen:
        return None
    if "int" in seen and "float" in seen:  # int is acceptable wherever float is
        seen.remove("int")
    if "bool" in seen and "int" in seen:
        seen.remove("bool")
    if len(seen) > 3:
        return None
    if "None" in seen and len(seen) > 1:
        seen.remove("None")
        seen.append("None")
    return " | ".join(seen)


def _numeric(a: Optional[str], b: Optional[str]) -> Optional[str]:
    nums = {"int", "float", "bool", "complex", "Decimal"}
    if a in nums and b in nums:
        if "complex" in (a, b):
            return "complex"
        if "Decimal" in (a, b):
            return "Decimal"
        return "float" if "float" in (a, b) else "int"
    if a in nums and b is None or b in nums and a is None:
        return "float" if "float" in (a, b) else None
    return None


# --------------------------------------------------------------------------- inference


class _ModuleInfo:
    """Module-level facts: classes, annotated function returns, constants."""

    def __init__(self, module: cst.Module):
        self.classes: Dict[str, cst.ClassDef] = {}
        self.func_returns: Dict[str, str] = {}
        self.constants: Dict[str, cst.BaseExpression] = {}
        self.annotated: Dict[str, str] = {}
        for stmt in module.body:
            if isinstance(stmt, cst.ClassDef):
                self.classes[stmt.name.value] = stmt
            elif isinstance(stmt, cst.FunctionDef) and stmt.returns is not None:
                self.func_returns[stmt.name.value] = _CODE.code_for_node(stmt.returns.annotation)
            elif isinstance(stmt, cst.SimpleStatementLine):
                for small in stmt.body:
                    if isinstance(small, cst.Assign):
                        for t in small.targets:
                            if isinstance(t.target, cst.Name):
                                self.constants[t.target.value] = small.value
                    elif isinstance(small, cst.AnnAssign) and isinstance(small.target, cst.Name):
                        self.annotated[small.target.value] = _CODE.code_for_node(small.annotation.annotation)


class TypeInferrer:
    """Infers the types of names local to one function."""

    def __init__(self, module: cst.Module, func: cst.FunctionDef, class_node: Optional[cst.ClassDef] = None,
                 self_name: Optional[str] = None, _info: Optional[_ModuleInfo] = None):
        self.info = _info or _ModuleInfo(module)
        self.module = module
        self.func = func
        self.class_node = class_node
        self.self_name = self_name
        # name -> ("annotation", text) | ("value", expr) | ("iter", expr, index-path) | ("default", expr)
        self.bindings: Dict[str, List[tuple]] = {}
        self._visiting: Set[str] = set()
        self._attr_cache: Optional[Dict[str, List[tuple]]] = None
        self._collect()

    # ---- bindings
    def _add(self, name: str, binding: tuple) -> None:
        self.bindings.setdefault(name, []).append(binding)

    def _bind_target(self, target: cst.BaseExpression, binding_for) -> None:
        """``binding_for(path)`` builds the binding for the element at ``path`` of a tuple target."""
        def walk(t, path):
            if isinstance(t, cst.Name):
                self._add(t.value, binding_for(path))
            elif isinstance(t, (cst.Tuple, cst.List)):
                for i, el in enumerate(t.elements):
                    if isinstance(el, cst.Element):
                        walk(el.value, path + (i,))
        walk(target, ())

    def _collect(self) -> None:
        params = self.func.params
        for p in list(params.posonly_params) + list(params.params) + list(params.kwonly_params):
            if p.annotation is not None:
                self._add(p.name.value, ("annotation", _CODE.code_for_node(p.annotation.annotation)))
            elif p.default is not None:
                self._add(p.name.value, ("value", p.default))
        inferrer = self

        class Collector(cst.CSTVisitor):
            def visit_FunctionDef(self, node) -> bool:
                return node is inferrer.func  # skip nested functions

            def visit_ClassDef(self, node) -> bool:
                return False

            def visit_Lambda(self, node) -> bool:
                return False

            def visit_Assign(self, node: cst.Assign) -> None:
                for t in node.targets:
                    inferrer._bind_target(t.target, lambda path, v=node.value: ("value", v, path))

            def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
                if isinstance(node.target, cst.Name):
                    inferrer._add(node.target.value, ("annotation", _CODE.code_for_node(node.annotation.annotation)))

            def visit_AugAssign(self, node: cst.AugAssign) -> None:
                if isinstance(node.target, cst.Name):  # `total += 1.5` makes `total` a float
                    inferrer._add(node.target.value, ("aug", node.value))

            def visit_For(self, node: cst.For) -> None:
                inferrer._bind_target(node.target, lambda path, it=node.iter: ("iter", it, path))

            def visit_CompFor(self, node: cst.CompFor) -> None:
                inferrer._bind_target(node.target, lambda path, it=node.iter: ("iter", it, path))

        self.func.visit(Collector())

    # ---- names
    def name_type(self, name: str) -> Tuple[Optional[str], str]:
        """(type, how) for a local name, combining every binding of it in the function."""
        bindings = self.bindings.get(name, [])
        annotated = [b[1] for b in bindings if b[0] == "annotation"]
        if annotated:
            return union(annotated) or annotated[0], "annotated"
        if name in self._visiting or not bindings:
            return None, "unknown"
        self._visiting.add(name)
        try:
            types = [self._binding_type(b) for b in bindings]
        finally:
            self._visiting.discard(name)
        known = [t for t in types if t is not None]
        if not known:
            return None, "unknown"
        # e.g. `total = 0` then `total = total + x`: the unknown re-binding is the same variable
        return union(known), "inferred"

    def _binding_type(self, b: tuple) -> Optional[str]:
        kind = b[0]
        if kind == "value":
            expr, path = b[1], (b[2] if len(b) > 2 else ())
            if path:
                return self._unpack(expr, self.expr_type(expr) if not isinstance(expr, (cst.Tuple, cst.List)) else None, path)
            return self.expr_type(expr)
        if kind == "aug":
            t = self.expr_type(b[1])
            return t if t in ("int", "float", "complex", "str", "Decimal") else None
        if kind == "iter":
            expr, path = b[1], b[2]
            return self._iter_item(expr, path)
        return None

    def _unpack(self, expr, t: Optional[str], path) -> Optional[str]:
        for idx in path:
            if isinstance(expr, (cst.Tuple, cst.List)) and idx < len(expr.elements):
                expr = expr.elements[idx].value
                t = self.expr_type(expr)
                continue
            expr = None
            if not t:
                return None
            base, args = split_generic(t)
            if base in ("tuple", "Tuple") and args and not (len(args) == 2 and args[1] == "..."):
                t = args[idx] if idx < len(args) else None
            else:
                t = element_type(t)
        return t

    def _iter_item(self, it: cst.BaseExpression, path) -> Optional[str]:
        item: Optional[str]
        if isinstance(it, cst.Call) and isinstance(it.func, cst.Name) and it.func.value in ("enumerate", "zip"):
            parts = [self.expr_type(a.value) for a in it.args if a.keyword is None]
            elems = [element_type(p) for p in parts]
            if it.func.value == "enumerate":
                elems = ["int", elems[0] if elems else None]
            if not path:
                return f"tuple[{', '.join(e or 'Any' for e in elems)}]"
            first = path[0]
            item = elems[first] if first < len(elems) else None
            return self._unpack(None, item, path[1:]) if len(path) > 1 else item
        if isinstance(it, cst.Call) and isinstance(it.func, cst.Attribute) and it.func.attr.value in ("items", "keys", "values"):
            base, args = split_generic(self.expr_type(it.func.value) or "")
            if base in MAP_GENERICS and len(args) == 2:
                k, v = args
            elif base == "Counter" and args:
                k, v = args[0], "int"
            else:
                return None
            method = it.func.attr.value
            if method == "keys":
                item = k
            elif method == "values":
                item = v
            else:
                item = f"tuple[{k}, {v}]"
            return self._unpack(None, item, path) if path else item
        item = element_type(self.expr_type(it))
        return self._unpack(None, item, path) if path else item

    # ---- expressions
    def expr_type(self, e: Optional[cst.BaseExpression], depth: int = 0) -> Optional[str]:
        if e is None or depth > 8:
            return None
        d = depth + 1
        if isinstance(e, cst.Integer):
            return "int"
        if isinstance(e, cst.Float):
            return "float"
        if isinstance(e, cst.Imaginary):
            return "complex"
        if isinstance(e, cst.SimpleString):
            return "bytes" if "b" in e.prefix.lower() else "str"
        if isinstance(e, (cst.FormattedString, cst.ConcatenatedString)):
            return "str"
        if isinstance(e, cst.Name):
            v = e.value
            if v in ("True", "False"):
                return "bool"
            if v == "None":
                return "None"
            if v in self.bindings:
                return self.name_type(v)[0]
            if v in self.info.annotated:
                return self.info.annotated[v]
            if v in self.info.constants and v not in self._visiting:
                self._visiting.add(v)
                try:
                    return self.expr_type(self.info.constants[v], d)
                finally:
                    self._visiting.discard(v)
            return None
        if isinstance(e, (cst.List, cst.Set, cst.Tuple)):
            elems = [self.expr_type(el.value, d) for el in e.elements if isinstance(el, cst.Element)]
            base = {cst.List: "list", cst.Set: "set", cst.Tuple: "tuple"}[type(e)]
            if base == "tuple":
                if elems and all(elems) and len(elems) <= 5:
                    return f"tuple[{', '.join(elems)}]"
                return "tuple"
            inner = union(elems) if elems else None
            return f"{base}[{inner}]" if inner else base
        if isinstance(e, cst.Dict):
            keys = [self.expr_type(el.key, d) for el in e.elements if isinstance(el, cst.DictElement)]
            vals = [self.expr_type(el.value, d) for el in e.elements if isinstance(el, cst.DictElement)]
            k, v = (union(keys), union(vals)) if keys else (None, None)
            return f"dict[{k}, {v}]" if k and v else "dict"
        if isinstance(e, cst.ListComp):
            inner = self.expr_type(e.elt, d)
            return f"list[{inner}]" if inner else "list"
        if isinstance(e, cst.SetComp):
            inner = self.expr_type(e.elt, d)
            return f"set[{inner}]" if inner else "set"
        if isinstance(e, cst.DictComp):
            k, v = self.expr_type(e.key, d), self.expr_type(e.value, d)
            return f"dict[{k}, {v}]" if k and v else "dict"
        if isinstance(e, cst.GeneratorExp):
            return "Iterator"
        if isinstance(e, cst.Lambda):
            return "Callable"
        if isinstance(e, cst.Comparison):
            return "bool"
        if isinstance(e, cst.UnaryOperation):
            if isinstance(e.operator, cst.Not):
                return "bool"
            return self.expr_type(e.expression, d)
        if isinstance(e, cst.BooleanOperation):
            return union([self.expr_type(e.left, d), self.expr_type(e.right, d)])
        if isinstance(e, cst.IfExp):
            return union([self.expr_type(e.body, d), self.expr_type(e.orelse, d)])
        if isinstance(e, cst.BinaryOperation):
            return self._binop(e, d)
        if isinstance(e, cst.Call):
            return self._call(e, d)
        if isinstance(e, cst.Subscript):
            return self._subscript(e, d)
        if isinstance(e, cst.Attribute):
            if isinstance(e.value, cst.Name) and e.value.value == self.self_name:
                return self.self_attr_type(e.attr.value)
            return None
        return None

    def _binop(self, e: cst.BinaryOperation, d: int) -> Optional[str]:
        left, right = self.expr_type(e.left, d), self.expr_type(e.right, d)
        op = e.operator
        if isinstance(op, cst.Modulo) and left == "str":
            return "str"
        if isinstance(op, cst.Add) and (left == "str" or right == "str"):
            return "str"
        if isinstance(op, (cst.Add, cst.Multiply)):
            for a, b in ((left, right), (right, left)):
                if a and split_generic(a)[0] in ("list", "tuple", "str") and (isinstance(op, cst.Add) or b == "int"):
                    return a
        if isinstance(op, cst.Divide):
            return "float" if left in ("int", "float", "bool", None) and right in ("int", "float", "bool", None) \
                and (left or right) else _numeric(left, right)
        if isinstance(op, (cst.BitOr, cst.BitAnd, cst.BitXor, cst.Subtract)) and left and split_generic(left)[0] in ("set", "frozenset"):
            return left
        return _numeric(left, right)

    def _call(self, e: cst.Call, d: int) -> Optional[str]:
        args = [a.value for a in e.args if a.keyword is None and not a.star]
        f = e.func
        if isinstance(f, cst.Name):
            n = f.value
            if n in BUILTIN_SAME:
                if n in ("list", "set", "frozenset") and args:
                    el = element_type(self.expr_type(args[0], d))
                    return f"{n}[{el}]" if el else n
                return n
            if n in BUILTIN_RETURNS:
                return BUILTIN_RETURNS[n]
            if n == "round":
                return "float" if len(args) > 1 else "int"
            if n in ("abs", "min", "max") and args:
                if len(args) == 1:
                    return element_type(self.expr_type(args[0], d))
                return union(self.expr_type(a, d) for a in args)
            if n == "sum" and args:
                el = element_type(self.expr_type(args[0], d))
                return el if el in ("int", "float") else ("float" if el is None else None)
            if n == "sorted" and args:
                el = element_type(self.expr_type(args[0], d))
                return f"list[{el}]" if el else "list"
            if n == "defaultdict":
                factory = args[0] if args else None
                if isinstance(factory, cst.Name) and factory.value in BUILTIN_SAME:
                    return f"defaultdict[Any, {factory.value}]"
                return "defaultdict"
            if n in self.info.func_returns:
                return self.info.func_returns[n]
            if n in self.info.classes or n in KNOWN_CLASSES:
                return n
            if n[:1].isupper():
                return n  # most likely an imported class
            return None
        if isinstance(f, cst.Attribute):
            m = f.attr.value
            if isinstance(f.value, cst.Name) and f.value.value == self.self_name and self.class_node is not None:
                return self._method_return(m)
            if m == "choice" and args:  # random.choice / rng.choice
                return element_type(self.expr_type(args[0], d))
            if m in ("sample", "choices") and args:
                el = element_type(self.expr_type(args[0], d))
                return f"list[{el}]" if el else "list"
            owner = self.expr_type(f.value, d)
            if owner == "str":
                if m in STR_METHODS:
                    return "str"
                if m in STR_PREDICATES:
                    return "bool"
                if m in ("split", "splitlines", "rsplit"):
                    return "list[str]"
                if m in ("find", "count", "index", "rfind"):
                    return "int"
                if m == "encode":
                    return "bytes"
            if owner:
                base, targs = split_generic(owner)
                if m == "copy":
                    return owner
                if base in MAP_GENERICS and m in ("get", "pop", "setdefault") and len(targs) == 2:
                    default = self.expr_type(args[1], d) if len(args) > 1 else "None"
                    return union([targs[1], default]) if m == "get" else targs[1]
                if base in ("list", "List", "deque") and m == "pop" and targs:
                    return targs[0]
                if base == "Counter" and m == "most_common":
                    return f"list[tuple[{targs[0] if targs else 'Any'}, int]]"
                if base in ("date", "datetime") and m in ("isoformat", "strftime"):
                    return "str"
                if base in ("date", "datetime") and m in ("replace",):
                    return base
                if base == "datetime" and m == "date":
                    return "date"
            if isinstance(f.value, cst.Name) and m[:1].isupper():
                return f"{f.value.value}.{m}"  # module.Class(...), e.g. random.Random(42)
            if isinstance(f.value, cst.Name) and f.value.value in ("datetime", "date") and m in ("now", "today", "fromisoformat"):
                return f.value.value
            return None
        return None

    def _subscript(self, e: cst.Subscript, d: int) -> Optional[str]:
        t = self.expr_type(e.value, d)
        if not t or len(e.slice) != 1:
            return None
        sl = e.slice[0].slice
        if isinstance(sl, cst.Slice):
            return t if split_generic(t)[0] in ("list", "List", "tuple", "str") else None
        base, args = split_generic(t)
        if base == "str":
            return "str"
        if base in MAP_GENERICS and len(args) == 2:
            return args[1]
        if base == "Counter":
            return "int"
        if base in ("tuple", "Tuple") and args:
            if isinstance(sl, cst.Index) and isinstance(sl.value, cst.Integer) and not (len(args) == 2 and args[1] == "..."):
                i = int(sl.value.value)
                return args[i] if i < len(args) else None
            return element_type(t)
        if base in SEQ_GENERICS and args:
            return args[0]
        return None

    # ---- self.attributes and methods of the enclosing class
    def _class_methods(self) -> Dict[str, cst.FunctionDef]:
        out: Dict[str, cst.FunctionDef] = {}
        if self.class_node is not None:
            for stmt in self.class_node.body.body:
                if isinstance(stmt, cst.FunctionDef):
                    out[stmt.name.value] = stmt
        return out

    def _method_return(self, name: str) -> Optional[str]:
        m = self._class_methods().get(name)
        if m is not None and m.returns is not None:
            return _CODE.code_for_node(m.returns.annotation)
        return None

    def self_attr_type(self, attr: str) -> Optional[str]:
        if self.class_node is None:
            return None
        # class-level annotations (dataclasses) win
        for stmt in self.class_node.body.body:
            if isinstance(stmt, cst.SimpleStatementLine):
                for small in stmt.body:
                    if isinstance(small, cst.AnnAssign) and isinstance(small.target, cst.Name) \
                            and small.target.value == attr:
                        return _CODE.code_for_node(small.annotation.annotation)
        init = self._class_methods().get("__init__")
        if init is None or ("self." + attr) in self._visiting:
            return None
        self._visiting.add("self." + attr)
        try:
            sub = TypeInferrer(self.module, init, self.class_node, self.self_name, self.info)
            types = []
            for node in _self_assignments(init, attr):
                if isinstance(node, cst.AnnAssign):
                    return _CODE.code_for_node(node.annotation.annotation)
                types.append(sub.expr_type(node.value))
            return union(types) if types else None
        finally:
            self._visiting.discard("self." + attr)


def _self_assignments(func: cst.FunctionDef, attr: str) -> List[cst.CSTNode]:
    found: List[cst.CSTNode] = []

    class V(cst.CSTVisitor):
        def visit_Assign(self, node: cst.Assign) -> None:
            for t in node.targets:
                if isinstance(t.target, cst.Attribute) and t.target.attr.value == attr:
                    found.append(node)

        def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
            if isinstance(node.target, cst.Attribute) and node.target.attr.value == attr:
                found.append(node)

    func.visit(V())
    return found


# --------------------------------------------------------------------------- usage fallback


def guess_from_usage(hints: List[str]) -> Optional[str]:
    """A type that would satisfy how the block uses a value (hints come from extract._usage_hint)."""
    if any(h.startswith((".append", ".extend", ".insert", ".sort")) for h in hints):
        return "list"
    if any(h.startswith((".items", ".keys", ".values", ".get", ".setdefault", ".update")) for h in hints):
        return "dict"
    if any(h.startswith((".add", ".discard")) for h in hints):
        return "set"
    if any(h.startswith("." + m) for h in hints for m in STR_METHODS | STR_PREDICATES | {"split"}):
        return "str"
    if any(h.startswith('["') or h.startswith("['") for h in hints):
        return "dict"
    if "iterated" in hints or "len(...)" in hints:
        return "Iterable"
    if "arithmetic" in hints:
        return "float"
    if "called" in hints:
        return "Callable"
    return None


def describe(inferrer: TypeInferrer, name: str, hints: List[str]) -> TypeGuess:
    t, how = inferrer.name_type(name)
    if t is not None:
        return TypeGuess(t, how)
    g = guess_from_usage(hints)
    if g is not None:
        return TypeGuess(g, "guessed: " + ", ".join(hints))
    return TypeGuess("Any", "unknown")


def returns_type(types: List[TypeGuess]) -> str:
    if not types:
        return "None"
    if len(types) == 1:
        return types[0].type
    return f"tuple[{', '.join(t.type for t in types)}]"
