"""Shared serialization logic for rendering a ProjectGraph."""

from __future__ import annotations

from codezoom.model import ProjectGraph, SymbolData


def _symbol_to_dict(sym: SymbolData) -> dict:
    d: dict = {"name": sym.name, "type": sym.kind}
    if sym.line is not None:
        d["lineno"] = sym.line
    if sym.calls:
        d["calls"] = sym.calls
    if sym.inherits:
        d["inherits"] = sym.inherits
    if sym.children:
        d["methods"] = {k: _symbol_to_dict(v) for k, v in sym.children.items()}
    if sym.visibility is not None:
        d["visibility"] = sym.visibility
    if sym.origin is not None:
        d["origin"] = sym.origin
    return d


def graph_to_dict(graph: ProjectGraph) -> dict:
    """Convert a *ProjectGraph* to the dictionary representation used by all output formats."""
    hierarchy: dict = {}
    function_data: dict = {}

    for node_id, node in graph.hierarchy.items():
        entry = {
            "children": node.children,
            "imports_from": node.imports_from,
            "imports_to": node.imports_to,
        }
        if node.class_deps:
            entry["class_deps"] = node.class_deps
        if not node.is_exported:
            entry["is_exported"] = False
        hierarchy[node_id] = entry
        if node.symbols:
            function_data[node_id] = {
                k: _symbol_to_dict(v) for k, v in node.symbols.items()
            }

    external_deps_all = sorted(d.name for d in graph.external_deps)
    external_deps_direct = sorted(d.name for d in graph.external_deps if d.is_direct)

    return {
        "project_name": graph.project_name,
        "root_node_ids": graph.root_node_ids,
        "hierarchy": hierarchy,
        "functionData": function_data,
        "external_deps": external_deps_all,
        "external_deps_direct": external_deps_direct,
        "external_deps_graph": graph.external_deps_graph,
        "module_direct_deps": graph.module_direct_deps,
        "cycles": graph.cycles,
    }
