"""Extract external package dependencies from pom.xml via jgo."""

from __future__ import annotations

import logging
from pathlib import Path

from codezoom.model import ExternalDep, ProjectGraph

logger = logging.getLogger(__name__)


def _get_group_artifact(pom_path: Path) -> str | None:
    """Extract groupId:artifactId from a pom.xml using jgo.

    Uses jgo's POM class which handles parent inheritance for groupId.
    """
    try:
        from jgo.maven import POM
    except ImportError:
        return None

    try:
        pom = POM(pom_path)
        group_id = pom.groupId
        artifact_id = pom.artifactId
        if group_id and artifact_id:
            return f"{group_id}:{artifact_id}"
    except (OSError, ValueError, KeyError, AttributeError) as e:
        logger.debug("Could not extract coords from %s: %s", pom_path, e)
    return None


class JavaMavenDepsExtractor:
    """Populate external_deps and external_deps_graph from Maven POM."""

    def can_handle(self, project_dir: Path) -> bool:
        return (project_dir / "pom.xml").exists()

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        try:
            from jgo.maven import POM, MavenContext, Model  # noqa: F401
        except ImportError:
            logger.warning(
                "jgo not installed — skipping Maven dependency extraction. "
                "Install with: pip install codezoom[java]"
            )
            return

        from codezoom.extractors.java import _discover_maven_modules

        modules = _discover_maven_modules(project_dir)
        if modules:
            _extract_multi_module(project_dir, modules, graph)
        else:
            _extract_single_module(project_dir, graph)


def _extract_single_module(project_dir: Path, graph: ProjectGraph) -> None:
    """Extract deps from a single-module Maven project."""
    from jgo.maven import POM, MavenContext, Model

    pom_path = project_dir / "pom.xml"
    if not pom_path.exists():
        return

    try:
        pom = POM(pom_path)
        context = MavenContext()
        model = Model(pom, context)
    except (OSError, ValueError, KeyError) as e:
        logger.warning("Could not parse pom.xml: %s", e)
        return

    # Get direct dependencies (depth=1) for marking is_direct
    try:
        direct_deps, _ = model.dependencies(max_depth=1)
    except (OSError, ValueError, KeyError) as e:
        logger.warning("Could not resolve direct deps: %s", e)
        direct_deps = []

    direct_keys = {
        f"{d.groupId}:{d.artifactId}"
        for d in direct_deps
        if d.scope in (None, "compile", "runtime")
    }

    # Resolve full transitive tree
    try:
        all_deps, tree = model.dependencies()
    except (OSError, ValueError, KeyError) as e:
        logger.warning("Could not resolve transitive deps: %s", e)
        # Fall back to direct-only
        graph.external_deps = [
            ExternalDep(name=k, is_direct=True) for k in sorted(direct_keys)
        ]
        return

    # Filter to compile/runtime scope
    filtered = [d for d in all_deps if d.scope in (None, "compile", "runtime")]

    # Build adjacency list from the DependencyNode tree
    dep_graph: dict[str, list[str]] = {}

    visited: set[int] = set()

    def _walk_tree(node):
        node_id = id(node)
        if node_id in visited:
            return
        visited.add(node_id)
        for child in node.children:
            parent_key = f"{child.dep.groupId}:{child.dep.artifactId}"
            if child.dep.scope not in (None, "compile", "runtime"):
                continue
            child_deps = []
            for grandchild in child.children:
                if grandchild.dep.scope in (None, "compile", "runtime"):
                    gc_key = f"{grandchild.dep.groupId}:{grandchild.dep.artifactId}"
                    child_deps.append(gc_key)
            if child_deps:
                dep_graph[parent_key] = child_deps
            _walk_tree(child)

    _walk_tree(tree)

    # Build dep list
    all_names: set[str] = set()
    for d in filtered:
        all_names.add(f"{d.groupId}:{d.artifactId}")

    graph.external_deps = [
        ExternalDep(name=n, is_direct=(n in direct_keys)) for n in sorted(all_names)
    ]
    graph.external_deps_graph = dep_graph
    logger.debug(
        "Maven deps: %d total (%d direct)",
        len(all_names),
        len(direct_keys),
    )


