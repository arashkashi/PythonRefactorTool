"""Command line entry point.

    pyrefactor FOLDER_OR_FILE             interactive tree (click a block to extract it, in place)
    pyrefactor tree FILE                  print the size-weighted tree
    pyrefactor analyze FILE --lines A-B   show what extracting A-B would take/return
    pyrefactor extract FILE --lines A-B --name NAME [--rename old=new ...] [--yes]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from rich.console import Console
from rich.markup import escape
from rich.syntax import Syntax
from rich.tree import Tree as RichTree

from .analyzer import TreeNode, build_tree, largest, size_scale
from .extract import RefactorError, analyze, extract
from .render import apply_to_disk, legend, node_label, parse_range
from .testgen import write_test

SUBCOMMANDS = {"tui", "tree", "analyze", "extract"}


def _existing_file(value: str) -> Path:
    path = Path(value)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"{value}: no such file")
    return path


def _existing_path(value: str) -> Path:
    path = Path(value)
    if not path.exists():
        raise argparse.ArgumentTypeError(f"{value}: no such file or folder")
    return path


def _tree_opts(p: argparse.ArgumentParser, folder_ok: bool = False) -> None:
    if folder_ok:
        p.add_argument("file", type=_existing_path, help="project folder or Python file to refactor")
    else:
        p.add_argument("file", type=_existing_file, help="Python file to refactor")
    p.add_argument("--depth", type=int, default=3, help="block nesting levels shown below a function (default 3)")
    p.add_argument("--min-lines", type=int, default=5, help="hide blocks shorter than this (default 5)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pyrefactor", description="Browse and refactor a Python file with LibCST.")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("tui", help="interactive tree (default when only a folder/file is given)")
    _tree_opts(p, folder_ok=True)
    p.add_argument("--no-tests", action="store_true", help="do not offer unit-test drafts by default")
    p.add_argument("--tests-dir", type=Path, help="where to write test drafts (default: <project>/tests)")

    p = sub.add_parser("tree", help="print the size-weighted outline")
    _tree_opts(p)
    p.add_argument("--sort", action="store_true", help="sort siblings by size instead of source order")
    p.add_argument("--no-blocks", action="store_true", help="only modules, classes and functions")

    p = sub.add_parser("analyze", help="show the inferred signature for a line range")
    p.add_argument("file", type=_existing_file)
    p.add_argument("--lines", required=True, help="line range, e.g. 120-180")

    p = sub.add_parser("extract", help="extract a line range into a new function")
    p.add_argument("file", type=_existing_file)
    p.add_argument("--lines", required=True, help="line range, e.g. 120-180")
    p.add_argument("--name", help="new function name (default: suggested name)")
    p.add_argument("--rename", action="append", default=[], metavar="OLD=NEW", help="rename an argument")
    p.add_argument("--yes", "-y", action="store_true", help="apply without asking")
    p.add_argument("--dry-run", action="store_true", help="only print the diff")
    p.add_argument("--no-tests", action="store_true", help="do not write a unit-test draft")
    p.add_argument("--tests-dir", type=Path, help="where to write test drafts (default: <file dir>/tests)")
    return parser


def _print_tree(console: Console, root: TreeNode, sort: bool, blocks: bool) -> None:
    scale = size_scale(root)
    rt = RichTree(node_label(root, scale))

    def add(rnode: RichTree, node: TreeNode) -> None:
        kids = [c for c in node.children if blocks or c.kind != "block"]
        if sort:
            kids.sort(key=lambda c: -c.loc)
        for child in kids:
            add(rnode.add(node_label(child, scale)), child)

    add(rt, root)
    console.print(legend(scale))
    console.print(rt)
    console.print("\n[bold]Largest functions/methods[/bold] (refactoring candidates):")
    for n in largest(root, n=5):
        console.print(f"  {n.loc:>5} lines  {n.qualname}  (L{n.start}-{n.end})")


def _print_plan(console: Console, plan) -> None:
    console.print(f"[bold]Enclosing:[/bold] {plan.qualname}   [bold]as:[/bold] {plan.kind}")
    if (plan.source_start, plan.source_end) != plan.requested:
        console.print(
            f"[yellow]Snapped lines {plan.requested[0]}-{plan.requested[1]} to whole statements: "
            f"{plan.source_start}-{plan.source_end}[/yellow]"
        )
    else:
        console.print(f"[bold]Lines:[/bold] {plan.source_start}-{plan.source_end} ({plan.loc})")
    console.print(f"[bold]Suggested name:[/bold] {plan.suggested_name}")
    for p in plan.params:
        hints = ", ".join(plan.param_hints.get(p, []))
        g = plan.param_types[p]
        console.print(f"  [cyan]arg[/cyan] {p}: {escape(g.label)}  [dim]({escape(g.how)})[/dim]"
                      + (f"   [dim]used as: {hints}[/dim]" if hints else ""), highlight=False)
    returns = ", ".join(f"{r}: {plan.return_types[r].label}" for r in plan.returns) or "(nothing)"
    if len(plan.returns) > 1:
        returns += f"  -> {plan.returns_type}"
    console.print(f"[bold]Returns:[/bold] {escape(returns)}",
                  highlight=False)
    for w in plan.warnings:
        console.print(f"[yellow]⚠ {w}[/yellow]")
    for p in plan.problems:
        console.print(f"[bold red]✗ {p}[/bold red]")


def _ask(prompt: str, default: str) -> str:
    answer = input(f"{prompt} [{default}]: ").strip()
    return answer or default


def _cmd_extract(args, console: Console) -> int:
    source = args.file.read_text(encoding="utf-8")
    start, end = parse_range(args.lines)
    plan = analyze(source, start, end)
    _print_plan(console, plan)
    if not plan.ok:
        return 2
    interactive = not args.yes and not args.dry_run and sys.stdin.isatty()

    name = args.name or plan.suggested_name
    renames = {}
    for item in args.rename:
        old, _, new = item.partition("=")
        renames[old.strip()] = new.strip()
    if interactive:  # confirm the name and every argument, like the TUI dialog
        name = _ask("Function name", name)
        for p in plan.params:
            renames[p] = _ask(f"  argument `{p}`", renames.get(p, p))

    result = extract(source, start, end, name=name, renames=renames, filename=args.file.name)
    console.print(Syntax(result.diff, "diff", theme="ansi_dark"))
    if args.dry_run:
        return 0
    if interactive and _ask("Apply this change? (y/n)", "y").lower() not in ("y", "yes"):
        console.print("Aborted.")
        return 1
    backup = apply_to_disk(args.file, result.new_source)
    console.print(f"[green]✓ wrote {args.file}[/green]  (backup: {backup})")
    if not args.no_tests:
        console.print(f"[green]✓ test draft:[/green] {write_test(result, args.file, args.tests_dir)}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in SUBCOMMANDS and not argv[0].startswith("-"):
        argv.insert(0, "tui")  # `pyrefactor file.py` opens the interactive tree
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help()
        return 1
    console = Console()
    try:
        if args.cmd == "tui":
            from .tui import RefactorApp  # imported lazily: textual is slow to import

            RefactorApp(
                args.file,
                max_block_depth=args.depth,
                min_block_lines=args.min_lines,
                write_tests=not args.no_tests,
                tests_dir=args.tests_dir,
            ).run()
            return 0
        if args.cmd == "tree":
            root = build_tree(args.file.read_text(encoding="utf-8"), args.file.name, args.depth, args.min_lines)
            _print_tree(console, root, args.sort, not args.no_blocks)
            return 0
        if args.cmd == "analyze":
            plan = analyze(args.file.read_text(encoding="utf-8"), *parse_range(args.lines))
            _print_plan(console, plan)
            return 0 if plan.ok else 2
        if args.cmd == "extract":
            return _cmd_extract(args, console)
    except RefactorError as exc:
        console.print(f"[bold red]✗ {exc}[/bold red]")
        return 2
    except ValueError as exc:
        console.print(f"[bold red]✗ {exc}[/bold red]")
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
