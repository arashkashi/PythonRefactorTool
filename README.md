# pyrefactor

Browse a Python file as a **size-weighted tree** and **extract functions** from it, built on
[LibCST](https://libcst.readthedocs.io/) (formatting and comments are preserved exactly).

```
$ pyrefactor tree sample/legacy_shop.py --sort --depth 1 --min-lines 15
◆ legacy_shop.py  4852 lines
├── def process_orders  ████████████████████████ 647 L1342-1988        ← red: top 10% of this file
│   ├── ▸ for order in orders:  ██ 45 L1910-1954                       ← ▸ = extractable block
│   ├── ▸ with audit.section("validate"):  ██ 43 L1400-1442
│   ├── ▸ with audit.section("pricing"):  █ 36 L1504-1539
├── class ReportGenerator  █████████████████████ 559 L1995-2553
│   ├── def generate_monthly_report  ████████████████ 432 L2067-2498
```

## Install

Requires Python 3.9 or newer. The steps below install `pyrefactor` as a global command in its own
isolated environment (via [pipx](https://pipx.pypa.io/)), so it works in any terminal without activating anything.

### macOS

```bash
# 1. Python 3.9+ and pipx (skip what you already have)
brew install python pipx
pipx ensurepath                      # then open a new terminal window

# 2. Install pyrefactor straight from GitHub
pipx install git+https://github.com/arashkashi/PythonRefactorTool.git

# 3. Check
pyrefactor --help
```

No Homebrew? Use `python3 -m pip install --user pipx && python3 -m pipx ensurepath` for step 1.

### Windows (PowerShell)

```powershell
# 1. Python 3.9+ (tick "Add python.exe to PATH" in the installer) and pipx
winget install Python.Python.3.12    # or download from https://www.python.org/downloads/windows/
py -m pip install --user pipx
py -m pipx ensurepath                # then close and reopen PowerShell

# 2. Install pyrefactor straight from GitHub (needs Git: winget install Git.Git)
pipx install git+https://github.com/arashkashi/PythonRefactorTool.git

# 3. Check
pyrefactor --help
```

Use **Windows Terminal** (default on Windows 11; `winget install Microsoft.WindowsTerminal` on Windows 10) —
the old console window renders the tree colours and mouse clicks poorly.

### Update / uninstall

```bash
pipx upgrade pyrefactor        # or: pipx install --force git+https://github.com/arashkashi/PythonRefactorTool.git
pipx uninstall pyrefactor
```

## Try it on the included sample

The sample (`sample/legacy_shop.py`, ~4,850 lines of deliberately messy Python) lives in the repository,
so clone it first. Work on a copy so you can compare before/after.

**macOS**

```bash
git clone https://github.com/arashkashi/PythonRefactorTool.git
mkdir -p ~/pyrefactor-try && cp PythonRefactorTool/sample/legacy_shop.py ~/pyrefactor-try/
cd ~/pyrefactor-try
python3 legacy_shop.py > before.txt          # record behaviour before refactoring
pyrefactor .                                 # open the UI: click a ▸ block → Preview diff → Apply → q
python3 legacy_shop.py > after.txt && diff before.txt after.txt && echo "SAME BEHAVIOUR"
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/arashkashi/PythonRefactorTool.git
mkdir $HOME\pyrefactor-try; copy PythonRefactorTool\sample\legacy_shop.py $HOME\pyrefactor-try\
cd $HOME\pyrefactor-try
py legacy_shop.py > before.txt
pyrefactor .
py legacy_shop.py > after.txt; if (Compare-Object (Get-Content before.txt) (Get-Content after.txt)) { "DIFFERENT" } else { "SAME BEHAVIOUR" }
```

Good blocks to try: inside `process_orders`, `▸ with audit.section("pricing")` (clean extraction);
inside `run_month_end_close`, `▸ if month in closed_months:` (refused: contains `return`).
The old version stays next to the file as `legacy_shop.py.bak` and still runs.

## Development install

```bash
git clone https://github.com/arashkashi/PythonRefactorTool.git && cd PythonRefactorTool
python3 -m venv .venv                         # Windows: py -m venv .venv
source .venv/bin/activate                     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest
```

## Use

```bash
pyrefactor ~/my_project                           # whole project: every .py file, biggest first; refactors in place
pyrefactor sample/legacy_shop.py                  # a single file
pyrefactor tree sample/legacy_shop.py --no-blocks # print only classes/functions
pyrefactor tree sample/legacy_shop.py --sort      # biggest first
pyrefactor analyze sample/legacy_shop.py --lines 120-180     # inferred args/returns, no changes
pyrefactor extract sample/legacy_shop.py --lines 120-180     # asks name + each arg, shows diff, asks to apply
pyrefactor extract FILE --lines 120-180 --name f --rename items=line_items -y   # scripted
```

### Interactive tree

Pointing it at a folder lists every `.py` file (skipping `.venv`, `.git`, `__pycache__`, `build`, … ),
**biggest first**, with the 5 largest files and the 5 largest functions per file marked `◀ #1`…`#5`.
A file is parsed when you expand it. Refactors are written **in place** (a `.bak` copy is kept next to the
file) and test drafts go to `<project>/tests/`.

| Action | Effect |
|---|---|
| move cursor | details pane: inferred arguments/returns, refusal reasons, code preview |
| **click / enter on a ▸ block** | opens the Extract dialog |
| click / enter on a class or function | expand / collapse |
| `space` | expand / collapse |
| `e` on a function | Extract dialog with the whole body as the range; edit **Lines** + Enter to narrow it |
| `s` | toggle biggest-first (default) / source order |
| `x` | expand all |
| `r` | reload from disk |

In the dialog you confirm the **function name** and **each argument name** (renaming an argument renames
it inside the new function only; the call site keeps the caller's variable). *Preview diff* → *Apply*.
On apply it writes `FILE.bak` (then `.bak2`, …) and, if ticked, a unit-test draft.

### Levels of abstraction

module → class → function/method → block (`if/for/while/with/try/match`).
Function internals are collapsed by default. `--depth N` limits how deep nested blocks go (default 3),
`--min-lines N` hides blocks shorter than N lines (default 5) so the tree stays high level.

### Size encoding

A terminal can't render different font sizes, so size is shown three ways:
a proportional `█` bar, the line count, and a heat colour based on **this file's** percentiles of
function sizes (green < p50 ≤ yellow < p75 ≤ orange < p90 ≤ **red**). Top-10% functions are also shown reversed.

## What Extract Function does

* **Arguments**: names local to the enclosing function that the block reads and that were bound before it
  (or carried around an enclosing loop).
* **Returns**: names the block binds that are read after it (or carried around an enclosing loop).
* In a method: uses `self` → new method called as `self.x(...)`; doesn't → `@staticmethod`. Classmethods stay classmethods.
* **Refused with an explanation** (never silently wrong): `return`, `yield`, `await`, `global`/`nonlocal`,
  `break`/`continue` aimed at a loop outside the selection, nested (closure) functions.
* A selection that cuts through statements is snapped to whole statements; the dialog/CLI says so.

Known limits: the inference is name-based, so it can't see aliasing tricks such as `locals()`/`exec`;
extracted functions get a `TODO` docstring and no type annotations.

## Unit-test drafts

Written to `<file dir>/tests/test_<module>_<function>.py` (never overwrites; override with `--tests-dir`).
They import the function, build the object via `__new__` for methods and pre-set only the `self.`
attributes the block reads, fill placeholder arguments based on how each argument is *used*
(`iterated` → `[]`, `["key"]` → `{}`, arithmetic → `0`), unpack the return values, and leave `assert ... == ...`
TODOs plus an empty parametrized edge-case test. They are marked `skip` so they don't fail until completed.

## Tests

```bash
.venv/bin/python -m pytest
```

Includes behaviour-equivalence tests (run code before/after extraction and compare) and a headless
Textual test that clicks a block, edits the name/argument, previews and applies.
