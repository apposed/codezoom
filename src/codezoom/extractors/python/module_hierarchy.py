"""Extract module hierarchy via pydeps or simple file-walk fallback."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from codezoom.model import NodeData, ProjectGraph


class ModuleHierarchyExtractor:
    """Populate hierarchy with package/module tree and inter-module imports."""

    def __init__(
        self,
        *,
        exclude: list[str] | None = None,
    ):
        self._exclude = exclude

    def can_handle(self, project_dir: Path) -> bool:
        return (project_dir / "pyproject.toml").exists()

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        src_dir = _find_source_dir(project_dir, graph.root_node_id)
        if src_dir is None:
            return

        deps = _run_pydeps(project_dir, src_dir, graph.root_node_id, self._exclude)
        if deps is None:
            # Fallback: build hierarchy from file tree only (no import edges)
            deps = _build_deps_from_files(src_dir, graph.root_node_id)

        _build_hierarchical_data(deps, graph)


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
    """Run pydeps and return the JSON dict, or None on failure."""
    pydeps_path = shutil.which("pydeps")
    if not pydeps_path:
        print(
            "Warning: pydeps not found (install with `pip install pydeps`). "
            "Falling back to file-based hierarchy.",
            file=sys.stderr,
        )
        return None

    cmd: list[str] = [
        pydeps_path,
        str(src_dir),
        "--show-deps",
        "--no-show",
        "--no-output",
    ]
    if exclude:
        cmd.extend(["-xx"] + exclude)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(project_dir),
    )
    if result.returncode != 0:
        print(f"Warning: pydeps failed: {result.stderr}", file=sys.stderr)
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"Warning: pydeps JSON parse error: {e}", file=sys.stderr)
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


def _build_hierarchical_data(deps: dict, graph: ProjectGraph) -> None:
    """Build the hierarchy inside *graph* from pydeps output."""
    root_id = graph.root_node_id

    hierarchy: dict[str, dict[str, set]] = defaultdict(
        lambda: {"children": set(), "imports_from": set(), "imports_to": set()}
    )
    hierarchy[root_id] = {"children": set(), "imports_from": set(), "imports_to": set()}

    for module_name, info in deps.items():
        if module_name == "__main__":
            continue

        parts = module_name.split(".")

        # Build parent→child edges
        current = root_id
        for i, _part in enumerate(parts[1:], 1):
            child_name = ".".join(parts[: i + 1])
            hierarchy[current]["children"].add(child_name)
            hierarchy[child_name]  # ensure exists
            current = child_name

        # Track module-level imports
        for imported in info.get("imports", []):
            if imported != "__main__":
                hierarchy[module_name]["imports_to"].add(imported)
                hierarchy[imported]["imports_from"].add(module_name)

    # Aggregate imports for package nodes
    def aggregate_imports(node_id: str) -> tuple[set[str], set[str]]:
        node = hierarchy[node_id]
        if not node["children"]:
            node["imports_to"] = {
                imp for imp in node["imports_to"] if not imp.startswith(node_id)
            }
            return node["imports_to"], node["imports_from"]

        all_to = set(node["imports_to"])
        all_from = set(node["imports_from"])
        for child_id in node["children"]:
            child_to, child_from = aggregate_imports(child_id)
            all_to.update(child_to)
            all_from.update(child_from)

        all_to = {imp for imp in all_to if not imp.startswith(node_id)}
        hierarchy[node_id]["imports_to"] = all_to
        hierarchy[node_id]["imports_from"] = all_from
        return all_to, all_from

    aggregate_imports(root_id)

    # Write into graph.hierarchy (preserving any existing symbols data)
    for node_id, raw in hierarchy.items():
        existing = graph.hierarchy.get(node_id)
        graph.hierarchy[node_id] = NodeData(
            children=sorted(raw["children"]),
            imports_to=sorted(raw["imports_to"]),
            imports_from=sorted(raw["imports_from"]),
            symbols=existing.symbols if existing else None,
        )
