import textwrap

import pytest

from pyrefactor.analyzer import build_tree, size_scale
from pyrefactor.extract import RefactorError, analyze, extract

SRC = textwrap.dedent(
    '''\
    import math


    class Cart:
        def __init__(self):
            self.rate = 0.2

        def total(self, items, coupon):
            subtotal = 0
            # --- sum line items ---
            for it in items:
                subtotal += it["price"] * it["qty"]
                if it["qty"] > 10:
                    subtotal -= 1
            if coupon:
                subtotal *= 0.9
            tax = subtotal * self.rate
            return subtotal + tax


    def report(rows):
        out = []
        count = 0
        for r in rows:
            if not r:
                continue
            out.append(str(r))
            count += 1
        if count > 2:
            label = "many"
        print(len(out), math.pi > 3)
        return out


    def running(values):
        acc = 0
        seen = []
        for v in values:
            if v > 0:
                acc += v
                seen.append(acc)
            print(v)
        return acc, seen
    '''
)


def run(source):
    ns = {}
    exec(compile(source, "<test>", "exec"), ns)
    items = [{"price": 2.5, "qty": 3}, {"price": 1.0, "qty": 12}]
    return (
        ns["Cart"]().total(items, True),
        ns["Cart"]().total(items, False),
        ns["report"]([1, 0, 2, 3]),
        ns["running"]([1, -2, 3]),
    )


def test_method_using_self_becomes_method_and_preserves_behaviour():
    res = extract(SRC, 15, 17, "apply_coupon", {"coupon": "discount_code"})
    assert res.plan.kind == "method"
    assert res.plan.params == ["coupon", "subtotal"]
    assert res.plan.returns == ["subtotal", "tax"]
    assert "def apply_coupon(self, discount_code, subtotal):" in res.new_source
    assert "subtotal, tax = self.apply_coupon(coupon, subtotal)" in res.new_source
    assert run(res.new_source) == run(SRC)


def test_block_without_self_in_method_becomes_staticmethod():
    res = extract(SRC, 11, 14, "sum_line_items")
    assert res.plan.kind == "staticmethod"
    assert "@staticmethod" in res.new_source
    assert run(res.new_source) == run(SRC)


def test_module_function_with_inputs_and_outputs():
    res = extract(SRC, 23, 28, "collect")
    assert res.plan.kind == "function"
    assert res.plan.params == ["rows", "out"]  # count is initialised inside the selection
    assert res.plan.returns == ["count"]
    assert run(res.new_source) == run(SRC)


def test_loop_body_extraction_carries_accumulator():
    plan = analyze(SRC, 39, 41)  # the `if v > 0:` block inside the loop
    assert plan.params == ["v", "acc", "seen"]
    assert plan.returns == ["acc"]
    res = extract(SRC, 39, 41, "accumulate")
    assert run(res.new_source) == run(SRC)


def test_section_comment_drives_suggested_name():
    assert analyze(SRC, 11, 14).suggested_name == "sum_line_items"


def test_refuses_continue_to_outer_loop():
    plan = analyze(SRC, 25, 26)
    assert not plan.ok
    with pytest.raises(RefactorError, match="continue"):
        extract(SRC, 25, 26, "nope")


def test_refuses_return():
    with pytest.raises(RefactorError, match="return"):
        extract(SRC, 17, 18, "nope")


@pytest.mark.parametrize(
    "name, renames, match",
    [
        ("class", None, "identifier"),
        ("report", None, "already defined"),
        ("ok_name", {"coupon": "subtotal"}, "unique"),
        ("ok_name", {"coupon": "tax"}, "already used"),
        ("ok_name", {"nope": "x"}, "Not parameters"),
    ],
)
def test_validates_user_input(name, renames, match):
    with pytest.raises(RefactorError, match=match):
        extract(SRC, 15, 17, name, renames)


def test_tree_levels_and_scale():
    root = build_tree(SRC, "x.py", min_block_lines=2)
    kinds = [(n.kind, n.name) for n in root.children]
    assert kinds == [("class", "Cart"), ("function", "report"), ("function", "running")]
    total = root.children[0].children[1]
    assert total.kind == "method" and [c.name for c in total.children] == ["for", "if"]
    scale = size_scale(root)
    assert scale.level(scale.max_loc) == 3


REUSE = textwrap.dedent(
    '''\
    def pipeline(orders, mode):
        totals = {}
        for o in orders:
            print("validate", o)
        for o in orders:
            t = 0
            for q in o:
                t += q
            totals[len(totals)] = t
        if mode == "a":
            t = 1
        elif mode == "b":
            t = 2
        else:
            t = 3
        for o in orders:
            print(o, t)
        return totals
    '''
)


def test_rebound_loop_variables_are_not_args_or_returns():
    plan = analyze(REUSE, 5, 9)
    assert plan.params == ["orders", "totals"]  # not `o`, `t`, `q`: each is bound before use inside
    assert plan.returns == []  # later reads of `o`/`t` see later bindings (for / if-elif-else)
    ns_old, ns_new = {}, {}
    exec(REUSE, ns_old)
    exec(extract(REUSE, 5, 9, "sum_orders").new_source, ns_new)
    assert ns_old["pipeline"]([[1, 2], [3]], "b") == ns_new["pipeline"]([[1, 2], [3]], "b")
