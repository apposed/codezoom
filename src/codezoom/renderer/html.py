"""Render a ProjectGraph to a standalone HTML file."""

from __future__ import annotations

import json
from pathlib import Path
from string import Template

from codezoom.model import ProjectGraph, SymbolData

_TEMPLATE_PATH = Path(__file__).with_name("template.html")


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
    return d


def _graph_to_json(graph: ProjectGraph) -> str:
    """Serialize *graph* into the JSON blob consumed by the template JS."""
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

    data = {
        "project_name": graph.project_name,
        "root_node_ids": graph.root_node_ids,
        "hierarchy": hierarchy,
        "functionData": function_data,
        "external_deps": external_deps_all,
        "external_deps_direct": external_deps_direct,
        "external_deps_graph": graph.external_deps_graph,
        "module_direct_deps": graph.module_direct_deps,
    }
    return json.dumps(data)


def render_html(graph: ProjectGraph, output_path: Path) -> None:
    """Write the interactive HTML visualization to *output_path*."""
    template = Template(_TEMPLATE_PATH.read_text())
    data_json = _graph_to_json(graph)
    html = template.safe_substitute(DATA_JSON=data_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)