def _extract_multi_module(
    project_dir: Path, modules: list[str], graph: ProjectGraph
) -> None:
    """Extract and merge deps across all modules of a multi-module Maven project."""
    from jgo.maven import POM, MavenContext, Model

    # Build set of internal module coordinates to filter out
    internal_coords: set[str] = set()
    for module in modules:
        pom_path = project_dir / module / "pom.xml"
        coord = _get_group_artifact(pom_path)
        if coord:
            internal_coords.add(coord)
    # Also add the root pom itself
    root_coord = _get_group_artifact(project_dir / "pom.xml")
    if root_coord:
        internal_coords.add(root_coord)

    logger.debug(
        "Multi-module: %d modules, %d internal coords",
        len(modules),
        len(internal_coords),
    )

    all_names: set[str] = set()
    all_direct_keys: set[str] = set()
    merged_dep_graph: dict[str, set[str]] = {}
    per_module_direct: dict[str, list[str]] = {}

    for module in modules:
        pom_path = project_dir / module / "pom.xml"
        if not pom_path.exists():
            continue

        try:
            pom = POM(pom_path)
            context = MavenContext()
            model = Model(pom, context)
        except (OSError, ValueError, KeyError) as e:
            logger.debug("Could not parse %s/pom.xml: %s", module, e)
            continue

        # Get direct dependencies
        try:
            direct_deps, _ = model.dependencies(max_depth=1)
        except (OSError, ValueError, KeyError) as e:
            logger.debug("Could not resolve direct deps for %s: %s", module, e)
            direct_deps = []

        module_direct: list[str] = []
        for d in direct_deps:
            if d.scope in (None, "compile", "runtime"):
                key = f"{d.groupId}:{d.artifactId}"
                if key not in internal_coords:
                    all_direct_keys.add(key)
                    module_direct.append(key)
        if module_direct:
            per_module_direct[module] = sorted(module_direct)

        # Resolve full transitive tree
        try:
            deps, tree = model.dependencies()
        except (OSError, ValueError, KeyError) as e:
            logger.debug("Could not resolve transitive deps for %s: %s", module, e)
            continue

        # Collect external dep names
        for d in deps:
            if d.scope in (None, "compile", "runtime"):
                key = f"{d.groupId}:{d.artifactId}"
                if key not in internal_coords:
                    all_names.add(key)

        # Build adjacency from tree, filtering internal coords
        visited: set[int] = set()

        def _walk_tree(node):
            node_id = id(node)
            if node_id in visited:
                return
            visited.add(node_id)
            for child in node.children:
                parent_key = f"{child.dep.groupId}:{child.dep.artifactId}"
                if child.dep.scope not in (None, "compile", "runtime"):
                    continue
                if parent_key in internal_coords:
                    # Still walk children — they may have external transitive deps
                    _walk_tree(child)
                    continue
                child_deps: list[str] = []
                for grandchild in child.children:
                    if grandchild.dep.scope in (None, "compile", "runtime"):
                        gc_key = f"{grandchild.dep.groupId}:{grandchild.dep.artifactId}"
                        if gc_key not in internal_coords:
                            child_deps.append(gc_key)
                if child_deps:
                    if parent_key not in merged_dep_graph:
                        merged_dep_graph[parent_key] = set()
                    merged_dep_graph[parent_key].update(child_deps)
                _walk_tree(child)

        _walk_tree(tree)

    graph.external_deps = [
        ExternalDep(name=n, is_direct=(n in all_direct_keys)) for n in sorted(all_names)
    ]
    graph.external_deps_graph = {k: sorted(v) for k, v in merged_dep_graph.items()}
    graph.module_direct_deps = per_module_direct
    logger.debug(
        "Maven multi-module deps: %d total (%d direct), filtered %d internal coords",
        len(all_names),
        len(all_direct_keys),
        len(internal_coords),
    )
