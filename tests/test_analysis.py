"""Tests for graph analysis (cycle detection)."""

from codezoom.analysis import find_class_cycles, find_cycles
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
