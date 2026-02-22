"""Orchestrator: detect → extract → render."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from codezoom.analysis import find_cycles
from codezoom.detect import detect_extractors
from codezoom.model import ProjectGraph
from codezoom.renderer.html import render_html

logger = logging.getLogger(__name__)


def _guess_project_name(project_dir: Path) -> str:
    """Guess the project display name from pixi.toml, pyproject.toml, pom.xml, or directory name."""
    # Try pixi.toml first (pixi manages the environment when present)
    for toml_name in ("pixi.toml", "pyproject.toml"):
        toml_path = project_dir / toml_name
        if toml_path.exists():
            try:
                import tomllib

                with open(toml_path, "rb") as f:
                    data = tomllib.load(f)
                name = data.get("project", {}).get("name")
                if name:
                    # Normalise: PyPI allows hyphens but Python packages use underscores
                    return name.replace("-", "_")
            except (OSError, tomllib.TOMLDecodeError, KeyError):
                pass

    pom_path = project_dir / "pom.xml"
    if pom_path.exists():
        try:
            from jgo.maven.pom import POM

            pom = POM(pom_path)
            name = pom.name or pom.artifactId
            if name:
                return name
        except (ImportError, OSError, ValueError):
            pass

    # Gradle: try settings.gradle.kts or build.gradle.kts for project name
    for settings_name in ("settings.gradle.kts", "settings.gradle"):
        settings_path = project_dir / settings_name
        if settings_path.exists():
            name = _guess_gradle_name(settings_path)
            if name:
                return name

    for build_name in ("build.gradle.kts", "build.gradle"):
        build_path = project_dir / build_name
        if build_path.exists():
            name = _guess_gradle_name_from_build(build_path)
            if name:
                return name

    # Cargo.toml: workspace or package name
    cargo_toml = project_dir / "Cargo.toml"
    if cargo_toml.exists():
        try:
            import tomllib

            with open(cargo_toml, "rb") as f:
                data = tomllib.load(f)
            # Try [package] name first (single-crate project)
            name = data.get("package", {}).get("name")
            if name:
                return name
            # Workspace: fall back to directory name
        except (OSError, tomllib.TOMLDecodeError, KeyError):
            pass

    return project_dir.name.replace("-", "_")


def _guess_gradle_name(settings_path: Path) -> str | None:
    """Extract project name from settings.gradle.kts or settings.gradle."""
    import re

    try:
        content = settings_path.read_text(encoding="utf-8")
    except OSError:
        return None

    # Try rootProject.name = "..."
    m = re.search(r'rootProject\.name\s*=\s*"([^"]+)"', content)
    if m:
        return m.group(1)

    # Try name = "..." inside qupathExtension or qupath block
    m = re.search(r'name\s*=\s*"([^"]+)"', content)
    if m:
        return m.group(1)

    return None


def _guess_gradle_name_from_build(build_path: Path) -> str | None:
    """Extract project name from build.gradle(.kts) top-level assignment."""
    import re

    try:
        content = build_path.read_text(encoding="utf-8")
    except OSError:
        return None

    # Look for a top-level name assignment: name = "..."
    m = re.search(r'^name\s*=\s*"([^"]+)"', content, re.MULTILINE)
    if m:
        return m.group(1)

    return None


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
    from codezoom.extractors.python import is_python_project

    # Maven projects: root_node_id set by JavaPackageHierarchyExtractor.
    if (project_dir / "pom.xml").exists() and not is_python_project(project_dir):
        return None

    # Gradle projects: root_node_id set by JavaPackageHierarchyExtractor.
    if (
        (project_dir / "build.gradle.kts").exists()
        or (project_dir / "build.gradle").exists()
    ) and not is_python_project(project_dir):
        return None

    # Rust projects: root_node_ids set by RustModuleHierarchyExtractor.
    if (project_dir / "Cargo.toml").exists() and not is_python_project(project_dir):
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
        root_node_ids=[root_node_id],
    )

    logger.debug("Project: %s, root_node_ids: %s", project_name, graph.root_node_ids)

    extractors = detect_extractors(project_dir, project_name)
    if not extractors:
        logger.error("Could not detect project type.")
        sys.exit(1)

    logger.debug("Extractors: %s", [type(e).__name__ for e in extractors])

    for ext in extractors:
        if ext.can_handle(project_dir):
            ext.extract(project_dir, graph)

    graph.cycles = find_cycles(graph.hierarchy)
    logger.debug("Cycles detected: %d", len(graph.cycles))

    out_path = output or (project_dir / "codezoom.html")
    render_html(graph, out_path)

    logger.info("Generated %s", out_path)

    if open_browser:
        import webbrowser

        webbrowser.open(out_path.as_uri())

    return out_path
