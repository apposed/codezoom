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
