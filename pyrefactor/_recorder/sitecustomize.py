"""Loaded automatically (via PYTHONPATH) into the user's program by ``pyrefactor.record``.

It waits until the module that holds the freshly extracted function has defined it, wraps
that function, and records a few real calls: the arguments (pickled before the call), the
result, the arguments' state after the call (to catch in-place changes), printed output and
exceptions. A profile hook is only active until the function is found; after that the
program runs at full speed. Everything is written to ``PYREFACTOR_OUT`` at exit.

Only the standard library is used: this runs inside the user's interpreter.
"""
import atexit
import builtins
import collections
import datetime
import decimal
import functools
import inspect
import io
import os
import pickle
import re
import sys

_TARGET = os.path.realpath(os.environ.get("PYREFACTOR_TARGET", ""))
_MODULE = os.environ.get("PYREFACTOR_MODULE", "")
_FUNC = os.environ.get("PYREFACTOR_FUNC", "")
_CLASS = os.environ.get("PYREFACTOR_CLASS", "")  # "" for module-level functions
_OUT = os.environ.get("PYREFACTOR_OUT", "")
_MAX_CASES = int(os.environ.get("PYREFACTOR_MAX_CASES", "3"))
_MAX_INLINE = 200
_MAX_BYTES = 20_000_000

_state = {"calls": 0, "cases": [], "seen": set(), "installed": False, "errors": [], "file_cache": {}}


# --------------------------------------------------------------------------- comparison helpers


def _shape(x, depth=0):
    """A structure that compares equal when two values hold the same data (objects by their fields)."""
    if depth > 50:
        return "..."
    d = depth + 1
    if isinstance(x, (str, bytes, int, float, complex, bool, type(None))):
        return x
    if isinstance(x, dict):
        return (type(x).__name__, sorted(((repr(k), _shape(v, d)) for k, v in x.items()), key=lambda t: t[0]))
    if isinstance(x, (list, tuple)):
        return (type(x).__name__, [_shape(v, d) for v in x])
    if isinstance(x, (set, frozenset)):
        return (type(x).__name__, sorted(repr(_shape(v, d)) for v in x))
    fields = getattr(x, "__dict__", None)
    if fields is None and hasattr(type(x), "__slots__"):
        fields = {s: getattr(x, s) for s in type(x).__slots__ if hasattr(x, s)}
    if fields is not None:
        return (type(x).__qualname__, _shape(dict(fields), d))
    return (type(x).__qualname__, repr(x))


def _eval_namespace(globs):
    ns = dict(vars(builtins))
    ns.update(globs)
    ns.update(datetime=datetime, Decimal=decimal.Decimal, OrderedDict=collections.OrderedDict,
              Counter=collections.Counter, deque=collections.deque)
    return ns


def _describe(value, globs):
    """{'pickle': bytes, 'type': str, 'repr': str|None, 'names': [...], 'eq': bool}"""
    out = {"type": type(value).__qualname__, "repr": None, "names": [], "eq": False}
    try:  # e.g. "list of 10 Order", shown next to values that cannot be written inline
        if isinstance(value, (list, tuple, set, frozenset, dict)):
            n = len(value)
            if isinstance(value, dict):
                out["type"] += f" with {n} entries"
            else:
                first = next(iter(value), None)
                out["type"] += f" of {n}" + (f" {type(first).__qualname__}" if n else "")
    except Exception:  # noqa: BLE001
        pass
    try:
        data = pickle.dumps(value, protocol=4)
    except Exception as exc:  # noqa: BLE001 - anything unpicklable is reported, not fatal
        raise _Unrecordable(f"{type(value).__qualname__} cannot be pickled ({exc})")
    out["pickle"] = data
    try:
        out["eq"] = bool(pickle.loads(data) == value)
    except Exception:  # noqa: BLE001
        out["eq"] = False
    try:
        r = repr(value)
        if len(r) <= _MAX_INLINE:
            back = eval(r, _eval_namespace(globs))  # noqa: S307 - the user's own program, own values
            if type(back) is type(value) and _shape(back) == _shape(value):
                out["repr"] = r
                out["names"] = sorted(
                    n for n in set(re.findall(r"\b([A-Za-z_]\w*)\(", r))
                    if isinstance(globs.get(n), type) and getattr(globs[n], "__module__", "") == _MODULE
                )
    except Exception:  # noqa: BLE001
        pass
    return out


def _mutable_ids(value, limit=200_000):
    """ids of the mutable objects reachable from ``value`` (to find objects shared between arguments)."""
    seen, stack = set(), [value]
    while stack and len(seen) < limit:
        x = stack.pop()
        if isinstance(x, (str, bytes, int, float, complex, bool, type(None), type)) or id(x) in seen:
            continue
        seen.add(id(x))
        if isinstance(x, dict):
            stack.extend(x.values())
        elif isinstance(x, (list, tuple, set, frozenset)):
            stack.extend(x)
        else:
            fields = getattr(x, "__dict__", None)
            if isinstance(fields, dict):
                stack.extend(fields.values())
    return seen


class _Unrecordable(Exception):
    pass


class _Tee(io.TextIOBase):
    def __init__(self, inner, buf):
        self.inner, self.buf = inner, buf

    def write(self, s):
        self.buf.write(s)
        return self.inner.write(s)

    def flush(self):
        self.inner.flush()


# --------------------------------------------------------------------------- recording


