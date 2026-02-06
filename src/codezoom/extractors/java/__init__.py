"""Java extractors — stubs for future implementation."""

from __future__ import annotations

from pathlib import Path

from codezoom.model import ProjectGraph


class JavaMavenDeps:
    """Extract dependencies from pom.xml / build.gradle. (Not yet implemented.)"""

    def can_handle(self, project_dir: Path) -> bool:
        return (project_dir / "pom.xml").exists() or (project_dir / "build.gradle").exists()

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        pass  # TODO: parse pom.xml / build.gradle


class JavaPackageHierarchy:
    """Extract Java package/class hierarchy. (Not yet implemented.)"""

    def can_handle(self, project_dir: Path) -> bool:
        return (project_dir / "pom.xml").exists() or (project_dir / "build.gradle").exists()

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        pass  # TODO: walk src/main/java tree


class JavaAstSymbols:
    """Extract methods/fields from Java classes. (Not yet implemented.)"""

    def can_handle(self, project_dir: Path) -> bool:
        return (project_dir / "pom.xml").exists() or (project_dir / "build.gradle").exists()

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        pass  # TODO: parse Java AST
