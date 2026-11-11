"""Tests for the Java package hierarchy builder."""

from __future__ import annotations

from codezoom.extractors.java.package_hierarchy import _build_hierarchical_data
from codezoom.model import ProjectGraph


def test_common_prefix_is_root():
    graph = ProjectGraph(project_name="proj")
    edges = [("org.example.a", "org.example.b")]
    _build_hierarchical_data(edges, [], graph)

    assert graph.root_node_ids == ["org.example"]
    assert graph.hierarchy["org.example"].children == [
        "org.example.a",
        "org.example.b",
    ]


def test_no_common_prefix_uses_project_root():
    graph = ProjectGraph(project_name="proj")
    edges = [
        ("bdv.util", "ch.epfl.biop"),
        ("sc.fiji.bdv", "bdv.util"),
    ]
    _build_hierarchical_data(edges, [], graph)

    assert graph.root_node_ids == ["proj"]
    assert graph.hierarchy["proj"].children == ["bdv", "ch", "sc"]
    # The synthetic root contains every package, so it imports none of them.
    assert graph.hierarchy["proj"].imports_to == []
    assert graph.hierarchy["bdv"].children == ["bdv.util"]
    assert graph.hierarchy["bdv"].imports_to == ["ch.epfl.biop"]
    assert graph.hierarchy["sc"].imports_to == ["bdv.util"]