def _wrap(func, sig, globs):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        _state["calls"] += 1
        call_no = _state["calls"]
        if len(_state["cases"]) >= _MAX_CASES:
            return func(*args, **kwargs)
        try:
            before = pickle.dumps((args, kwargs), protocol=4)
        except Exception as exc:  # noqa: BLE001
            _state["errors"].append(f"call {call_no}: arguments cannot be pickled ({exc})")
            return func(*args, **kwargs)
        # which lines of the function run: cases are kept per distinct code path
        lines = set()
        code = func.__code__

        def local(frame, event, arg):
            if event == "line":
                lines.add(frame.f_lineno)
            return local

        def outer(frame, event, arg):
            return local if frame.f_code is code else None

        buf, old = io.StringIO(), sys.stdout
        sys.stdout = _Tee(old, buf)
        prev_trace = sys.gettrace()
        sys.settrace(outer)
        exc_info = None
        result = None
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            exc_info = (type(exc).__name__, str(exc))
            raise
        finally:
            sys.settrace(prev_trace)
            sys.stdout = old
            _record(call_no, sig, globs, before, args, kwargs, result, buf.getvalue(), exc_info, lines)
        return result

    return wrapper


def _record(call_no, sig, globs, before, args, kwargs, result, stdout, exc_info, lines):
    try:
        b_args, b_kwargs = pickle.loads(before)
        bound_before = sig.bind(*b_args, **b_kwargs).arguments
        bound_after = sig.bind(*args, **kwargs).arguments
        case = {"call": call_no, "args": {}, "after": {}, "stdout": stdout, "exc": exc_info, "result": None}
        reach = {name: _mutable_ids(value) for name, value in bound_before.items()}
        for name, value in bound_before.items():
            case["args"][name] = _describe(value, globs)
            others = set().union(*(ids for n, ids in reach.items() if n != name)) if len(reach) > 1 else set()
            if reach[name] & others:
                case["args"][name]["repr"] = None  # shares objects with another argument: keep them shared
            if _shape(value) != _shape(bound_after[name]):
                case["after"][name] = _describe(bound_after[name], globs)
        # arguments that are not written inline are pickled together, so shared objects stay shared
        bundle = {n: v for n, v in bound_before.items() if case["args"][n]["repr"] is None}
        case["bundle"] = pickle.dumps(bundle, protocol=4) if bundle else None
        if exc_info is None:
            case["result"] = _describe(result, globs)
        size = sum(len(v["pickle"]) for part in ("args", "after") for v in case[part].values())
        if size > _MAX_BYTES:
            raise _Unrecordable(f"recorded values are too large ({size // 1_000_000} MB)")
        key = (frozenset(lines), exc_info[0] if exc_info else None)
        if key in _state["seen"]:
            return  # same code path as a case we already have
        case["lines"] = sorted(lines)
        _state["seen"].add(key)
        _state["cases"].append(case)
        _flush()
    except _Unrecordable as exc:
        _state["errors"].append(f"call {call_no}: {exc}")
    except Exception as exc:  # noqa: BLE001 - recording must never break the user's program
        _state["errors"].append(f"call {call_no}: {type(exc).__name__}: {exc}")


def _flush():
    if not _OUT:
        return
    tmp = _OUT + ".tmp"
    with open(tmp, "wb") as fh:
        pickle.dump({"calls": _state["calls"], "cases": _state["cases"], "errors": _state["errors"],
                     "installed": _state["installed"]}, fh, protocol=4)
    os.replace(tmp, _OUT)


def _adopt_main(globs):
    """Running ``python file.py`` makes the module ``__main__``; pickles must say ``<module>`` instead."""
    if globs.get("__name__") != "__main__" or not _MODULE:
        return
    sys.modules.setdefault(_MODULE, sys.modules["__main__"])
    for value in list(globs.values()):
        if getattr(value, "__module__", None) == "__main__" and (inspect.isclass(value) or inspect.isfunction(value)):
            try:
                value.__module__ = _MODULE
            except (AttributeError, TypeError):
                pass


def _install(globs):
    owner = globs.get(_CLASS) if _CLASS else None
    if _CLASS and not inspect.isclass(owner):
        return False
    raw = owner.__dict__.get(_FUNC) if owner is not None else globs.get(_FUNC)
    if raw is None:
        return False
    _adopt_main(globs)
    if isinstance(raw, staticmethod):
        func = raw.__func__
        wrapped = staticmethod(_wrap(func, inspect.signature(func), globs))
    elif isinstance(raw, classmethod):
        func = raw.__func__
        wrapped = classmethod(_wrap(func, inspect.signature(func), globs))
    else:
        func = raw
        wrapped = _wrap(func, inspect.signature(func), globs)
    if owner is not None:
        setattr(owner, _FUNC, wrapped)
    else:
        globs[_FUNC] = wrapped
    _state["installed"] = True
    return True


def _is_target(filename):
    cache = _state["file_cache"]
    hit = cache.get(filename)
    if hit is None:
        hit = cache[filename] = os.path.realpath(filename) == _TARGET
    return hit


def _profile(frame, event, arg):
    if event != "call" or not _is_target(frame.f_code.co_filename):
        return
    if _install(frame.f_globals):
        sys.setprofile(None)
        threading_setprofile(None)


def threading_setprofile(fn):
    try:
        import threading
        threading.setprofile(fn)
    except Exception:  # noqa: BLE001
        pass


if _TARGET and _FUNC and _OUT:
    atexit.register(_flush)
    sys.setprofile(_profile)
    threading_setprofile(_profile)
