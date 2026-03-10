"""Extract module hierarchy via pydeps or simple file-walk fallback."""

from __future__ import annotations

import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

from codezoom.extractors.python import is_python_project
from codezoom.model import NodeData, ProjectGraph

logger = logging.getLogger(__name__)


class ModuleHierarchyExtractor:
    """Populate hierarchy with package/module tree and inter-module imports."""

    def __init__(
        self,
        *,
        exclude: list[str] | None = None,
    ):
        self._exclude = exclude

    def can_handle(self, project_dir: Path) -> bool:
        return is_python_project(project_dir)

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        for root_node_id in graph.root_node_ids:
            src_dir = _find_source_dir(project_dir, root_node_id)
            if src_dir is not None:
                self._extract_package(project_dir, graph, root_node_id, src_dir)
            else:
                # Multi-package project: root node has packages as children.
                root_node = graph.hierarchy.get(root_node_id)
                for pkg in root_node.children if root_node else []:
                    pkg_dir = _find_source_dir(project_dir, pkg)
                    if pkg_dir is not None:
                        self._extract_package(project_dir, graph, pkg, pkg_dir)

    def _extract_package(
        self,
        project_dir: Path,
        graph: ProjectGraph,
        pkg_name: str,
        src_dir: Path,
    ) -> None:
        deps = _run_pydeps(project_dir, src_dir, pkg_name, self._exclude)
        if deps is None:
            # Fallback: build hierarchy from file tree only (no import edges)
            deps = _build_deps_from_files(src_dir, pkg_name)
        _build_hierarchical_data(deps, graph, pkg_name)


def _find_source_dir(project_dir: Path, root_node_id: str) -> Path | None:
    """Locate the Python package directory inside the project."""
    # Try src-layout first
    candidate = project_dir / "src" / root_node_id
    if candidate.is_dir():
        return candidate
    # Try flat layout
    candidate = project_dir / root_node_id
    if candidate.is_dir():
        return candidate
    return None


def _run_pydeps(
    project_dir: Path,
    src_dir: Path,
    root_node_id: str,
    exclude: list[str] | None,
) -> dict | None:
    """Run pydeps programmatically and return the dep dict, or None on failure."""
    try:
        from pydeps import target as pydeps_target
        from pydeps.configs import Config
        from pydeps.py2depgraph import py2dep
    except ImportError:
        logger.warning(
            "pydeps not installed (install codezoom with the `python` extra). "
            "Falling back to file-based hierarchy."
        )
        return None

    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))

    config = Config(
        fname=str(src_dir),
        no_output=True,
        no_show=True,
        no_dot=True,
        show_deps=False,
        show_raw_deps=False,
        show_dot=False,
        exclude_exact=list(exclude) if exclude else [],
    )
    ctx = dict(iter(config))

    try:
        inp = pydeps_target.Target(str(src_dir))
        with inp.chdir_work():
            ctx["fname"] = inp.fname
            ctx["isdir"] = inp.is_dir
            dep_graph = py2dep(inp, **ctx)
            return json.loads(dep_graph.__json__())
    except Exception as e:
        logger.warning("pydeps failed: %s", e)
        return None


def _build_deps_from_files(src_dir: Path, root_node_id: str) -> dict:
    """Build a minimal dep dict from the file tree (no import edges)."""
    deps: dict[str, dict] = {}
    for py_file in src_dir.rglob("*.py"):
        if py_file.name == "__init__.py":
            continue
        relative = py_file.relative_to(src_dir.parent)
        module_name = (
            str(relative).replace("/", ".").replace("\\", ".").removesuffix(".py")
        )
        deps[module_name] = {"imports": []}
    return deps


def _build_hierarchical_data(deps: dict, graph: ProjectGraph, root_id: str) -> None:
    """Build the hierarchy inside *graph* from pydeps output."""

    hierarchy: dict[str, dict[str, set]] = defaultdict(
        lambda: {"children": set(), "imports_from": set(), "imports_to": set()}
    )
    hierarchy[root_id] = {"children": set(), "imports_from": set(), "imports_to": set()}

    for module_name, info in deps.items():
        if module_name == "__main__":
            continue
        # Only include project-internal modules (skip third-party deps).
        if module_name != root_id and not module_name.startswith(root_id + "."):
            continue

        parts = module_name.split(".")

        # Build parent→child edges
        current = root_id
        for i, _part in enumerate(parts[1:], 1):
            child_name = ".".join(parts[: i + 1])
            hierarchy[current]["children"].add(child_name)
            hierarchy[child_name]  # ensure exists
            current = child_name

        # Track module-level imports (only to other project-internal modules).
        for imported in info.get("imports", []):
            if imported == "__main__":
                continue
            if imported != root_id and not imported.startswith(root_id + "."):
                continue
            hierarchy[module_name]["imports_to"].add(imported)
            hierarchy[imported]["imports_from"].add(module_name)

    # Aggregate imports bottom-up: process leaves first, then parents.
    # Build processing order via iterative post-order traversal.
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
            all_to = set(node["imports_to"])
            all_from = set(node["imports_from"])
            for child_id in node["children"]:
                all_to.update(hierarchy[child_id]["imports_to"])
                all_from.update(hierarchy[child_id]["imports_from"])
            node["imports_to"] = {imp for imp in all_to if not imp.startswith(node_id)}
            node["imports_from"] = all_from

    # Write into graph.hierarchy (preserving any existing symbols and is_exported data)
    for node_id, raw in hierarchy.items():
        existing = graph.hierarchy.get(node_id)
        graph.hierarchy[node_id] = NodeData(
            children=sorted(raw["children"]),
            imports_to=sorted(raw["imports_to"]),
            imports_from=sorted(raw["imports_from"]),
            symbols=existing.symbols if existing else None,
            is_exported=existing.is_exported
            if existing
            else not node_id.split(".")[-1].startswith("_"),
        )
