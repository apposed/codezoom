"""Tests for graph analysis (cycle detection)."""

from codezoom.analysis import aggregate_imports, find_class_cycles, find_cycles
from codezoom.model import NodeData

# ---------------------------------------------------------------------------
# find_cycles (package-level)
# ---------------------------------------------------------------------------


def _hierarchy(*pairs):
    """Build a hierarchy dict from (id, imports_to) pairs."""
    return {node_id: NodeData(imports_to=list(deps)) for node_id, deps in pairs}


def test_find_cycles_no_cycle():
    h = _hierarchy(("a", ["b"]), ("b", ["c"]), ("c", []))
    assert find_cycles(h) == []


def test_find_cycles_simple():
    h = _hierarchy(("a", ["b"]), ("b", ["a"]))
    (cycle,) = find_cycles(h)
    assert set(cycle) == {"a", "b"}


def test_find_cycles_three_way():
    h = _hierarchy(("a", ["b"]), ("b", ["c"]), ("c", ["a"]))
    (cycle,) = find_cycles(h)
    assert set(cycle) == {"a", "b", "c"}


def test_find_cycles_self_loop_ignored():
    # A self-loop is size-1 SCC — must not be reported.
    h = _hierarchy(("a", ["a"]))
    assert find_cycles(h) == []


def test_find_cycles_missing_target_skipped():
    # Target node not in hierarchy — must not crash.
    h = _hierarchy(("a", ["b", "external"]), ("b", ["a"]))
    (cycle,) = find_cycles(h)
    assert set(cycle) == {"a", "b"}


def test_find_cycles_two_independent_cycles():
    h = _hierarchy(
        ("a", ["b"]),
        ("b", ["a"]),
        ("x", ["y"]),
        ("y", ["x"]),
    )
    cycles = find_cycles(h)
    assert len(cycles) == 2
    groups = {frozenset(c) for c in cycles}
    assert frozenset({"a", "b"}) in groups
    assert frozenset({"x", "y"}) in groups


# ---------------------------------------------------------------------------
# find_class_cycles
# ---------------------------------------------------------------------------


def _class_hierarchy(**pkg_deps):
    """Build a hierarchy dict from keyword args: pkg_id -> class_deps dict."""
    return {
        pkg_id: NodeData(class_deps=class_deps)
        for pkg_id, class_deps in pkg_deps.items()
    }


def test_find_class_cycles_mutual():
    """Two classes in the same package that reference each other form a cycle."""
    h = _class_hierarchy(
        **{"com.example": {"NDArrays": ["ShmImg"], "ShmImg": ["NDArrays"]}}
    )
    (cycle,) = find_class_cycles(h)
    assert set(cycle) == {"com.example:NDArrays", "com.example:ShmImg"}


def test_find_class_cycles_no_cycle():
    h = _class_hierarchy(**{"com.example": {"A": ["B"], "B": ["C"]}})
    assert find_class_cycles(h) == []


def test_find_class_cycles_cross_package_ignored():
    """Cross-package entries (targets containing '.') must not form class cycles."""
    h = _class_hierarchy(
        **{
            "com.a": {"Foo": ["com.b"]},
            "com.b": {"Bar": ["com.a"]},
        }
    )
    assert find_class_cycles(h) == []


def test_find_class_cycles_three_way():
    h = _class_hierarchy(**{"pkg": {"A": ["B"], "B": ["C"], "C": ["A"]}})
    (cycle,) = find_class_cycles(h)
    assert set(cycle) == {"pkg:A", "pkg:B", "pkg:C"}


def test_find_class_cycles_no_class_deps():
    """Nodes without class_deps must not crash or produce false cycles."""
    h = {
        "com.example": NodeData(imports_to=["com.other"]),
        "com.other": NodeData(),
    }
    assert find_class_cycles(h) == []


def test_find_class_cycles_independent_cycles_across_packages():
    h = _class_hierarchy(
        **{
            "pkg.a": {"X": ["Y"], "Y": ["X"]},
            "pkg.b": {"P": ["Q"], "Q": ["P"]},
        }
    )
    cycles = find_class_cycles(h)
    assert len(cycles) == 2
    groups = {frozenset(c) for c in cycles}
    assert frozenset({"pkg.a:X", "pkg.a:Y"}) in groups
    assert frozenset({"pkg.b:P", "pkg.b:Q"}) in groups


def test_find_cycles_long_chain():
    n = 5000
    hierarchy = {f"n{i}": NodeData(imports_to=[f"n{(i + 1) % n}"]) for i in range(n)}
    cycles = find_cycles(hierarchy)
    assert len(cycles) == 1
    assert len(cycles[0]) == n


def _raw(children=(), imports_to=(), imports_from=()):
    return {
        "children": set(children),
        "imports_to": set(imports_to),
        "imports_from": set(imports_from),
    }


def test_aggregate_imports_rolls_up_and_drops_internal():
    hierarchy = {
        "pkg": _raw(["pkg.a", "pkg.b"]),
        "pkg.a": _raw(["pkg.a.x"]),
        "pkg.a.x": _raw(imports_to=["pkg.b", "pkg.a"]),
        "pkg.b": _raw(imports_from=["pkg.a.x"]),
    }
    aggregate_imports("pkg", hierarchy)

    assert hierarchy["pkg.a"]["imports_to"] == {"pkg.b"}
    assert hierarchy["pkg"]["imports_to"] == set()
    assert hierarchy["pkg"]["imports_from"] == set()


def test_aggregate_imports_name_prefix_is_not_containment():
    hierarchy = {
        "pkg": _raw(["pkg.mod", "pkg.module"]),
        "pkg.mod": _raw(imports_to=["pkg.module"]),
        "pkg.module": _raw(imports_from=["pkg.mod"]),
    }
    aggregate_imports("pkg", hierarchy)

    assert hierarchy["pkg.mod"]["imports_to"] == {"pkg.module"}


def test_aggregate_imports_cyclic_children_terminates():
    hierarchy = {
        "a": _raw(["b"]),
        "b": _raw(["a"], imports_to=["c"]),
        "c": _raw(),
    }
    aggregate_imports("a", hierarchy)

    assert hierarchy["a"]["imports_to"] == {"c"}
