"""Textual UI: size-weighted outline on the left, details on the right, extract dialog on tap."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from rich.console import Group
from rich.syntax import Syntax
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, Static, Tree
from textual.widgets.tree import TreeNode as UITreeNode

from .analyzer import SizeScale, TreeNode, build_tree, largest, size_scale
from .extract import ExtractPlan, ExtractResult, RefactorError, analyze, extract, is_warm, warm_cache
from .render import HEAT, apply_to_disk, file_label, legend, node_label, parse_range
from .project import discover, line_count
from .testgen import write_test

PREVIEW_LINES = 60


def _plan_summary(plan: ExtractPlan) -> Text:
    t = Text()
    kind = {
        "function": "module-level function",
        "method": "method (uses self)",
        "classmethod": "classmethod (uses cls)",
        "staticmethod": "@staticmethod (does not use self)",
    }[plan.kind]
    t.append("Would extract: ", style="bold")
    t.append(f"lines {plan.source_start}-{plan.source_end} ({plan.loc}) of {plan.qualname} as a {kind}\n")
    t.append("Arguments:  ", style="bold")
    t.append((", ".join(plan.params) or "(none)") + "\n", style="cyan")
    t.append("Returns:    ", style="bold")
    t.append((", ".join(plan.returns) or "(nothing)") + "\n", style="green")
    for p in plan.problems:
        t.append(f"✗ {p}\n", style="bold red")
    for w in plan.warnings:
        t.append(f"⚠ {w}\n", style="yellow")
    return t


class DiffScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "back", "Back"), Binding("a", "apply", "Apply")]

    def __init__(self, result: ExtractResult, test_note: str):
        super().__init__()
        self.result = result
        self.test_note = test_note

    def compose(self) -> ComposeResult:
        with Vertical(id="diff-dialog"):
            yield Label(
                f"Preview: extract [b]{self.result.name}({', '.join(self.result.params)})[/b]  {self.test_note}",
                id="diff-title",
            )
            with VerticalScroll(id="diff-body"):
                yield Static(Syntax(self.result.diff, "diff", theme="ansi_dark", word_wrap=False))
            with Horizontal(classes="buttons"):
                yield Button("Apply (a)", id="apply", variant="success")
                yield Button("Back (esc)", id="back")

    @on(Button.Pressed, "#apply")
    def action_apply(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#back")
    def action_back(self) -> None:
        self.dismiss(False)


class ExtractScreen(ModalScreen[Optional[Tuple[ExtractResult, bool]]]):
    """Ask for the new function name and its arguments, then preview and confirm."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, source: str, filename: str, start: int, end: int, write_tests: bool):
        super().__init__()
        self.source = source
        self.filename = filename
        self.range = (start, end)
        self.write_tests = write_tests
        self.plan: Optional[ExtractPlan] = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Extract function", id="dialog-title")
            with Horizontal(classes="row"):
                yield Label("Lines", classes="field-label")
                yield Input(value=f"{self.range[0]}-{self.range[1]}", id="lines")
            yield Static(id="summary")
            with Horizontal(classes="row"):
                yield Label("Function name", classes="field-label")
                yield Input(id="fname")
            yield Label("Arguments (edit a name to rename it inside the new function):", id="params-title")
            yield VerticalScroll(id="params")
            yield Checkbox("Write a unit-test draft", value=self.write_tests, id="wtest")
            with Horizontal(classes="buttons"):
                yield Button("Preview diff", id="preview", variant="primary")
                yield Button("Cancel", id="cancel")

    async def on_mount(self) -> None:
        await self._analyze()
        self.query_one("#fname", Input).focus()

    async def _analyze(self) -> None:
        summary = self.query_one("#summary", Static)
        params = self.query_one("#params", VerticalScroll)
        await params.remove_children()
        self.query_one("#preview", Button).disabled = True
        if not is_warm(self.source):
            summary.update(Text("Analysing file… (first time takes a few seconds on large files)", style="italic"))
        try:
            self.plan = await asyncio.to_thread(analyze, self.source, *self.range)
        except RefactorError as exc:
            self.plan = None
            summary.update(Text(f"✗ {exc}", style="bold red"))
            self.query_one("#preview", Button).disabled = True
            return
        plan = self.plan
        summary.update(_plan_summary(plan))
        self.query_one("#lines", Input).value = f"{plan.source_start}-{plan.source_end}"
        self.query_one("#fname", Input).value = plan.suggested_name
        self.query_one("#preview", Button).disabled = not plan.ok
        rows = []
        for i, p in enumerate(plan.params):
            hints = plan.param_hints.get(p, [])
            rows.append(
                Horizontal(
                    Label(p, classes="field-label"),
                    Input(value=p, id=f"param-{i}", classes="param"),
                    Label(", ".join(hints), classes="hint"),
                    classes="row",
                )
            )
        if not rows:
            rows.append(Label("(no arguments needed)", classes="hint"))
        await params.mount(*rows)

    @on(Input.Submitted, "#lines")
    async def _lines_changed(self, event: Input.Submitted) -> None:
        try:
            self.range = parse_range(event.value)
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        await self._analyze()

    @on(Input.Submitted, "#fname")
    @on(Input.Submitted, ".param")
    @on(Button.Pressed, "#preview")
    def _preview(self) -> None:
        if self.plan is None or not self.plan.ok:
            return
        name = self.query_one("#fname", Input).value.strip()
        renames: Dict[str, str] = {}
        for i, p in enumerate(self.plan.params):
            renames[p] = self.query_one(f"#param-{i}", Input).value.strip()
        try:
            result = extract(self.source, *self.range, name=name, renames=renames, filename=self.filename)
        except RefactorError as exc:
            self.notify(str(exc), title="Cannot extract", severity="error", timeout=8)
            return
        want_test = self.query_one("#wtest", Checkbox).value
        note = "[dim](+ unit-test draft)[/dim]" if want_test else ""

        def done(apply: Optional[bool]) -> None:
            if apply:
                self.dismiss((result, want_test))

        self.app.push_screen(DiffScreen(result, note), done)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


