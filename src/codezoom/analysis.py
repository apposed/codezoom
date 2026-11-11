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

    def _visit(root: str) -> None:
        # Note: Iterative rather than recursive, so long dependency chains
        # do not exceed Python's recursion limit.
        index[root] = lowlink[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(hierarchy[root].imports_to))]

        while work:
            v, targets = work[-1]
            descended = False
            for w in targets:
                if w not in hierarchy:
                    continue
                if w not in index:
                    index[w] = lowlink[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(hierarchy[w].imports_to)))
                    descended = True
                    break
                if w in on_stack:
                    lowlink[v] = min(lowlink[v], index[w])
            if descended:
                continue

            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[v])

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


def aggregate_imports(root_id: str, hierarchy: dict[str, dict[str, set]]) -> None:
    """Aggregate imports bottom-up from leaves to parents.

    *hierarchy* maps node IDs to ``children``, ``imports_to`` and
    ``imports_from`` sets. Afterwards, each node's sets also include those of
    its descendants, minus edges that stay within its own subtree.
    """
    # Post-order traversal: children before parents. Nodes are marked when
    # first reached, so a malformed hierarchy with cycles cannot loop forever.
    order: list[str] = []
    reached = {root_id}
    stack = [(root_id, iter(sorted(hierarchy[root_id]["children"])))]
    while stack:
        node_id, children = stack[-1]
        child_id = next((c for c in children if c not in reached), None)
        if child_id is None:
            stack.pop()
            order.append(node_id)
        else:
            reached.add(child_id)
            stack.append((child_id, iter(sorted(hierarchy[child_id]["children"]))))

    subtree: dict[str, set[str]] = {}
    for node_id in order:
        node = hierarchy[node_id]
        within = {node_id}
        all_to = set(node["imports_to"])
        all_from = set(node["imports_from"])
        for child_id in node["children"]:
            within |= subtree.get(child_id, set())
            all_to |= hierarchy[child_id]["imports_to"]
            all_from |= hierarchy[child_id]["imports_from"]
        subtree[node_id] = within
        node["imports_to"] = all_to - within
        node["imports_from"] = all_from - within


def reaggregate_imports(
    root_ids: list[str],
    hierarchy: dict[str, NodeData],
    members: set[str] | None = None,
) -> None:
    """Re-run :func:`aggregate_imports` over nodes already in a graph.

    If *members* is given, only those nodes (and the roots) take part.
    """
    keep = set(hierarchy) if members is None else (members | set(root_ids))
    raw = {
        node_id: {
            "children": {c for c in node.children if c in keep and c in hierarchy},
            "imports_to": set(node.imports_to),
            "imports_from": set(node.imports_from),
        }
        for node_id, node in hierarchy.items()
        if node_id in keep
    }
    for root_id in root_ids:
        if root_id in raw:
            aggregate_imports(root_id, raw)
    for node_id, entry in raw.items():
        hierarchy[node_id].imports_to = sorted(entry["imports_to"])
        hierarchy[node_id].imports_from = sorted(entry["imports_from"])


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
