"""Read Graphviz DOT files into a ProjectGraph, using Graphviz's own parser."""

from __future__ import annotations

import html
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from codezoom.model import NodeData, ProjectGraph

logger = logging.getLogger(__name__)

_GVPR_SCRIPT = Path(__file__).with_name("dot.gvpr")

# Separators emitted by dot.gvpr.
_RS = "\x1e"
_US = "\x1f"


class DotError(Exception):
    """Raised when a DOT file cannot be read."""


@dataclass
class _Subgraph:
    name: str
    parent: str
    label: str
    is_html: bool
    cluster_attr: str
    members: list[str] = field(default_factory=list)


@dataclass
class _DotGraph:
    """The raw structure of a DOT graph, as reported by gvpr."""

    name: str = ""
    directed: bool = True
    label: str = ""
    subgraphs: dict[str, _Subgraph] = field(default_factory=dict)
    node_labels: dict[str, tuple[str, bool]] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)


def extract_dot_file(dot_path: Path, graph: ProjectGraph) -> bool:
    """Populate *graph* from the DOT file at *dot_path*.

    Clusters become parent nodes whose children are the clusters and nodes
    directly inside them. Each edge is lifted to the pair of siblings that
    separates its endpoints, so it shows up at every level of the hierarchy.

    Returns whether the graph is directed.
    Raises DotError if Graphviz is missing or cannot parse the file.
    """
    dot = _run_gvpr(dot_path)

    clusters = {name: sg for name, sg in dot.subgraphs.items() if _is_cluster(sg)}

    # Node IDs for clusters, kept apart from any regular node of the same name.
    cluster_ids = {
        name: f"{name} (cluster)" if name in dot.node_labels else name
        for name in clusters
    }

    def cluster_parent(sg: _Subgraph) -> str | None:
        """Return the nearest enclosing cluster of *sg*, if any."""
        parent = dot.subgraphs.get(sg.parent)
        while parent is not None and parent.name not in clusters:
            parent = dot.subgraphs.get(parent.parent)
        return parent.name if parent is not None else None

    parents: dict[str, str | None] = {}
    depth: dict[str, int] = {}
    for name, sg in clusters.items():
        parent = cluster_parent(sg)
        parents[cluster_ids[name]] = cluster_ids[parent] if parent else None
    for cid, parent in parents.items():
        d, p = 0, parent
        while p is not None:
            d, p = d + 1, parents[p]
        depth[cid] = d

    # Each node belongs to the deepest cluster containing it. The members of a
    # subgraph include those of its nested subgraphs, so test all of them.
    for name, sg in clusters.items():
        cid = cluster_ids[name]
        for node in sg.members:
            owner = parents.get(node)
            if owner is None or depth[cid] > depth[owner]:
                parents[node] = cid
    for node in dot.node_labels:
        parents.setdefault(node, None)

    # Build the hierarchy: clusters first, then nodes, in declaration order.
    for name, sg in clusters.items():
        label = sg.label
        parent_sg = dot.subgraphs.get(sg.parent)
        inherited = parent_sg.label if parent_sg else dot.label
        # Note: Graphviz gives nested subgraphs their parent's label by default.
        if label == inherited:
            label = ""
        graph.hierarchy[cluster_ids[name]] = NodeData(
            label=_display_label(label, sg.is_html, name, dot.name) or name,
        )
    for node, (label, is_html) in dot.node_labels.items():
        graph.hierarchy[node] = NodeData(
            label=_display_label(label, is_html, node, dot.name) or node,
        )
    for nid, parent in parents.items():
        if parent is not None:
            graph.hierarchy[parent].children.append(nid)

    graph.root_node_ids = [nid for nid, parent in parents.items() if parent is None]

    for src, tgt in dot.edges:
        lifted = _lift_edge(src, tgt, parents)
        if lifted is None:
            continue
        a, b = lifted
        if b not in graph.hierarchy[a].imports_to:
            graph.hierarchy[a].imports_to.append(b)
        if a not in graph.hierarchy[b].imports_from:
            graph.hierarchy[b].imports_from.append(a)

    logger.debug(
        "DOT: %d nodes, %d edges, %d clusters, %d root nodes",
        len(dot.node_labels),
        len(dot.edges),
        len(clusters),
        len(graph.root_node_ids),
    )
    return dot.directed


def _run_gvpr(dot_path: Path) -> _DotGraph:
    gvpr = shutil.which("gvpr")
    if gvpr is None:
        raise DotError("gvpr not found; install Graphviz to read DOT files")
    try:
        result = subprocess.run(
            [gvpr, "-f", str(_GVPR_SCRIPT), str(dot_path)],
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except subprocess.TimeoutExpired as e:
        raise DotError(f"gvpr timed out reading {dot_path}") from e

    # Note: gvpr exits 0 even on syntax errors, so check for its output instead.
    dot = _parse_records(result.stdout)
    if dot is None:
        msg = result.stderr.strip() or "no graph found"
        raise DotError(f"Cannot read {dot_path}: {msg}")
    return dot


def _parse_records(output: str) -> _DotGraph | None:
    """Parse the records emitted by dot.gvpr; return None if there is no graph."""
    dot: _DotGraph | None = None
    for record in output.split(_RS):
        kind, *fields = record.split(_US)
        if kind == "G" and dot is None:
            dot = _DotGraph(name=fields[0], directed=fields[1] == "1", label=fields[2])
        elif dot is None:
            continue
        elif kind == "S":
            name, parent, label, is_html, cluster_attr = fields
            dot.subgraphs[name] = _Subgraph(
                name, parent, label, is_html == "1", cluster_attr
            )
        elif kind == "M":
            dot.subgraphs[fields[0]].members.append(fields[1])
        elif kind == "N":
            dot.node_labels[fields[0]] = (fields[1], fields[2] == "1")
        elif kind == "E":
            dot.edges.append((fields[0], fields[1]))
    return dot


def _is_cluster(sg: _Subgraph) -> bool:
    """Whether Graphviz would draw *sg* as a cluster."""
    if sg.cluster_attr:
        return sg.cluster_attr.lower() in ("true", "yes", "1")
    return sg.name.startswith("cluster")


def _display_label(label: str, is_html: bool, name: str, graph_name: str) -> str:
    """Turn a Graphviz label into plain display text."""
    if is_html:
        text = re.sub(r"<br\s*/?>", "\n", label, flags=re.IGNORECASE)
        return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()

    def escape(m: re.Match) -> str:
        c = m.group(1)
        if c == "N":
            return name
        if c == "G":
            return graph_name
        if c in "nlr":
            return "\n"
        return c

    return re.sub(r"\\(.)", escape, label).strip()


def _lift_edge(
    src: str, tgt: str, parents: dict[str, str | None]
) -> tuple[str, str] | None:
    """Return the ancestors of *src* and *tgt* that are siblings of each other.

    Returns None when one endpoint contains the other, or for self-loops.
    """

    def ancestry(nid: str) -> list[str]:
        chain = [nid]
        while (p := parents[chain[-1]]) is not None:
            chain.append(p)
        return chain[::-1]

    src_chain, tgt_chain = ancestry(src), ancestry(tgt)
    i = 0
    while i < len(src_chain) and i < len(tgt_chain) and src_chain[i] == tgt_chain[i]:
        i += 1
    if i == len(src_chain) or i == len(tgt_chain):
        return None
    return src_chain[i], tgt_chain[i]
