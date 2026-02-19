"""Extract external crate dependencies from cargo metadata."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from codezoom.model import ExternalDep, ProjectGraph

logger = logging.getLogger(__name__)


class RustCargoDepsExtractor:
    """Populate external_deps and external_deps_graph from cargo metadata."""

    def can_handle(self, project_dir: Path) -> bool:
        return (project_dir / "Cargo.toml").exists()

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        metadata = _run_cargo_metadata(project_dir)
        if metadata is None:
            return

        workspace_members = set(metadata.get("workspace_members", []))
        packages = {p["id"]: p for p in metadata.get("packages", [])}
        resolve_nodes = {
            n["id"]: n for n in metadata.get("resolve", {}).get("nodes", [])
        }

        # Collect direct deps from all workspace crates (normal deps only)
        workspace_crate_names = {
            packages[m]["name"] for m in workspace_members if m in packages
        }
        direct_dep_names: set[str] = set()
        per_crate_direct: dict[str, list[str]] = {}
        for member_id in workspace_members:
            pkg = packages.get(member_id)
            if not pkg:
                continue
            crate_direct: list[str] = []
            for dep in pkg.get("dependencies", []):
                kind = dep.get("kind")
                # kind=None means normal dep; skip "dev" and "build"
                if kind is not None:
                    continue
                dep_name = dep["name"]
                # Only include external deps (not other workspace crates)
                if dep_name not in workspace_crate_names:
                    direct_dep_names.add(dep_name)
                    crate_direct.append(dep_name)
            if crate_direct:
                per_crate_direct[pkg["name"]] = sorted(set(crate_direct))

        # Build transitive dependency graph from resolve nodes
        # Map package IDs to their crate names
        id_to_name: dict[str, str] = {}
        for pkg_id, pkg in packages.items():
            id_to_name[pkg_id] = pkg["name"]

        dep_graph: dict[str, list[str]] = {}
        all_dep_names: set[str] = set()

        for node_id, node in resolve_nodes.items():
            if node_id in workspace_members:
                continue  # Skip workspace members themselves

            node_name = id_to_name.get(node_id)
            if not node_name:
                continue

            all_dep_names.add(node_name)

            # Collect this node's dependencies (excluding dev/build)
            node_deps: list[str] = []
            for dep in node.get("deps", []):
                dep_kinds = dep.get("dep_kinds", [])
                # Keep if any dep_kind is normal (kind=None)
                has_normal = (
                    any(dk.get("kind") is None for dk in dep_kinds)
                    if dep_kinds
                    else True
                )
                if not has_normal:
                    continue
                dep_name = dep.get("name")
                if dep_name:
                    node_deps.append(dep_name)

            if node_deps:
                dep_graph[node_name] = sorted(set(node_deps))

        # Also walk from direct deps to collect all transitive deps
        visited: set[str] = set()

        def collect_transitive(name: str) -> None:
            if name in visited:
                return
            visited.add(name)
            all_dep_names.add(name)
            for dep in dep_graph.get(name, []):
                collect_transitive(dep)

        for name in direct_dep_names:
            collect_transitive(name)

        # Filter to only deps reachable from workspace direct deps
        reachable = visited & all_dep_names

        graph.external_deps = sorted(
            [
                ExternalDep(name=name, is_direct=(name in direct_dep_names))
                for name in reachable
                if name not in {id_to_name.get(m) for m in workspace_members}
            ],
            key=lambda d: d.name,
        )

        # Filter dep_graph to reachable deps only
        graph.external_deps_graph = {
            k: [v for v in vs if v in reachable]
            for k, vs in dep_graph.items()
            if k in reachable
        }

        # Per-crate direct deps for module→dependency edges in the graph
        graph.module_direct_deps = per_crate_direct

        logger.debug(
            "Rust deps: %d direct, %d total, %d graph edges",
            sum(1 for d in graph.external_deps if d.is_direct),
            len(graph.external_deps),
            sum(len(v) for v in graph.external_deps_graph.values()),
        )


def _run_cargo_metadata(project_dir: Path) -> dict | None:
    """Run cargo metadata and return parsed JSON."""
    try:
        result = subprocess.run(
            ["cargo", "metadata", "--format-version", "1"],
            capture_output=True,
            text=True,
            cwd=str(project_dir),
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("Could not run cargo metadata: %s", e)
        return None

    if result.returncode != 0:
        logger.warning(
            "cargo metadata failed: %s",
            result.stderr.strip() if result.stderr else "unknown error",
        )
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        logger.warning("cargo metadata JSON parse error: %s", e)
        return None
