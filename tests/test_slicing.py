"""The Returns section shows the statements that compute each returned value."""
import textwrap

from pyrefactor.extract import _Analysis, extract
from pyrefactor.slicing import backward_slice

SRC = textwrap.dedent(
    '''\
    def receive(pos, inventory, log):
        received = 0
        skipped = []
        for po in pos:
            if po["status"] != "ready":
                skipped.append(po["id"])
                continue
            log.write("po %s" % po["id"])
            for sku, qty in po["lines"]:
                inventory.receive(sku, qty)
                bonus = 1 if qty > 10 else 0
                received += qty + bonus
        return received, skipped
    '''
)


def _slice(name):
    an = _Analysis(SRC, 2, 12)
    return backward_slice(an.stmts, an.parents, [name])


def test_slice_keeps_guards_and_data_dependencies_only():
    assert _slice("received") == textwrap.dedent(
        '''\
        received = 0
        for po in pos:
            if po["status"] != "ready":
                continue
            for sku, qty in po["lines"]:
                bonus = 1 if qty > 10 else 0
                received += qty + bonus'''
    )


def test_slice_for_a_list_built_with_append():
    assert _slice("skipped") == textwrap.dedent(
        '''\
        skipped = []
        for po in pos:
            if po["status"] != "ready":
                skipped.append(po["id"])
                continue'''
    )


def test_docstring_shows_derivation_with_renames_and_side_effect_calls():
    new = extract(SRC, 2, 12, "tally", renames={"pos": "orders"}).new_source
    assert "Updates `log` in place; calls `inventory.receive()`; returns `received`, `skipped`." in new
    assert "inventory (Any): May be changed by `inventory.receive()`." in new
    assert "received (int), computed by:" in new
    assert "            for po in orders:\n" in new  # renamed argument, also inside the derivation
    assert "inventory.receive" not in new.split("computed by:")[1].split('"""')[0]  # not part of the value
    compile(new, "x.py", "exec")
