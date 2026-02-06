"""Orchestrator: detect → extract → render."""

from __future__ import annotations

import sys
from pathlib import Path

from codezoom.detect import detect_extractors
from codezoom.model import ProjectGraph
from codezoom.renderer.html import render_html


def _guess_project_name(project_dir: Path) -> str:
    """Guess the project (package) name from pyproject.toml or directory name."""
    pyproject = project_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            import tomllib

            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
            name = data.get("project", {}).get("name")
            if name:
                # Normalise: PyPI allows hyphens but Python packages use underscores
                return name.replace("-", "_")
        except Exception:
            pass
    return project_dir.name.replace("-", "_")


def run(
    project_dir: Path,
    *,
    output: Path | None = None,
    name: str | None = None,
    open_browser: bool = False,
) -> Path:
    """Run the full codezoom pipeline and return the output path."""
    project_dir = project_dir.resolve()
    project_name = name or _guess_project_name(project_dir)
    root_node_id = project_name  # top-level node in the hierarchy

    graph = ProjectGraph(
        project_name=project_name,
        root_node_id=root_node_id,
    )

    extractors = detect_extractors(project_dir, project_name)
    if not extractors:
        print("Error: could not detect project type.", file=sys.stderr)
        sys.exit(1)

    for ext in extractors:
        if ext.can_handle(project_dir):
            ext.extract(project_dir, graph)

    out_path = output or (project_dir / f"{project_name}_deps.html")
    render_html(graph, out_path)

    print(f"Generated {out_path}")

    if open_browser:
        import webbrowser

        webbrowser.open(out_path.as_uri())

    return out_path
