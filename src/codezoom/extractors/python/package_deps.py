"""Extract external package dependencies from pyproject.toml + uv.lock."""

from __future__ import annotations

import logging
from pathlib import Path

from codezoom.model import ExternalDep, ProjectGraph

logger = logging.getLogger(__name__)


class PackageDepsExtractor:
    """Populate external_deps and external_deps_graph from project metadata."""

    def can_handle(self, project_dir: Path) -> bool:
        return (project_dir / "pyproject.toml").exists()

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        direct_deps, dep_graph = _extract_python_dependencies(project_dir)

        # Collect all deps (direct + transitive)
        all_deps: set[str] = set(direct_deps)
        visited: set[str] = set()

        def collect_transitive(pkg_name: str) -> None:
            if pkg_name in visited:
                return
            visited.add(pkg_name)
            if pkg_name in dep_graph:
                for dep in dep_graph[pkg_name]:
                    all_deps.add(dep)
                    collect_transitive(dep)

        for dep in direct_deps:
            collect_transitive(dep)

        direct_set = set(direct_deps)
        graph.external_deps = [
            ExternalDep(name=d, is_direct=(d in direct_set)) for d in sorted(all_deps)
        ]
        graph.external_deps_graph = dep_graph


def _extract_python_dependencies(
    project_root: Path,
) -> tuple[list[str], dict[str, list[str]]]:
    """Return (direct_deps, dependency_graph) from pyproject.toml + uv.lock."""
    import tomllib

    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        return [], {}

    # --- direct deps from pyproject.toml ---
    direct_deps: list[str] = []
    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)

        for dep in data.get("project", {}).get("dependencies", []):
            pkg_name = (
                dep.split("[")[0]
                .split(">")[0]
                .split("<")[0]
                .split("=")[0]
                .split(";")[0]
                .strip()
            )
            if pkg_name:
                direct_deps.append(pkg_name.lower())
    except Exception as e:
        logger.warning("Could not parse pyproject.toml: %s", e)

    # --- transitive deps from uv.lock ---
    dep_graph: dict[str, list[str]] = {}
    uv_lock_path = project_root / "uv.lock"
    if uv_lock_path.exists():
        try:
            with open(uv_lock_path, "rb") as f:
                lock_data = tomllib.load(f)

            packages = lock_data.get("package", [])
            if isinstance(packages, list):
                for pkg_info in packages:
                    pkg_name = pkg_info.get("name", "").lower()
                    if not pkg_name:
                        continue
                    pkg_deps: list[str] = []
                    dependencies = pkg_info.get("dependencies", [])
                    if isinstance(dependencies, list):
                        for dep in dependencies:
                            if isinstance(dep, dict):
                                dep_name = dep.get("name", "").lower()
                                if dep_name and dep_name not in pkg_deps:
                                    pkg_deps.append(dep_name)
                    if pkg_deps:
                        dep_graph[pkg_name] = pkg_deps
        except Exception as e:
            logger.warning("Could not parse uv.lock: %s", e)

    return sorted(set(direct_deps)), dep_graph
