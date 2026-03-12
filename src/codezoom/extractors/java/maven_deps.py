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


def _dep_key(d) -> str:
    return f"{d.groupId}:{d.artifactId}"


def _build_dep_graph(
    deps,
    context,
    known_names: set[str],
    exclude_keys: set[str] | None = None,
) -> dict[str, list[str]]:
    """Build an adjacency list by reading each dep's own POM for declared direct deps.

    Using the resolved tree would miss edges to deps that were already resolved at a
    shallower depth (nearest-wins pruning), so we look up each dep's POM directly.
    """
    from jgo.maven import Model

    dep_graph: dict[str, list[str]] = {}
    for dep in deps:
        parent_key = _dep_key(dep)
        if exclude_keys and parent_key in exclude_keys:
            continue
        try:
            dep_model = Model(dep.artifact.component.pom(), context)
            declared, _ = dep_model.dependencies(max_depth=0)
        except (OSError, ValueError, KeyError, RuntimeError) as e:
            logger.debug("Could not load POM for %s: %s", parent_key, e)
            continue
        children = [
            _dep_key(d)
            for d in declared
            if d.scope in (None, "compile", "runtime")
            and _dep_key(d) in known_names
            and (exclude_keys is None or _dep_key(d) not in exclude_keys)
        ]
        if children:
            dep_graph[parent_key] = sorted(children)
    return dep_graph


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

    # Get direct dependencies (depth=0 on the actual POM) for marking is_direct
    try:
        direct_deps, _ = model.dependencies(max_depth=0)
    except (OSError, ValueError, KeyError) as e:
        logger.warning("Could not resolve direct deps: %s", e)
        direct_deps = []

    direct_keys = {
        _dep_key(d) for d in direct_deps if d.scope in (None, "compile", "runtime")
    }

    # Resolve full transitive closure
    try:
        all_deps, _ = model.dependencies()
    except (OSError, ValueError, KeyError) as e:
        logger.warning("Could not resolve transitive deps: %s", e)
        # Fall back to direct-only
        graph.external_deps = [
            ExternalDep(name=k, is_direct=True) for k in sorted(direct_keys)
        ]
        return

    # Filter to compile/runtime scope
    filtered = [d for d in all_deps if d.scope in (None, "compile", "runtime")]
    all_names = {_dep_key(d) for d in filtered}

    dep_graph = _build_dep_graph(filtered, context, all_names)

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

    all_deps_by_key: dict[str, object] = {}  # key -> Dependency (last-seen wins)
    all_direct_keys: set[str] = set()
    per_module_direct: dict[str, list[str]] = {}
    last_context = None

    for module in modules:
        pom_path = project_dir / module / "pom.xml"
        if not pom_path.exists():
            continue

        try:
            pom = POM(pom_path)
            context = MavenContext()
            last_context = context
            model = Model(pom, context)
        except (OSError, ValueError, KeyError) as e:
            logger.debug("Could not parse %s/pom.xml: %s", module, e)
            continue

        # Get direct dependencies
        try:
            direct_deps, _ = model.dependencies(max_depth=0)
        except (OSError, ValueError, KeyError) as e:
            logger.debug("Could not resolve direct deps for %s: %s", module, e)
            direct_deps = []

        module_direct: list[str] = []
        for d in direct_deps:
            if d.scope in (None, "compile", "runtime"):
                key = _dep_key(d)
                if key not in internal_coords:
                    all_direct_keys.add(key)
                    module_direct.append(key)
        if module_direct:
            per_module_direct[module] = sorted(module_direct)

        # Resolve full transitive closure
        try:
            deps, _ = model.dependencies()
        except (OSError, ValueError, KeyError) as e:
            logger.debug("Could not resolve transitive deps for %s: %s", module, e)
            continue

        # Collect external deps (preserve objects for graph building)
        for d in deps:
            if d.scope in (None, "compile", "runtime"):
                key = _dep_key(d)
                if key not in internal_coords:
                    all_deps_by_key[key] = d

    all_names = set(all_deps_by_key.keys())
    dep_graph: dict[str, list[str]] = {}
    if last_context is not None:
        dep_graph = _build_dep_graph(
            all_deps_by_key.values(), last_context, all_names, internal_coords
        )

    graph.external_deps = [
        ExternalDep(name=n, is_direct=(n in all_direct_keys)) for n in sorted(all_names)
    ]
    graph.external_deps_graph = dep_graph
    graph.module_direct_deps = per_module_direct
    logger.debug(
        "Maven multi-module deps: %d total (%d direct), filtered %d internal coords",
        len(all_names),
        len(all_direct_keys),
        len(internal_coords),
    )
