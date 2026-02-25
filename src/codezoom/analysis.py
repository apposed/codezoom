"""Post-extraction graph analysis (cycle detection, etc.)."""

from __future__ import annotations

from codezoom.model import NodeData


def find_cycles(hierarchy: dict[str, NodeData]) -> list[list[str]]:
    """Return strongly-connected components of size ≥ 2 using Tarjan's algorithm.

    Each returned list is a group of node IDs that are mutually reachable via
    ``imports_to`` edges — i.e. a dependency cycle.  Nodes that are not part of
    any cycle are omitted.
    """
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[list[str]] = []
    counter = [0]

    def _visit(v: str) -> None:
        index[v] = lowlink[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        for w in hierarchy[v].imports_to:
            if w not in hierarchy:
                continue
            if w not in index:
                _visit(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])

        if lowlink[v] == index[v]:
            scc: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) >= 2:
                sccs.append(scc)

    for v in hierarchy:
        if v not in index:
            _visit(v)

    return sccs


def find_class_cycles(hierarchy: dict[str, NodeData]) -> list[list[str]]:
    """Return SCCs of size ≥ 2 at the class level using ``class_deps`` data.

    Same-package class→class edges (targets without a dot) are the only ones
    that can form class-level cycles here; cross-package entries resolve to a
    package node ID and are ignored.  Each returned list contains fully-qualified
    class node IDs of the form ``"pkg.name:ClassName"``.
    """
    # Build adjacency list over class node IDs.
    graph: dict[str, list[str]] = {}

    for pkg_id, node in hierarchy.items():
        if not node.class_deps:
            continue
        for src_class, targets in node.class_deps.items():
            src_id = f"{pkg_id}:{src_class}"
            if src_id not in graph:
                graph[src_id] = []
            for target in targets:
                if "." in target:
                    continue  # cross-package edge: target is a package ID, not a class
                tgt_id = f"{pkg_id}:{target}"
                graph[src_id].append(tgt_id)
                if tgt_id not in graph:
                    graph[tgt_id] = []

    if not graph:
        return []

    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[list[str]] = []
    counter = [0]

    def _visit(v: str) -> None:
        index[v] = lowlink[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        for w in graph[v]:
            if w not in index:
                _visit(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])

        if lowlink[v] == index[v]:
            scc: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) >= 2:
                sccs.append(scc)

    for v in graph:
        if v not in index:
            _visit(v)

    return sccs
