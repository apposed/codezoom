"""Extract external package dependencies from pom.xml via jgo."""

from __future__ import annotations

import logging
from pathlib import Path

from codezoom.model import ExternalDep, ProjectGraph

logger = logging.getLogger(__name__)


class JavaMavenDepsExtractor:
    """Populate external_deps and external_deps_graph from Maven POM."""

    def can_handle(self, project_dir: Path) -> bool:
        return (project_dir / "pom.xml").exists()

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        try:
            from jgo.maven.core import MavenContext
            from jgo.maven.model import Model
            from jgo.maven.pom import POM
        except ImportError:
            logger.warning(
                "jgo not installed — skipping Maven dependency extraction. "
                "Install with: pip install codezoom[java]"
            )
            return

        pom_path = project_dir / "pom.xml"
        if not pom_path.exists():
            return

        try:
            pom = POM(pom_path)
            context = MavenContext()
            model = Model(pom, context)
        except Exception as e:
            logger.warning("Could not parse pom.xml: %s", e)
            return

        # Get direct dependencies (depth=1) for marking is_direct
        try:
            direct_deps, _ = model.dependencies(max_depth=1)
        except Exception as e:
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
        except Exception as e:
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

        def _walk_tree(node):
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
