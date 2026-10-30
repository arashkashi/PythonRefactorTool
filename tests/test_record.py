"""Run a program once, record calls of the extracted function, and write a runnable test."""
import subprocess
import sys
import textwrap

from pyrefactor.extract import extract
from pyrefactor.record import default_command, record_calls
from pyrefactor.render import apply_to_disk
from pyrefactor.testgen import write_test

SHOP = textwrap.dedent(
    '''\
    import random
    from dataclasses import dataclass


    @dataclass
    class Item:
        sku: str
        qty: int


    class Basket:
        def __init__(self):
            self.lines = []

        def add(self, items, seen, rng):
            # --- merge lines
            # Repeated SKUs are merged into one line.
            for item in items:
                if item.sku in seen:
                    seen[item.sku].qty += item.qty
                    print("merged", item.sku)
                else:
                    seen[item.sku] = Item(item.sku, item.qty)
                    self.lines.append(seen[item.sku])
            bonus = rng.randint(1, 3)
            return bonus


    def checkout(rows):
        total = 0
        for qty, price in rows:
            if qty <= 0:
                raise ValueError("bad qty %d" % qty)
            total += qty * price
        return total


    def main():
        rng = random.Random(7)
        b = Basket()
        seen = {}
        print(b.add([Item("A", 1), Item("B", 2)], seen, rng))
        print(b.add([Item("A", 5)], seen, rng))
        print(checkout([(1, 2.5), (2, 1.0)]))
        try:
            checkout([(0, 1.0)])
        except ValueError:
            print("refused")


    if __name__ == "__main__":
        main()
    '''
)


def _extract_record(tmp_path, start, end, name):
    target = tmp_path / "shop.py"
    if not target.exists():
        target.write_text(SHOP)
    result = extract(target.read_text(), start, end, name)
    apply_to_disk(target, result.new_source)
    rec = record_calls(target, result, default_command(target, tmp_path), tmp_path)
    return target, result, rec


def _pytest(tmp_path, path):
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(path)],
                          capture_output=True, text=True, cwd=tmp_path)


def test_method_with_in_place_changes_and_output(tmp_path):
    target, result, rec = _extract_record(tmp_path, 18, 24, "merge_lines")  # the for loop in Basket.add
    assert rec.ok, rec.problem
    assert rec.calls == 2 and len(rec.cases) == 2  # new SKUs only, then a merge: two code paths
    test = write_test(result, target, tmp_path / "tests", rec)
    text = test.read_text()
    assert "TODO" not in text and "skip" not in text
    assert "obj.merge_lines(items, seen)" in text
    assert "capsys.readouterr().out == 'merged A\\n'" in text
    run = _pytest(tmp_path, test)
    assert "2 passed" in run.stdout, run.stdout + run.stderr

    doc = target.read_text()
    assert "Merge lines: one step of `Basket.add`" in doc
    assert "Repeated SKUs are merged into one line." in doc
    assert "Updates `seen` in place; sets `self.lines`" not in doc  # lines is appended to, not assigned


def test_exception_path_and_inline_values(tmp_path):
    target, result, rec = _extract_record(tmp_path, 31, 34, "add_rows")  # the loop in checkout
    assert len(rec.cases) == 2
    text = write_test(result, target, tmp_path / "tests", rec).read_text()
    assert "rows = [(1, 2.5), (2, 1.0)]" in text
    assert "assert result == 4.5" in text
    assert "pytest.raises(ValueError, match=re.escape('bad qty 0'))" in text
    assert not (tmp_path / "tests" / "data").exists()  # everything fit inline
    assert "2 passed" in _pytest(tmp_path, tmp_path / "tests").stdout


def test_function_never_called_falls_back_to_draft(tmp_path):
    target = tmp_path / "lib.py"
    target.write_text("def f(xs):\n    out = []\n    for x in xs:\n        out.append(x * 2)\n    return out\n")
    result = extract(target.read_text(), 3, 4, "double_all")
    apply_to_disk(target, result.new_source)
    rec = record_calls(target, result, f"{sys.executable} -c pass", tmp_path)
    assert not rec.ok and "never called" in rec.problem
    text = write_test(result, target, tmp_path / "tests", rec).read_text()
    assert "No values could be recorded" in text and "skip" in text
