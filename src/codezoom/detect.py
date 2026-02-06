"""Auto-detect project type and return appropriate extractors."""

from __future__ import annotations

import sys
from pathlib import Path

from codezoom.extractors.base import Extractor
from codezoom.extractors.python.ast_symbols import AstSymbolsExtractor
from codezoom.extractors.python.module_hierarchy import ModuleHierarchyExtractor
from codezoom.extractors.python.package_deps import PackageDepsExtractor


def detect_extractors(project_dir: Path, project_name: str) -> list[Extractor]:
    """Return an ordered list of extractors applicable to *project_dir*."""
    extractors: list[Extractor] = []

    if (project_dir / "pyproject.toml").exists():
        # Read optional codezoom config
        exclude = _read_config_exclude(project_dir, project_name)
        extractors.append(PackageDepsExtractor())
        extractors.append(ModuleHierarchyExtractor(exclude=exclude))
        extractors.append(AstSymbolsExtractor())

    if (project_dir / "pom.xml").exists() or (project_dir / "build.gradle").exists():
        print(
            "Note: Java project detected — Java support coming soon.",
            file=sys.stderr,
        )

    return extractors


def _read_config_exclude(project_dir: Path, project_name: str) -> list[str] | None:
    """Read pydeps exclude list from .codezoom.toml or pyproject.toml."""
    import tomllib

    # Try .codezoom.toml first
    codezoom_toml = project_dir / ".codezoom.toml"
    if codezoom_toml.exists():
        try:
            with open(codezoom_toml, "rb") as f:
                data = tomllib.load(f)
            return data.get("codezoom", {}).get("exclude", None)
        except Exception:
            pass

    # Fall back to [tool.codezoom] in pyproject.toml
    pyproject = project_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
            return data.get("tool", {}).get("codezoom", {}).get("exclude", None)
        except Exception:
            pass

    return None
