"""Java extractors for Maven and Gradle projects."""

from __future__ import annotations

from pathlib import Path

from codezoom.extractors.java.ast_symbols import JavaAstSymbolsExtractor
from codezoom.extractors.java.gradle_deps import GradleDepsExtractor
from codezoom.extractors.java.maven_deps import JavaMavenDepsExtractor
from codezoom.extractors.java.package_hierarchy import JavaPackageHierarchyExtractor

__all__ = [
    "GradleDepsExtractor",
    "JavaAstSymbolsExtractor",
    "JavaMavenDepsExtractor",
    "JavaPackageHierarchyExtractor",
]


def _find_classes_dir(project_dir: Path) -> Path | None:
    """Find compiled classes directory for Maven or Gradle projects.

    Returns the first existing directory, or None.
    """
    candidates = [
        project_dir / "target" / "classes",  # Maven
        project_dir / "build" / "classes" / "java" / "main",  # Gradle
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None
