"""Tests for the Graphviz DOT extractor."""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path

import pytest

from codezoom.extractors.dot import (
    DotError,
    _display_label,
    _lift_edge,
    extract_dot_file,
)
from codezoom.model import ProjectGraph
from codezoom.pipeline import run

needs_graphviz = pytest.mark.skipif(
    shutil.which("gvpr") is None, reason="Graphviz (gvpr) not installed"
)


def _extract(src: str, tmp_path: Path) -> tuple[ProjectGraph, bool]:
    dot_file = tmp_path / "test.dot"
    dot_file.write_text(textwrap.dedent(src), encoding="utf-8")
    graph = ProjectGraph(project_name="test")
    directed = extract_dot_file(dot_file, graph)
    return graph, directed


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def test_display_label_escapes():
    assert _display_label(r"\N", False, "a", "G") == "a"
    assert _display_label(r"\N in \G", False, "a", "G") == "a in G"
    assert _display_label(r"one\ntwo\lthree", False, "a", "G") == "one\ntwo\nthree"
    assert _display_label(r"back\\slash", False, "a", "G") == "back\\slash"


def test_display_label_html():
    label = "<b>bold</b> &amp; <i>more</i><br/>next"
    assert _display_label(label, True, "a", "G") == "bold & more\nnext"


def test_display_label_plain_angle_brackets_kept():
    assert _display_label("<b>", False, "a", "G") == "<b>"


# ---------------------------------------------------------------------------
# Edge lifting
# ---------------------------------------------------------------------------


def test_lift_edge_siblings():
    parents = {"a": None, "b": None}
    assert _lift_edge("a", "b", parents) == ("a", "b")


def test_lift_edge_across_clusters():
    parents = {"A": None, "B": None, "x": "A", "y": "B", "z": None}
    assert _lift_edge("x", "y", parents) == ("A", "B")
    assert _lift_edge("x", "z", parents) == ("A", "z")


def test_lift_edge_within_cluster():
    parents = {"A": None, "x": "A", "y": "A"}
    assert _lift_edge("x", "y", parents) == ("x", "y")


def test_lift_edge_self_loop():
    assert _lift_edge("a", "a", {"a": None}) is None


# ---------------------------------------------------------------------------
# extract_dot_file
# ---------------------------------------------------------------------------


@needs_graphviz
def test_extract_flat_digraph(tmp_path):
    graph, directed = _extract(
        """
        digraph G {
            a -> b;
            b -> c;
        }
        """,
        tmp_path,
    )

    assert directed is True
    assert set(graph.hierarchy) == {"a", "b", "c"}
    assert graph.root_node_ids == ["a", "b", "c"]
    assert graph.hierarchy["a"].imports_to == ["b"]
    assert graph.hierarchy["b"].imports_to == ["c"]
    assert graph.hierarchy["b"].imports_from == ["a"]


@needs_graphviz
def test_extract_undirected(tmp_path):
    graph, directed = _extract("graph G { a -- b; }", tmp_path)
    assert directed is False
    assert graph.hierarchy["a"].imports_to == ["b"]


@needs_graphviz
def test_extract_edge_chain_and_duplicates(tmp_path):
    graph, _ = _extract("digraph G { a -> b -> c; a -> b; }", tmp_path)
    assert graph.hierarchy["a"].imports_to == ["b"]
    assert graph.hierarchy["b"].imports_to == ["c"]
    assert graph.hierarchy["b"].imports_from == ["a"]


@needs_graphviz
def test_extract_labels(tmp_path):
    graph, _ = _extract(
        """
        digraph G {
            a [label="Alpha"];
            b;
            "org.example.Foo";
            h [label=<<b>Bold</b>>];
        }
        """,
        tmp_path,
    )

    assert graph.hierarchy["a"].label == "Alpha"
    assert graph.hierarchy["b"].label == "b"
    # Dotted IDs keep their full name rather than being split like packages.
    assert graph.hierarchy["org.example.Foo"].label == "org.example.Foo"
    assert graph.hierarchy["h"].label == "Bold"


@needs_graphviz
def test_extract_quoted_ids(tmp_path):
    graph, _ = _extract('digraph G { "Module A" -> "Module B"; }', tmp_path)
    assert graph.hierarchy["Module A"].imports_to == ["Module B"]


