"""Orchestrator: detect → extract → render."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from codezoom.detect import detect_extractors
from codezoom.model import ProjectGraph
from codezoom.renderer.html import render_html

logger = logging.getLogger(__name__)


def _guess_project_name(project_dir: Path) -> str:
    """Guess the project display name from pyproject.toml, pom.xml, or directory name."""
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

    pom_path = project_dir / "pom.xml"
    if pom_path.exists():
        try:
            from jgo.maven.pom import POM

            pom = POM(pom_path)
            name = pom.name or pom.artifactId
            if name:
                return name
        except Exception:
            pass

    return project_dir.name.replace("-", "_")


def _find_package_name(project_dir: Path) -> str | None:
    """Discover the actual importable package name.

    For Python projects the package name may differ from the project name
    (e.g. project ``pyimagej`` provides package ``imagej``).  We look for a
    directory containing an ``__init__.py`` under ``src/`` (src-layout) or
    directly under the project root (flat layout), skipping common
    non-package dirs.

    For Java projects we return None — the hierarchy extractor computes
    ``root_node_id`` from jdeps output.
    """
    # Java projects: root_node_id set by JavaPackageHierarchyExtractor.
    if (project_dir / "pom.xml").exists() and not (
        project_dir / "pyproject.toml"
    ).exists():
        return None

    _SKIP = {
        ".git",
        ".github",
        ".tox",
        ".venv",
        ".eggs",
        ".mypy_cache",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        "docs",
        "doc",
        "tests",
        "test",
        "scripts",
        "bin",
        "examples",
    }

    # src-layout: look for src/<pkg>/__init__.py
    src = project_dir / "src"
    if src.is_dir():
        for child in sorted(src.iterdir()):
            if (
                child.is_dir()
                and child.name not in _SKIP
                and (child / "__init__.py").exists()
            ):
                return child.name

    # flat layout: look for <pkg>/__init__.py at the project root
    for child in sorted(project_dir.iterdir()):
        if (
            child.is_dir()
            and child.name not in _SKIP
            and (child / "__init__.py").exists()
        ):
            return child.name

    return None


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
    root_node_id = _find_package_name(project_dir) or project_name

    graph = ProjectGraph(
        project_name=project_name,
        root_node_id=root_node_id,
    )

    logger.debug("Project: %s, root_node_id: %s", project_name, root_node_id)

    extractors = detect_extractors(project_dir, project_name)
    if not extractors:
        logger.error("Could not detect project type.")
        sys.exit(1)

    logger.debug("Extractors: %s", [type(e).__name__ for e in extractors])

    for ext in extractors:
        if ext.can_handle(project_dir):
            ext.extract(project_dir, graph)

    out_path = output or (project_dir / f"{project_name}_deps.html")
    render_html(graph, out_path)

    logger.info("Generated %s", out_path)

    if open_browser:
        import webbrowser

        webbrowser.open(out_path.as_uri())

    return out_path
