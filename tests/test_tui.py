"""Drive the Textual UI headlessly: click a block, confirm name/args, apply."""
import textwrap

from textual.widgets import Input, Tree

from pyrefactor.tui import DiffScreen, ExtractScreen, RefactorApp

SRC = textwrap.dedent(
    '''\
    def summarize(orders):
        total = 0
        lines = []
        for o in orders:
            amount = o["qty"] * o["price"]
            total += amount
            lines.append(f"{o['sku']}: {amount}")
            if amount > 100:
                lines.append("  big order")
        print("\\n".join(lines))
        return total
    '''
)


def _find(tree: Tree, kind: str):
    stack = [tree.root]
    while stack:
        node = stack.pop()
        if node.data is not None and node.data.kind == "code" and node.data.node.kind == kind:
            return node
        stack.extend(node.children)
    return None


async def test_click_block_confirm_and_apply(tmp_path):
    target = tmp_path / "shop.py"
    target.write_text(SRC)
    app = RefactorApp(target, min_block_lines=2)
    async with app.run_test(size=(140, 45)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        tree = app.query_one("#tree", Tree)
        fn = _find(tree, "function")
        fn.expand()
        await pilot.pause()
        block = _find(tree, "block")
        assert block.data.node.header.startswith("for o in orders")

        tree.move_cursor(block)
        await pilot.pause()
        tree.action_select_cursor()  # same as clicking / pressing enter on the node
        await pilot.pause()
        assert isinstance(app.screen, ExtractScreen)

        dialog = app.screen
        assert dialog.plan.params == ["orders", "total", "lines"]
        assert dialog.plan.returns == ["total"]
        dialog.query_one("#fname", Input).value = "add_order_lines"
        dialog.query_one("#param-0", Input).value = "order_rows"
        await pilot.click("#preview")
        await pilot.pause()
        assert isinstance(app.screen, DiffScreen)
        await pilot.click("#apply")
        await pilot.pause()

    new = target.read_text()
    assert "total = add_order_lines(orders, total, lines)" in new
    assert "def add_order_lines(order_rows, total, lines):" in new
    assert (tmp_path / "shop.py.bak").read_text() == SRC
    test_draft = tmp_path / "tests" / "test_shop_add_order_lines.py"
    assert test_draft.exists()
    compile(test_draft.read_text(), str(test_draft), "exec")

    ns_old, ns_new = {}, {}
    exec(SRC, ns_old)
    exec(new, ns_new)
    orders = [{"sku": "A", "qty": 2, "price": 70}, {"sku": "B", "qty": 1, "price": 5}]
    assert ns_old["summarize"](orders) == ns_new["summarize"](orders)


async def test_refused_block_disables_preview(tmp_path):
    target = tmp_path / "loop.py"
    target.write_text(
        textwrap.dedent(
            """\
            def first_negative(xs):
                for x in xs:
                    if x < 0:
                        print("found")
                        return x
                return None
            """
        )
    )
    app = RefactorApp(target, min_block_lines=2)
    async with app.run_test(size=(140, 45)) as pilot:
        await app.workers.wait_for_complete()
        app.open_extract(2, 5)
        await pilot.pause()
        assert app.screen.query_one("#preview").disabled
        assert app.screen.plan.problems


async def test_project_folder_sorted_ranked_and_refactored_in_place(tmp_path):
    import subprocess
    import sys

    (tmp_path / "pkg").mkdir()
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text("x = 1\n")
    (tmp_path / "small.py").write_text("def tiny():\n    return 1\n")
    (tmp_path / "pkg" / "shop.py").write_text(SRC + "\n\ndef helper(a):\n    return a + 1\n")

    app = RefactorApp(tmp_path, min_block_lines=2)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        assert sorted(f.rel for f in app.files.values()) == ["pkg/shop.py", "small.py"]  # .venv skipped
        tree = app.query_one("#tree", Tree)
        top = [n.data for n in tree.root.children]
        assert top[0].kind == "dir" and top[1].kind == "file"  # pkg/ (bigger) before small.py

        shop_ui = app.file_nodes[tmp_path / "pkg" / "shop.py"]
        assert shop_ui.data.file.root is None  # not parsed until expanded
        shop_ui.expand()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        funcs = [c.data.node for c in shop_ui.children]
        assert [f.name for f in funcs] == ["summarize", "helper"]  # biggest first
        assert "#1 largest" in str(shop_ui.children[0].label)

        shop_ui.children[0].expand()
        await pilot.pause()
        block = _find(tree, "block")
        tree.move_cursor(block)
        await pilot.pause()
        tree.action_select_cursor()
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, ExtractScreen)
        dialog.query_one("#fname", Input).value = "add_order_lines"
        await pilot.click("#preview")
        await pilot.pause()
        await pilot.click("#apply")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.pause()
        # the new function is tagged and selected, even though biggest-first sorting puts it lower down
        cur = tree.cursor_node
        assert cur.data.node.name == "add_order_lines", cur.label
        assert "✚ new" in str(cur.label)

    new = (tmp_path / "pkg" / "shop.py").read_text()
    assert "def add_order_lines(" in new
    assert (tmp_path / "pkg" / "shop.py.bak").exists()
    draft = tmp_path / "tests" / "test_shop_add_order_lines.py"
    assert draft.exists()
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(draft)],
                         capture_output=True, text=True, cwd=tmp_path)
    assert "skipped" in run.stdout and "error" not in run.stdout.lower(), run.stdout
