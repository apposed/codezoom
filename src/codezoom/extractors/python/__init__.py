"""Python extractors — shared helpers."""

from __future__ import annotations

from pathlib import Path


def is_python_project(project_dir: Path) -> bool:
    """Return True if *project_dir* contains a Python project indicator."""
    return (project_dir / "pixi.toml").exists() or (
        project_dir / "pyproject.toml"
    ).exists()