TOP_RANKS = 5  # mark the N largest functions per file (and N largest files)


def _scale_of(sizes: List[int]) -> SizeScale:
    sizes = sorted(sizes) or [1]

    def pct(p: float) -> int:
        return sizes[min(len(sizes) - 1, int(round(p * (len(sizes) - 1))))]

    return SizeScale(p50=max(2, pct(0.5)), p75=max(3, pct(0.75)), p90=max(4, pct(0.9)), max_loc=max(sizes))


@dataclass
class FileState:
    path: Path
    rel: str
    loc: int
    source: str = ""
    root: Optional[TreeNode] = None
    scale: Optional[SizeScale] = None
    ranks: Dict[int, int] = field(default_factory=dict)  # id(TreeNode) -> rank among functions/methods
    error: str = ""
    loading: bool = False


@dataclass
class Item:
    """What a row in the tree stands for."""

    kind: str  # "project" | "dir" | "file" | "code"
    path: Optional[Path] = None
    file: Optional[FileState] = None
    node: Optional[TreeNode] = None


class RefactorApp(App):
    TITLE = "pyrefactor"
    CSS = """
    #main { height: 1fr; }
    #tree { width: 3fr; border: round $primary; }
    #side { width: 2fr; border: round $secondary; padding: 0 1; }
    #legend { height: 1; padding: 0 1; }
    ExtractScreen, DiffScreen { align: center middle; }
    #dialog { width: 90%; max-width: 120; height: auto; max-height: 100%; border: thick $accent;
              background: $surface; padding: 0 2; }
    #diff-dialog { width: 95%; height: 90%; border: thick $accent; background: $surface; padding: 0 1; }
    #diff-body { height: 1fr; }
    #dialog-title, #diff-title { text-style: bold; }
    .row { height: auto; }
    .field-label { width: 22; padding: 1 1 0 0; }
    .row Input { width: 1fr; }
    .hint { color: $text-muted; padding: 1 0 0 1; width: 1fr; }
    #params { height: auto; max-height: 12; border: round $panel; }
    #summary { padding: 0 0 1 0; max-height: 8; overflow-y: auto; }
    .buttons { height: auto; }
    .buttons Button { margin-right: 2; }
    """
    BINDINGS = [
        Binding("e", "extract", "Extract"),
        Binding("s", "toggle_sort", "Size/source order"),
        Binding("x", "expand_all", "Expand all"),
        Binding("r", "reload", "Reload"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        path: Path,
        max_block_depth: int = 3,
        min_block_lines: int = 5,
        write_tests: bool = True,
        tests_dir: Optional[Path] = None,
    ):
        super().__init__()
        self.target = path.resolve()
        self.single_file = self.target.is_file()
        self.project_root = self.target.parent if self.single_file else self.target
        self.max_block_depth = max_block_depth
        self.min_block_lines = min_block_lines
        self.write_tests = write_tests
        # single file: <file dir>/tests ; project: <project>/tests
        self.tests_dir = tests_dir or (None if self.single_file else self.project_root / "tests")
        self.sort_by_size = True
        self.files: Dict[Path, FileState] = {}
        self.file_nodes: Dict[Path, UITreeNode] = {}
        self.expanded: set = set()
        self._plans: Dict[Tuple[Path, int, int], object] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="legend")
        with Horizontal(id="main"):
            yield Tree(self.target.name, id="tree")
            with VerticalScroll(id="side"):
                yield Static(id="info")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = str(self.target)
        tree = self.query_one("#tree", Tree)
        tree.auto_expand = False
        tree.guide_depth = 3
        self.discover()
        tree.focus()

    # ---- compatibility helpers for the single-file case (used by tests/scripts)
    @property
    def _single(self) -> Optional[FileState]:
        return next(iter(self.files.values())) if self.single_file and self.files else None

    @property
    def source(self) -> str:
        fs = self._single
        return fs.source if fs else ""

    @property
    def root(self) -> Optional[TreeNode]:
        fs = self._single
        return fs.root if fs else None

    # ---- model
    def discover(self) -> None:
        self.files = {}
        for p in discover(self.target):
            rel = p.relative_to(self.project_root).as_posix()
            self.files[p] = FileState(p, rel, line_count(p))
        self.file_scale = _scale_of([f.loc for f in self.files.values()])
        biggest = sorted(self.files.values(), key=lambda f: -f.loc)[:TOP_RANKS]
        self.file_ranks = {f.path: i + 1 for i, f in enumerate(biggest)} if len(self.files) > 1 else {}
        total = sum(f.loc for f in self.files.values())
        self.query_one("#legend", Static).update(
            Text(f"{len(self.files)} Python files · {total} lines   ", style="bold")
            + legend(self.file_scale if not self.single_file else _scale_of([1]))
            + Text("   ◀ #n = n-th largest · biggest first (s: source order)", style="dim")
        )
        if self.single_file:
            self.expanded.add(self.target)
        self.populate()
        for p in list(self.expanded):
            if p in self.files:
                self.load_file(self.files[p])
        if not self.files:
            self.notify("No Python files found here.", severity="warning")

    def load_file(self, fs: FileState) -> None:
        if fs.loading:
            return
        fs.loading = True
        self.run_worker(lambda: self._load_file_thread(fs), thread=True, group="load")

    def _load_file_thread(self, fs: FileState) -> None:
        try:
            fs.source = fs.path.read_text(encoding="utf-8")
            fs.root = build_tree(fs.source, fs.rel, self.max_block_depth, self.min_block_lines)
            fs.scale = size_scale(fs.root)
            fs.loc = fs.root.loc
            fs.ranks = {id(n): i + 1 for i, n in enumerate(largest(fs.root, n=TOP_RANKS))}
            fs.error = ""
        except Exception as exc:  # unreadable or unparsable file: show it in the tree
            fs.root, fs.error = None, f"cannot analyse: {str(exc).splitlines()[0][:120]}"
        fs.loading = False
        self.call_from_thread(self._file_loaded, fs)
        if fs.root is not None:
            try:
                warm_cache(fs.source)  # data-flow metadata for the extract dialog / details pane
            except RefactorError:
                pass
            self.call_from_thread(self._refresh_details)

    def _file_loaded(self, fs: FileState) -> None:
        ui = self.file_nodes.get(fs.path)
        if ui is not None:
            self._fill_file(ui, fs)
            if fs.path in self.expanded:
                ui.expand()
        self._refresh_details()

    # ---- tree building
    def _file_ui_label(self, fs: FileState) -> Text:
        note = "loading…" if fs.loading else (fs.error or "")
        if fs.root is not None and not note:
            n = sum(1 for x in fs.root.walk() if x.kind in ("function", "method"))
            note = f"{n} functions"
        return file_label(fs.rel if self.single_file else fs.path.name, fs.loc, self.file_scale,
                          self.file_ranks.get(fs.path), note)

    def _sorted(self, nodes: List[TreeNode]) -> List[TreeNode]:
        return sorted(nodes, key=lambda c: -c.loc) if self.sort_by_size else list(nodes)

    def _add_code(self, ui: UITreeNode, node: TreeNode, fs: FileState) -> None:
        for child in self._sorted(node.children):
            label = node_label(child, fs.scale, rank=fs.ranks.get(id(child)))
            item = Item("code", path=fs.path, file=fs, node=child)
            if child.children:
                sub = ui.add(label, data=item, expand=child.kind in ("class", "code"))
                self._add_code(sub, child, fs)
            else:
                ui.add_leaf(label, data=item)

    def _fill_file(self, ui: UITreeNode, fs: FileState) -> None:
        ui.remove_children()
        ui.set_label(self._file_ui_label(fs))
        if fs.root is not None:
            self._add_code(ui, fs.root, fs)
        elif fs.error:
            ui.add_leaf(Text(fs.error, style="red"))

    def populate(self) -> None:
        tree = self.query_one("#tree", Tree)
        tree.clear()
        self.file_nodes = {}
        total = sum(f.loc for f in self.files.values())
        root_label = Text(f"◆ {self.target.name}", style="bold")
        root_label.append(f"  {len(self.files)} files · {total} lines", style="dim")
        tree.root.set_label(root_label)
        tree.root.data = Item("project", path=self.project_root)

        # nested directory structure: dir -> (subdirs, files)
        dirs: Dict[Path, Tuple[set, List[FileState]]] = {self.project_root: (set(), [])}
        for fs in self.files.values():
            dirs.setdefault(fs.path.parent, (set(), []))[1].append(fs)
            d = fs.path.parent
            while d != self.project_root:
                dirs.setdefault(d.parent, (set(), []))[0].add(d)
                d = d.parent

        def dir_loc(d: Path) -> int:
            subdirs, files = dirs.get(d, (set(), []))
            return sum(f.loc for f in files) + sum(dir_loc(s) for s in subdirs)

        def add_dir(ui: UITreeNode, d: Path, depth: int) -> None:
            subdirs, files = dirs.get(d, (set(), []))
            entries: List[Tuple[int, str, object]] = [(dir_loc(s), s.name, s) for s in subdirs]
            entries += [(f.loc, f.path.name, f) for f in files]
            if self.sort_by_size:
                entries.sort(key=lambda e: -e[0])
            else:  # folders first, then files, alphabetically
                entries.sort(key=lambda e: (isinstance(e[2], FileState), e[1]))
            for loc, name, obj in entries:
                if isinstance(obj, FileState):
                    node = ui.add(self._file_ui_label(obj), data=Item("file", path=obj.path, file=obj), allow_expand=True)
                    self.file_nodes[obj.path] = node
                    if obj.root is not None:
                        self._fill_file(node, obj)
                        if obj.path in self.expanded:
                            node.expand()
                else:
                    label = Text(f"▣ {name}/", style="bold blue")
                    label.append(f"  {loc} lines", style="dim")
                    sub = ui.add(label, data=Item("dir", path=obj), expand=depth == 0)
                    add_dir(sub, obj, depth + 1)

        add_dir(tree.root, self.project_root, 0)
        tree.root.expand()

    # ---- details panel
    def _plan_for(self, fs: FileState, node: TreeNode):
        key = (fs.path, node.start, node.end)
        if key not in self._plans:
            try:
                self._plans[key] = analyze(fs.source, node.start, node.end)
            except RefactorError as exc:
                self._plans[key] = exc
        return self._plans[key]

    def _refresh_details(self) -> None:
        self.show_details(self._cursor_item())

    def show_details(self, item: Optional[Item]) -> None:
        info = self.query_one("#info", Static)
        if item is None:
            info.update("")
            return
        if item.kind in ("project", "dir"):
            files = [f for f in self.files.values() if item.path in f.path.parents]
            t = Text(f"{'PROJECT' if item.kind == 'project' else 'FOLDER'}  {item.path}\n", style="bold")
            t.append(f"{len(files)} Python files, {sum(f.loc for f in files)} lines\n\nLargest files:\n", style="dim")
            for f in sorted(files, key=lambda f: -f.loc)[:15]:
                t.append(f"  {f.loc:>6}  {f.rel}\n")
            t.append("\nExpand a file (enter / click) to see its functions.", style="italic dim")
            info.update(t)
            return
        fs = item.file
        if item.kind == "file":
            t = Text(f"FILE  {fs.rel}\n", style="bold")
            t.append(f"{fs.loc} lines\n\n", style="dim")
            if fs.root is None:
                t.append(fs.error or "Click / enter to expand and analyse this file.", style="italic")
            else:
                t.append("Largest functions/methods (refactor these first):\n", style="bold")
                for i, n in enumerate(largest(fs.root, n=15)):
                    style = HEAT[fs.scale.level(n.loc)]
                    t.append(f"  {('#%d' % (i + 1)):>4} ", style="bold red" if i < TOP_RANKS else "dim")
                    t.append(f"{n.loc:>5}", style=style)
                    t.append(f"  {n.qualname}  L{n.start}\n")
            info.update(t)
            return

        node = item.node
        head = Text()
        head.append(f"{node.kind.upper()}  ", style="bold")
        head.append(node.qualname if node.kind != "block" else f"{node.header}", style="bold cyan")
        head.append(f"\n{fs.rel}  lines {node.start}-{node.end}  ({node.loc} lines)\n", style="dim")
        rank = fs.ranks.get(id(node))
        if rank:
            head.append(f"#{rank} largest function in this file\n", style="bold red")
        parts = [head]
        if node.kind == "block" and not is_warm(fs.source):
            parts.append(Text("Analysing data flow… (one-time, a few seconds on large files)\n", style="italic yellow"))
        elif node.kind == "block":
            plan = self._plan_for(fs, node)
            if isinstance(plan, RefactorError):
                parts.append(Text(f"✗ {plan}\n", style="red"))
            else:
                parts.append(_plan_summary(plan))
            parts.append(Text("Press enter / click to extract this block.\n", style="italic dim"))
        elif node.kind in ("function", "method"):
            big = sorted((c for c in node.children if c.kind == "block"), key=lambda c: -c.loc)[:5]
            if big:
                parts.append(Text("Largest blocks inside:\n", style="bold"))
                for b in big:
                    parts.append(Text(f"  {b.loc:>4}  L{b.start}  {b.header[:50]}\n"))
            parts.append(Text("Press e to extract a line range of this function.\n", style="italic dim"))
        lines = fs.source.splitlines()
        shown_end = min(node.end, node.start + PREVIEW_LINES - 1)
        code = "\n".join(lines[node.start - 1 : shown_end])
        parts.append(Syntax(code, "python", line_numbers=True, start_line=node.start, theme="ansi_dark", word_wrap=False))
        if shown_end < node.end:
            parts.append(Text(f"… {node.end - shown_end} more lines", style="dim"))
        info.update(Group(*parts))

    # ---- events
    @on(Tree.NodeHighlighted)
    def _highlighted(self, event: Tree.NodeHighlighted) -> None:
        self.show_details(event.node.data)

    @on(Tree.NodeExpanded)
    def _expanded(self, event: Tree.NodeExpanded) -> None:
        item: Optional[Item] = event.node.data
        if item is not None and item.kind == "file":
            self.expanded.add(item.path)
            if item.file.root is None and not item.file.error:
                self.load_file(item.file)
                event.node.set_label(self._file_ui_label(item.file))

    @on(Tree.NodeCollapsed)
    def _collapsed(self, event: Tree.NodeCollapsed) -> None:
        item: Optional[Item] = event.node.data
        if item is not None and item.kind == "file":
            self.expanded.discard(item.path)

    @on(Tree.NodeSelected)
    def _selected(self, event: Tree.NodeSelected) -> None:
        item: Optional[Item] = event.node.data
        if item is None:
            return
        if item.kind == "code" and item.node.kind == "block":
            self.open_extract(item.file, item.node.start, item.node.end)
        else:
            event.node.toggle()

    # ---- actions
    def _cursor_item(self) -> Optional[Item]:
        ui = self.query_one("#tree", Tree).cursor_node
        return ui.data if ui is not None else None

    def action_extract(self) -> None:
        item = self._cursor_item()
        if item is None or item.kind != "code" or item.node.kind not in ("block", "function", "method"):
            self.notify("Move to a ▸ block (or a function to pick a line range).", severity="warning")
            return
        if item.node.kind == "block":
            self.open_extract(item.file, item.node.start, item.node.end)
        else:  # whole body minus the def line; the user narrows it in the dialog
            self.open_extract(item.file, item.node.start + 1, item.node.end)

    def open_extract(self, fs_or_start, start: Optional[int] = None, end: Optional[int] = None) -> None:
        if not isinstance(fs_or_start, FileState):  # open_extract(start, end) in single-file mode
            fs_or_start, start, end = self._single, fs_or_start, start
        fs = fs_or_start
        screen = ExtractScreen(fs.source, fs.rel, start, end, self.write_tests)
        self.push_screen(screen, lambda outcome: self._apply(fs, outcome))

    def _apply(self, fs: FileState, outcome: Optional[Tuple[ExtractResult, bool]]) -> None:
        if not outcome:
            return
        result, want_test = outcome
        if fs.path.read_text(encoding="utf-8") != fs.source:
            self.notify(f"{fs.rel} changed on disk since it was loaded; reload (r) and retry.", severity="error")
            return
        backup = apply_to_disk(fs.path, result.new_source)
        msg = f"Extracted {result.name}() in {fs.rel} · backup: {backup.name}"
        if want_test:
            test_path = write_test(result, fs.path, self.tests_dir)
            msg += f"\nTest draft: {test_path}"
        # re-analyse just this file, in place
        self._plans = {k: v for k, v in self._plans.items() if k[0] != fs.path}
        fs.root = None
        self.expanded.add(fs.path)
        self.load_file(fs)
        ui = self.file_nodes.get(fs.path)
        if ui is not None:
            ui.set_label(self._file_ui_label(fs))
        self.notify(msg, title="Refactored", timeout=10)

    def action_toggle_sort(self) -> None:
        self.sort_by_size = not self.sort_by_size
        self.populate()
        self.notify("Biggest first" if self.sort_by_size else "Source order")

    def action_expand_all(self) -> None:
        tree = self.query_one("#tree", Tree)
        if len(self.files) > 25:  # expanding a big project would parse every file
            node = tree.cursor_node or tree.root
            node.expand_all()
        else:
            tree.root.expand_all()

    def action_reload(self) -> None:
        self._plans.clear()
        self.discover()
        self.notify("Reloaded")
