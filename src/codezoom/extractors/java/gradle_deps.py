"""Extract external dependencies from Gradle via ./gradlew dependencies."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from codezoom.model import ExternalDep, ProjectGraph

logger = logging.getLogger(__name__)

# Configurations to query (in order of preference)
_CONFIGURATIONS = ["runtimeClasspath", "compileClasspath"]


def _is_gradle_project(project_dir: Path) -> bool:
    return (project_dir / "build.gradle.kts").exists() or (
        project_dir / "build.gradle"
    ).exists()


def _find_gradle_executable(project_dir: Path) -> str:
    """Find gradlew or fall back to gradle."""
    gradlew = project_dir / "gradlew"
    if gradlew.exists():
        return str(gradlew)
    return "gradle"


def _normalize_dep_name(coord: str) -> str:
    """Normalize a dependency coordinate to group:artifact (strip version)."""
    # Handle formats like:
    # - group:artifact:version
    # - group:artifact:version -> other:version (constraint)
    # - group:artifact:version (*)
    # - group:artifact:version (c)

    # Strip constraint arrows and markers
    coord = coord.split(" -> ")[0]
    coord = coord.split(" (")[0]

    parts = coord.split(":")
    if len(parts) >= 2:
        return f"{parts[0]}:{parts[1]}"
    return coord


def _parse_dependency_tree(output: str) -> tuple[set[str], dict[str, list[str]]]:
    """Parse Gradle dependency tree output into deps and graph.

    Returns:
        (all_deps, dep_graph) where:
        - all_deps: set of all dependency names (group:artifact)
        - dep_graph: dict mapping parent -> list of children
    """
    all_deps: set[str] = set()
    dep_graph: dict[str, list[str]] = {}

    # Stack to track current path in the tree
    # Each entry is (indentation_level, dependency_name)
    stack: list[tuple[int, str]] = []

    # Regex to match tree lines like:
    # +--- org.example:foo:1.0.0
    # |    +--- org.example:bar:2.0.0
    # \--- org.example:baz:3.0.0
    tree_line_re = re.compile(r"^([|+ \\\-]+)(.*)")

    for line in output.splitlines():
        match = tree_line_re.match(line)
        if not match:
            continue

        prefix, dep_str = match.groups()
        dep_str = dep_str.strip()

        if not dep_str or dep_str.startswith("project "):
            # Skip project dependencies (internal modules)
            continue

        # Calculate indentation level (number of tree characters / 4 or 5)
        # Gradle uses patterns like "+--- ", "|    ", "\--- "
        indent_level = len(prefix) // 5
        if indent_level == 0 and len(prefix) > 0:
            indent_level = 1

        dep_name = _normalize_dep_name(dep_str)
        all_deps.add(dep_name)

        # Pop stack until we're at the right parent level
        while stack and stack[-1][0] >= indent_level:
            stack.pop()

        # If we have a parent, add this as a child
        if stack:
            parent_name = stack[-1][1]
            if parent_name not in dep_graph:
                dep_graph[parent_name] = []
            if dep_name not in dep_graph[parent_name]:
                dep_graph[parent_name].append(dep_name)

        # Push current dep onto stack
        stack.append((indent_level, dep_name))

    return all_deps, dep_graph


class GradleDepsExtractor:
    """Populate external_deps and external_deps_graph via Gradle."""

    def can_handle(self, project_dir: Path) -> bool:
        return _is_gradle_project(project_dir)

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        gradle_exec = _find_gradle_executable(project_dir)

        # Try each configuration until one works
        for config in _CONFIGURATIONS:
            try:
                cmd = [gradle_exec, "dependencies", "--configuration", config]
                logger.debug("Running: %s", " ".join(cmd))

                result = subprocess.run(
                    cmd,
                    cwd=project_dir,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )

                if result.returncode != 0:
                    logger.debug(
                        "Gradle dependencies failed for %s (exit %d), trying next config",
                        config,
                        result.returncode,
                    )
                    continue

                # Parse the tree output
                all_deps, dep_graph = _parse_dependency_tree(result.stdout)

                if not all_deps:
                    logger.debug(
                        "No dependencies found in %s, trying next config", config
                    )
                    continue

                # Determine which deps are direct (appear at the top level)
                # Direct deps are those that appear in the output with minimal indentation
                direct_deps: set[str] = set()
                for line in result.stdout.splitlines():
                    # Match lines like "+--- group:artifact:version" or "\--- group:artifact:version"
                    # These are top-level (direct) dependencies
                    if re.match(r"^[+\\]---", line):
                        dep_str = line.split("---", 1)[1].strip()
                        if dep_str and not dep_str.startswith("project "):
                            direct_deps.add(_normalize_dep_name(dep_str))

                # Build external_deps list
                graph.external_deps = [
                    ExternalDep(name=name, is_direct=(name in direct_deps))
                    for name in sorted(all_deps)
                ]
                graph.external_deps_graph = dep_graph

                logger.debug(
                    "Gradle deps (%s): %d total (%d direct)",
                    config,
                    len(all_deps),
                    len(direct_deps),
                )
                return

            except subprocess.TimeoutExpired:
                logger.warning("Gradle dependencies timed out for %s", config)
                continue
            except (OSError, subprocess.SubprocessError) as e:
                logger.warning("Could not run gradle for %s: %s", config, e)
                continue

        # If we get here, all configurations failed
        logger.warning("Could not extract Gradle dependencies from any configuration")
