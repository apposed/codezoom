"""Render a ProjectGraph to a standalone JSON file."""

from __future__ import annotations

import json
from pathlib import Path

from codezoom.model import ProjectGraph
from codezoom.renderer._serialize import graph_to_dict


def render_json(graph: ProjectGraph, output_path: Path) -> None:
    """Write the project structure data to *output_path* as formatted JSON."""
    data = graph_to_dict(graph)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2) + "\n")
