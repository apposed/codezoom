"""Render a ProjectGraph to a standalone HTML file."""

from __future__ import annotations

import json
from pathlib import Path
from string import Template

from codezoom.model import ProjectGraph
from codezoom.renderer._serialize import graph_to_dict

_TEMPLATE_PATH = Path(__file__).with_name("template.html")


def render_html(graph: ProjectGraph, output_path: Path) -> None:
    """Write the interactive HTML visualization to *output_path*."""
    template = Template(_TEMPLATE_PATH.read_text())
    data_json = json.dumps(graph_to_dict(graph))
    html = template.safe_substitute(DATA_JSON=data_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)
