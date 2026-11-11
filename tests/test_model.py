"""Tests for the data model."""

from __future__ import annotations

from codezoom.model import NodeData, ProjectGraph


def test_sort_keys_sorts_dicts_but_not_lists():
    graph = ProjectGraph(
        project_name="p",
        root_node_ids=["b", "a"],
        hierarchy={"b": NodeData(children=["z", "y"]), "a": NodeData()},
        external_deps_graph={"y": ["2", "1"], "x": []},
        module_direct_deps={"n": [], "m": []},
    )

    graph.sort_keys()

    assert list(graph.hierarchy) == ["a", "b"]
    assert list(graph.external_deps_graph) == ["x", "y"]
    assert list(graph.module_direct_deps) == ["m", "n"]
    assert graph.root_node_ids == ["b", "a"]
    assert graph.hierarchy["b"].children == ["z", "y"]
    assert graph.external_deps_graph["y"] == ["2", "1"]