@needs_graphviz
def test_extract_clusters(tmp_path):
    graph, _ = _extract(
        """
        digraph G {
            subgraph cluster_A { label="Alpha"; x; y; }
            subgraph cluster_B { z; }
            w;
            x -> z;
            y -> w;
            x -> y;
        }
        """,
        tmp_path,
    )

    assert graph.root_node_ids == ["cluster_A", "cluster_B", "w"]
    assert graph.hierarchy["cluster_A"].children == ["x", "y"]
    assert graph.hierarchy["cluster_B"].children == ["z"]
    assert graph.hierarchy["cluster_A"].label == "Alpha"
    assert graph.hierarchy["cluster_B"].label == "cluster_B"

    # Edges between clusters are lifted to the cluster level.
    assert graph.hierarchy["cluster_A"].imports_to == ["cluster_B", "w"]
    assert graph.hierarchy["cluster_B"].imports_from == ["cluster_A"]
    assert graph.hierarchy["w"].imports_from == ["cluster_A"]
    # Edges within a cluster stay between its members.
    assert graph.hierarchy["x"].imports_to == ["y"]


@needs_graphviz
def test_extract_nested_clusters(tmp_path):
    graph, _ = _extract(
        """
        digraph G {
            subgraph cluster_outer {
                label="Outer";
                a;
                subgraph same_rank {
                    rank=same;
                    subgraph cluster_inner { b; }
                    c;
                }
            }
        }
        """,
        tmp_path,
    )

    # Non-cluster subgraphs add no level; nodes go to their deepest cluster.
    assert graph.root_node_ids == ["cluster_outer"]
    assert graph.hierarchy["cluster_outer"].children == ["cluster_inner", "a", "c"]
    assert graph.hierarchy["cluster_inner"].children == ["b"]
    # The inner cluster does not inherit the outer cluster's label.
    assert graph.hierarchy["cluster_inner"].label == "cluster_inner"


@needs_graphviz
def test_extract_cluster_attribute(tmp_path):
    graph, _ = _extract(
        """
        digraph G {
            subgraph group { cluster=true; a; }
            subgraph cluster_no { cluster=false; b; }
        }
        """,
        tmp_path,
    )

    assert graph.root_node_ids == ["group", "b"]
    assert graph.hierarchy["group"].children == ["a"]


@needs_graphviz
def test_extract_cluster_named_like_node(tmp_path):
    graph, _ = _extract(
        """
        digraph G {
            subgraph cluster_A { x; }
            cluster_A -> x;
        }
        """,
        tmp_path,
    )

    assert graph.hierarchy["cluster_A (cluster)"].children == ["x"]
    assert graph.hierarchy["cluster_A"].imports_to == ["cluster_A (cluster)"]


@needs_graphviz
def test_extract_syntax_error(tmp_path):
    with pytest.raises(DotError, match="syntax error"):
        _extract("digraph G { a -- b; }", tmp_path)


@needs_graphviz
def test_extract_empty_file(tmp_path):
    with pytest.raises(DotError):
        _extract("", tmp_path)


@needs_graphviz
def test_extract_nonexistent_file(tmp_path):
    graph = ProjectGraph(project_name="test")
    with pytest.raises(DotError):
        extract_dot_file(tmp_path / "missing.dot", graph)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


@needs_graphviz
def test_run_dot_file(tmp_path):
    dot_file = tmp_path / "deps.gv"
    dot_file.write_text("digraph G { a -> b -> a; }", encoding="utf-8")

    out_path = run(dot_file, json_output=True)

    assert out_path == tmp_path / "deps.json"
    data = json.loads(out_path.read_text())
    assert data["project_name"] == "deps"
    assert data["hierarchy"]["a"]["label"] == "a"
    assert data["cycles"]


@needs_graphviz
def test_run_dot_file_undirected_has_no_cycles(tmp_path):
    dot_file = tmp_path / "net.dot"
    dot_file.write_text("graph G { a -- b -- c -- a; }", encoding="utf-8")

    data = json.loads(run(dot_file, json_output=True).read_text())

    assert data["cycles"] == []
