"""Java extractors for Maven and Gradle projects."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from codezoom.extractors.java.ast_symbols import JavaAstSymbolsExtractor
from codezoom.extractors.java.gradle_deps import GradleDepsExtractor
from codezoom.extractors.java.maven_deps import JavaMavenDepsExtractor
from codezoom.extractors.java.package_hierarchy import JavaPackageHierarchyExtractor

logger = logging.getLogger(__name__)

__all__ = [
    "GradleDepsExtractor",
    "JavaAstSymbolsExtractor",
    "JavaMavenDepsExtractor",
    "JavaPackageHierarchyExtractor",
]


def _discover_maven_modules(project_dir: Path) -> list[str]:
    """Parse <modules> from root pom.xml."""
    pom_path = project_dir / "pom.xml"
    if not pom_path.exists():
        return []
    try:
        from jgo.maven import POM

        pom = POM(pom_path)
        return pom.values("modules/module")
    except ImportError:
        logger.debug("jgo not installed — cannot discover Maven modules")
        return []
    except (OSError, ValueError, KeyError) as e:
        logger.debug("Could not parse pom.xml for modules: %s", e)
        return []


def _discover_gradle_subprojects(project_dir: Path) -> list[str]:
    """Parse include() from settings.gradle(.kts) — stub for future Gradle support."""
    for name in ("settings.gradle.kts", "settings.gradle"):
        settings_path = project_dir / name
        if settings_path.exists():
            try:
                text = settings_path.read_text()
                # Match include("subproject") or include 'subproject'
                # and include(":subproject") forms
                matches = re.findall(
                    r"""include\s*\(?\s*["':]+([^"')]+)["']\s*\)?""",
                    text,
                )
                return [m.lstrip(":") for m in matches]
            except OSError as e:
                logger.debug("Could not parse %s: %s", name, e)
    return []


def _find_module_classes(project_dir: Path) -> dict[str, list[Path]]:
    """Map module names to their compiled classes directories.

    Returns a dict mapping module name -> list of classes dirs.
    For single-module projects, returns a single entry keyed by "".
    For multi-module, each key is the module directory name.
    """
    # Try Maven multi-module
    maven_modules = _discover_maven_modules(project_dir)
    if maven_modules:
        result: dict[str, list[Path]] = {}
        for module in maven_modules:
            candidate = project_dir / module / "target" / "classes"
            if candidate.is_dir():
                result[module] = [candidate]
        if result:
            return result

    # Fallback: single Maven
    single_maven = project_dir / "target" / "classes"
    if single_maven.is_dir():
        return {"": [single_maven]}

    # Try Gradle multi-project
    gradle_subprojects = _discover_gradle_subprojects(project_dir)
    if gradle_subprojects:
        result = {}
        for subproject in gradle_subprojects:
            candidate = project_dir / subproject / "build" / "classes" / "java" / "main"
            if candidate.is_dir():
                result[subproject] = [candidate]
        if result:
            return result

    # Fallback: single Gradle
    single_gradle = project_dir / "build" / "classes" / "java" / "main"
    if single_gradle.is_dir():
        return {"": [single_gradle]}

    return {}


def _find_classes_dirs(project_dir: Path) -> list[Path]:
    """Find all compiled classes directories for Maven/Gradle projects.

    Discovers multi-module Maven and multi-project Gradle layouts, falling
    back to single-module conventions.
    """
    module_classes = _find_module_classes(project_dir)
    dirs: list[Path] = []
    for module_dirs in module_classes.values():
        dirs.extend(module_dirs)
    return dirs


def _find_classes_dir(project_dir: Path) -> Path | None:
    """Find compiled classes directory for Maven or Gradle projects.

    Returns the first existing directory, or None.
    """
    dirs = _find_classes_dirs(project_dir)
    return dirs[0] if dirs else None
