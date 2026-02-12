"""Build Java package hierarchy from source tree and import statements."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path

from codezoom.model import NodeData, ProjectGraph

logger = logging.getLogger(__name__)

# Match Java package declaration: package com.example.foo;
_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;")

# Match Java import statement: import com.example.Foo;
_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;")

# Standard JDK / common external package prefixes to exclude from internal import edges.
_EXTERNAL_PREFIXES = (
    "java.",
    "javax.",
    "javafx.",
    "jdk.",
    "sun.",
    "com.sun.",
    "org.w3c.",
    "org.xml.",
    "org.ietf.",
    # Common third-party
    "org.slf4j.",
    "ch.qos.logback.",
    "com.google.",
    "org.apache.",
    "org.junit.",
    "org.mockito.",
    "org.assertj.",
    "org.yaml.",
    "ome.",
    "loci.",
    "net.imagej.",
    "org.scijava.",
    "ai.djl.",
    "org.bytedeco.",
    "org.locationtech.",
    "org.controlsfx.",
    "org.jfree.",
    "org.fxmisc.",
    "org.commonmark.",
    "org.kordamp.",
    "org.jfxtras.",
    "dev.zarr.",
    "info.picocli.",
    "net.java.",
)


def _find_source_root(project_dir: Path) -> Path | None:
    """Find the Java source root directory."""
    # Standard Gradle/Maven layout
    src_main_java = project_dir / "src" / "main" / "java"
    if src_main_java.is_dir():
        return src_main_java
    # Kotlin source set (some Gradle projects)
    src_main_kotlin = project_dir / "src" / "main" / "kotlin"
    if src_main_kotlin.is_dir():
        return src_main_kotlin
    return None


def _is_gradle_project(project_dir: Path) -> bool:
    return (project_dir / "build.gradle.kts").exists() or (
        project_dir / "build.gradle"
    ).exists()


class GradlePackageHierarchyExtractor:
    """Populate hierarchy with Java package tree and inter-package imports from source."""

    def can_handle(self, project_dir: Path) -> bool:
        return _is_gradle_project(project_dir)

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        source_root = _find_source_root(project_dir)
        if source_root is None:
            logger.warning(
                "No source root found (expected src/main/java/) — "
                "skipping package hierarchy."
            )
            return

        java_files = sorted(source_root.rglob("*.java"))
        if not java_files:
            logger.warning("No .java files found under %s", source_root)
            return

        # Discover packages and collect imports per package.
        all_packages: set[str] = set()
        # package -> set of imported packages (internal only)
        package_imports: dict[str, set[str]] = defaultdict(set)

        for java_file in java_files:
            pkg = _extract_package(java_file)
            if pkg is None:
                # Derive from directory structure
                rel = java_file.relative_to(source_root).parent
                if rel != Path("."):
                    pkg = str(rel).replace("/", ".").replace("\\", ".")
                else:
                    pkg = "(default)"

            all_packages.add(pkg)

            # Parse imports
            imports = _extract_imports(java_file)
            for imp in imports:
                # Convert class-level import to package: com.example.Foo -> com.example
                imp_pkg = imp.rsplit(".", 1)[0] if "." in imp else imp
                package_imports[pkg].add(imp_pkg)

        if not all_packages:
            return

        # Filter imports to keep only internal ones.
        for pkg, imports in package_imports.items():
            package_imports[pkg] = {
                imp for imp in imports if imp in all_packages and imp != pkg
            }

        # Build edges list (like jdeps output).
        edges: list[tuple[str, str]] = []
        for src_pkg, targets in package_imports.items():
            for tgt_pkg in targets:
                edges.append((src_pkg, tgt_pkg))

        _build_hierarchical_data(edges, all_packages, graph)

        logger.debug(
            "Source hierarchy: %d packages, %d import edges",
            len(all_packages),
            len(edges),
        )


def _extract_package(java_file: Path) -> str | None:
    """Extract package name from a Java source file."""
    try:
        with open(java_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _PACKAGE_RE.match(line)
                if m:
                    return m.group(1)
                # Stop after finding a non-comment, non-blank, non-package line
                stripped = line.strip()
                if (
                    stripped
                    and not stripped.startswith("//")
                    and not stripped.startswith("/*")
                    and not stripped.startswith("*")
                ):
                    break
    except OSError:
        pass
    return None


def _extract_imports(java_file: Path) -> list[str]:
    """Extract import statements from a Java source file."""
    imports: list[str] = []
    try:
        with open(java_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _IMPORT_RE.match(line)
                if m:
                    imp = m.group(1)
                    # Skip external/JDK imports
                    if not any(imp.startswith(prefix) for prefix in _EXTERNAL_PREFIXES):
                        imports.append(imp)
                    continue
                # Stop scanning after we pass the import section
                stripped = line.strip()
                if (
                    stripped
                    and not stripped.startswith("//")
                    and not stripped.startswith("/*")
                    and not stripped.startswith("*")
                    and not stripped.startswith("package")
                    and not stripped.startswith("import")
                    and stripped not in ("", "}")
                ):
                    break
    except OSError:
        pass
    return imports


def _build_hierarchical_data(
    edges: list[tuple[str, str]],
    all_packages: set[str],
    graph: ProjectGraph,
) -> None:
    """Build hierarchy inside *graph* from source-derived package data.

    This follows the same pattern as the Maven package_hierarchy.py extractor.
    """
    if not all_packages:
        return

    # Compute root_node_id as longest common package prefix.
    parts_list = [p.split(".") for p in sorted(all_packages)]
    common: list[str] = []
    for segments in zip(*parts_list):
        if len(set(segments)) == 1:
            common.append(segments[0])
        else:
            break

    root_id = ".".join(common) if common else sorted(all_packages)[0]
    graph.root_node_id = root_id

    # Build raw hierarchy data.
    hierarchy: dict[str, dict[str, set]] = defaultdict(
        lambda: {"children": set(), "imports_from": set(), "imports_to": set()}
    )
    hierarchy[root_id] = {
        "children": set(),
        "imports_from": set(),
        "imports_to": set(),
    }

    # Register all packages and their parent→child relationships.
    for pkg in all_packages:
        parts = pkg.split(".")
        for i in range(len(common), len(parts)):
            parent = ".".join(parts[:i]) if i > 0 else root_id
            child = ".".join(parts[: i + 1])
            if parent != child:
                hierarchy[parent]["children"].add(child)
            hierarchy[child]  # ensure exists

    # Record inter-package import edges.
    for src, tgt in edges:
        if src != tgt:
            hierarchy[src]["imports_to"].add(tgt)
            hierarchy[tgt]["imports_from"].add(src)

    # Aggregate imports bottom-up: process leaves first, then parents.
    order: list[str] = []
    stack = [root_id]
    visited: set[str] = set()
    while stack:
        node_id = stack[-1]
        children = hierarchy[node_id]["children"]
        unvisited = [c for c in children if c not in visited]
        if unvisited:
            stack.extend(unvisited)
        else:
            stack.pop()
            if node_id not in visited:
                visited.add(node_id)
                order.append(node_id)

    for node_id in order:
        node = hierarchy[node_id]
        if not node["children"]:
            node["imports_to"] = {
                imp for imp in node["imports_to"] if not imp.startswith(node_id)
            }
        else:
            all_to: set[str] = set(node["imports_to"])
            all_from: set[str] = set(node["imports_from"])
            for child_id in node["children"]:
                all_to.update(hierarchy[child_id]["imports_to"])
                all_from.update(hierarchy[child_id]["imports_from"])
            node["imports_to"] = {imp for imp in all_to if not imp.startswith(node_id)}
            node["imports_from"] = all_from

    # Write into graph.hierarchy (preserving any existing symbols data).
    for node_id, raw in hierarchy.items():
        existing = graph.hierarchy.get(node_id)
        graph.hierarchy[node_id] = NodeData(
            children=sorted(raw["children"]),
            imports_to=sorted(raw["imports_to"]),
            imports_from=sorted(raw["imports_from"]),
            symbols=existing.symbols if existing else None,
        )

    logger.debug("hierarchy: root=%r, %d nodes", root_id, len(hierarchy))
