"""Suggested types for extracted arguments/results, and where they end up."""
import textwrap

from pyrefactor.extract import analyze, extract

SRC = textwrap.dedent(
    '''\
    from collections import defaultdict
    from typing import Dict, List


    class Order:
        pass


    def money(x) -> float:
        return round(x, 2)


    def report(orders: List[Order], rates: Dict[str, float], mode, sink):
        total = 0
        names = []
        by_mode = defaultdict(float)
        label = "x" if mode else "y"
        for order in orders:
            rate = rates.get("eu", 1.0)
            total += money(rate * 2)
            names.append("%s" % order)
            by_mode[label] += rate
            sink.write(order)
            first = order
        return total, names, by_mode, first
    '''
)


def test_types_from_annotations_assignments_loops_and_usage():
    plan = analyze(SRC, 19, 23)  # rate = ... through sink.write(order)
    t = {p: (g.type, g.how) for p, g in plan.param_types.items()}
    assert t["order"] == ("Order", "inferred")  # for order in List[Order]
    assert t["rates"] == ("Dict[str, float]", "annotated")
    assert t["total"] == ("float", "inferred")  # 0 and += float -> float
    assert t["names"] == ("list", "inferred")
    assert t["by_mode"] == ("defaultdict[Any, float]", "inferred")
    assert t["label"] == ("str", "inferred")
    assert t["sink"][0] == "Any" and t["sink"][1] == "unknown"  # `.write()` says nothing certain
    assert t["by_mode"] == ("defaultdict[Any, float]", "inferred")


def test_usage_guess_is_marked():
    src = "def f(xs):\n    acc = xs\n    for x in acc:\n        acc.append(x)\n        print(x)\n"
    plan = analyze(src, 3, 5)
    g = plan.param_types["acc"]
    assert g.type == "list" and g.how.startswith("guessed")  # from `.append()`
    assert g.label == "list?"


def test_docstring_lists_types():
    new = extract(SRC, 19, 23, "step").new_source
    assert "rates (Dict[str, float]): Read only; used as: .get().  [type from an annotation]" in new
    assert "names (list): Updated in place (`names.append()`)" in new
    assert "TODO" not in new
    assert "float: the new value of `total`." in new
    compile(new, "x.py", "exec")
