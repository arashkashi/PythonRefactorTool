"""Record real calls of a freshly extracted function by running the user's program once.

The program runs in its own process with ``_recorder/sitecustomize.py`` on ``PYTHONPATH``;
that hook wraps the new function and writes what it saw to a temporary file, read back here.
The recording holds only builtins (strings, dicts, and the user's values as pickled bytes),
so nothing of the user's code is imported into pyrefactor itself.
"""
from __future__ import annotations

import os
import pickle
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .extract import ExtractResult

HOOK_DIR = Path(__file__).parent / "_recorder"


@dataclass
class Recording:
    command: str
    calls: int = 0  # how often the function ran during the program
    cases: List[dict] = field(default_factory=list)  # distinct behaviours, at most max_cases
    errors: List[str] = field(default_factory=list)
    problem: str = ""  # why nothing usable was recorded

    @property
    def ok(self) -> bool:
        return bool(self.cases)


def default_command(target: Path, cwd: Path) -> str:
    """``python3 <file>`` when the file is runnable on its own, else ""."""
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return ""
    if "__name__" not in text or "__main__" not in text:
        return ""
    python = "py" if os.name == "nt" and shutil.which("py") else ("python3" if shutil.which("python3") else "python")
    try:
        rel = target.resolve().relative_to(cwd.resolve())
    except ValueError:
        rel = target.resolve()
    return f"{python} {shlex.quote(rel.as_posix())}"


def record_calls(
    target: Path,
    result: ExtractResult,
    command: str,
    cwd: Path,
    max_cases: int = 3,
    timeout: int = 120,
) -> Recording:
    """Run ``command`` in ``cwd`` and record up to ``max_cases`` distinct calls of the new function."""
    rec = Recording(command=command)
    try:
        argv = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        rec.problem = f"cannot parse the command: {exc}"
        return rec
    if not argv:
        rec.problem = "no command to run"
        return rec
    plan = result.plan
    with tempfile.TemporaryDirectory(prefix="pyrefactor-rec-") as tmp:
        out = Path(tmp) / "recording.pkl"
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(HOOK_DIR)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        env.update(
            PYREFACTOR_TARGET=str(target.resolve()),
            PYREFACTOR_MODULE=target.stem,
            PYREFACTOR_FUNC=result.name,
            PYREFACTOR_CLASS=plan.class_qualname.split(".")[-1] if plan.kind != "function" else "",
            PYREFACTOR_OUT=str(out),
            PYREFACTOR_MAX_CASES=str(max_cases),
        )
        try:
            proc = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, timeout=timeout)
            stderr = proc.stderr.decode("utf-8", "replace")
            returncode: Optional[int] = proc.returncode
        except FileNotFoundError:
            rec.problem = f"command not found: {argv[0]}"
            return rec
        except subprocess.TimeoutExpired:
            stderr, returncode = "", None
            rec.errors.append(f"the program did not finish within {timeout}s; using what was recorded so far")
        if out.exists():
            with out.open("rb") as fh:
                data = pickle.load(fh)  # builtins only; the user's values stay as bytes
            rec.calls, rec.cases = data["calls"], data["cases"]
            rec.errors.extend(data["errors"])
        if returncode not in (0, None):
            tail = "\n".join(stderr.strip().splitlines()[-3:])
            rec.errors.append(f"the program exited with code {returncode}" + (f": {tail}" if tail else ""))
    if not rec.cases:
        if rec.calls == 0:
            rec.problem = f"`{result.name}` was never called while running `{command}`"
        else:
            rec.problem = f"`{result.name}` ran {rec.calls}x but no call could be recorded"
        if rec.errors:
            rec.problem += " (" + rec.errors[-1] + ")"
    return rec

