"""Tests for JSON rendering."""

import json

from codezoom.model import ExternalDep, NodeData, ProjectGraph, SymbolData
from codezoom.renderer.json import render_json


def _sample_graph():
    """Build a small ProjectGraph for testing."""
    graph = ProjectGraph(
        project_name="test-project",
        root_node_ids=["mypackage"],
    )
    graph.hierarchy["mypackage"] = NodeData(
        children=["mypackage.core", "mypackage.utils"],
    )
    graph.hierarchy["mypackage.core"] = NodeData(
        imports_to=["mypackage.utils"],
        symbols={
            "Processor": SymbolData(
                name="Processor",
                kind="class",
                line=10,
                children={
                    "run": SymbolData(name="run", kind="method", line=12),
                },
            ),
        },
    )
    graph.hierarchy["mypackage.utils"] = NodeData(
        imports_from=["mypackage.core"],
        symbols={
            "helper": SymbolData(name="helper", kind="function", line=1),
        },
    )
    graph.external_deps = [
        ExternalDep(name="requests", is_direct=True),
        ExternalDep(name="urllib3", is_direct=False),
    ]
    graph.external_deps_graph = {"requests": ["urllib3"]}
    return graph


def test_render_json_creates_valid_json(tmp_path):
    out = tmp_path / "out.json"
    render_json(_sample_graph(), out)

    data = json.loads(out.read_text())
    assert data["project_name"] == "test-project"
    assert data["root_node_ids"] == ["mypackage"]


def test_render_json_hierarchy_keys(tmp_path):
    out = tmp_path / "out.json"
    render_json(_sample_graph(), out)

    data = json.loads(out.read_text())
    assert set(data["hierarchy"]) == {"mypackage", "mypackage.core", "mypackage.utils"}


def test_render_json_symbols(tmp_path):
    out = tmp_path / "out.json"
    render_json(_sample_graph(), out)

    data = json.loads(out.read_text())
    fd = data["functionData"]
    assert "Processor" in fd["mypackage.core"]
    assert fd["mypackage.core"]["Processor"]["type"] == "class"
    assert "run" in fd["mypackage.core"]["Processor"]["methods"]


def test_render_json_external_deps(tmp_path):
    out = tmp_path / "out.json"
    render_json(_sample_graph(), out)

    data = json.loads(out.read_text())
    assert data["external_deps"] == ["requests", "urllib3"]
    assert data["external_deps_direct"] == ["requests"]
    assert data["external_deps_graph"] == {"requests": ["urllib3"]}


def test_render_json_creates_parent_dirs(tmp_path):
    out = tmp_path / "nested" / "dir" / "out.json"
    render_json(_sample_graph(), out)
    assert out.exists()
