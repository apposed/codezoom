"""Extract Rust module hierarchy from rustdoc JSON output."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections import defaultdict
from pathlib import Path

from codezoom.extractors.rust._rustdoc import get_rustdoc_json
from codezoom.model import NodeData, ProjectGraph

logger = logging.getLogger(__name__)


class RustModuleHierarchyExtractor:
    """Populate hierarchy with Rust module tree from rustdoc JSON."""

    def can_handle(self, project_dir: Path) -> bool:
        return (project_dir / "Cargo.toml").exists()

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        crate_info = _discover_workspace_crates(project_dir)
        if not crate_info:
            logger.warning("No workspace crates found in %s", project_dir)
            return

        crate_names = [name for name, _, _ in crate_info]

        # Each crate becomes a root
        graph.root_node_ids = list(crate_names)

        for crate_name, target_name, src_path in crate_info:
            doc = get_rustdoc_json(project_dir, crate_name, target_name)
            if doc is None:
                # Create an empty node for crates without rustdoc
                graph.hierarchy[crate_name] = NodeData()
                continue

            _build_crate_hierarchy(doc, crate_name, graph)

        # Add import edges from source-level `use crate::` statements
        for crate_name, _, src_path in crate_info:
            if src_path:
                _add_source_import_edges(crate_name, src_path, graph)

        # Compute cross-crate import edges at the crate level
        if len(crate_names) > 1:
            _compute_crate_level_imports(crate_names, graph)

        total_nodes = sum(1 for k in graph.hierarchy if k not in set(crate_names))
        logger.debug(
            "Rust hierarchy: %d crates, %d module nodes",
            len(crate_names),
            total_nodes,
        )


def _discover_workspace_crates(
    project_dir: Path,
) -> list[tuple[str, str, Path | None]]:
    """Discover workspace crate info via cargo metadata.

    Returns a sorted list of ``(package_name, lib_target_name, src_dir)``
    for each workspace member that has a library target. *src_dir* is the
    directory containing ``lib.rs`` (derived from the target's ``src_path``).
    """
    try:
        result = subprocess.run(
            ["cargo", "metadata", "--format-version", "1", "--no-deps"],
            capture_output=True,
            text=True,
            cwd=str(project_dir),
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("Could not run cargo metadata: %s", e)
        return []

    if result.returncode != 0:
        return []

    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    workspace_members = set(metadata.get("workspace_members", []))
    packages = metadata.get("packages", [])

    _LIB_KINDS = {"lib", "rlib", "cdylib", "staticlib", "dylib", "proc-macro"}

    crate_info: list[tuple[str, str, Path | None]] = []
    for pkg in packages:
        if pkg["id"] not in workspace_members:
            continue
        for target in pkg.get("targets", []):
            if _LIB_KINDS & set(target.get("kind", [])):
                src_path = target.get("src_path")
                src_dir = Path(src_path).parent if src_path else None
                crate_info.append((pkg["name"], target["name"], src_dir))
                break  # Take the first lib target

    return sorted(crate_info)


def _build_crate_hierarchy(doc: dict, crate_name: str, graph: ProjectGraph) -> None:
    """Build module hierarchy for a single crate from its rustdoc JSON."""
    index = doc.get("index", {})
    root_id = str(doc.get("root", ""))
    root_item = index.get(root_id)
    if root_item is None:
        graph.hierarchy[crate_name] = NodeData()
        return

    # Build hierarchy via recursive module walk
    hierarchy: dict[str, dict[str, set]] = defaultdict(
        lambda: {"children": set(), "imports_from": set(), "imports_to": set()}
    )

    _walk_module(index, root_item, crate_name, hierarchy)

    # Process use/re-export edges
    _collect_use_edges(index, root_item, crate_name, hierarchy)

    # Aggregate imports bottom-up
    _aggregate_imports(crate_name, hierarchy)

    # Write into graph.hierarchy
    crate_children: list[str] = []
    for node_id, raw in hierarchy.items():
        if node_id == crate_name:
            crate_children = sorted(raw["children"])
            continue
        existing = graph.hierarchy.get(node_id)
        graph.hierarchy[node_id] = NodeData(
            children=sorted(raw["children"]),
            imports_to=sorted(raw["imports_to"]),
            imports_from=sorted(raw["imports_from"]),
            symbols=existing.symbols if existing else None,
            is_exported=True,  # Will be refined by _walk_module
        )

    # Set visibility on nodes
    _set_visibility(index, root_item, crate_name, graph)

    # Create the crate root node
    graph.hierarchy[crate_name] = NodeData(
        children=crate_children,
    )


def _walk_module(
    index: dict,
    module_item: dict,
    module_path: str,
    hierarchy: dict[str, dict[str, set]],
) -> None:
    """Recursively walk modules, building parent→child edges."""
    inner = module_item.get("inner", {})
    if not isinstance(inner, dict) or "module" not in inner:
        return

    mod_data = inner["module"]
    items = mod_data.get("items", [])

    for item_id in items:
        item = index.get(str(item_id))
        if item is None:
            continue

        item_inner = item.get("inner", {})
        if not isinstance(item_inner, dict):
            continue

        if "module" in item_inner:
            child_name = item.get("name", "")
            if not child_name:
                continue
            child_path = f"{module_path}.{child_name}"
            hierarchy[module_path]["children"].add(child_path)
            hierarchy[child_path]  # ensure exists
            _walk_module(index, item, child_path, hierarchy)


def _collect_use_edges(
    index: dict,
    module_item: dict,
    module_path: str,
    hierarchy: dict[str, dict[str, set]],
) -> None:
    """Collect use/re-export edges from module items."""
    inner = module_item.get("inner", {})
    if not isinstance(inner, dict) or "module" not in inner:
        return

    mod_data = inner["module"]
    items = mod_data.get("items", [])

    for item_id in items:
        item = index.get(str(item_id))
        if item is None:
            continue

        item_inner = item.get("inner", {})
        if not isinstance(item_inner, dict):
            continue

        if "use" in item_inner:
            use_data = item_inner["use"]
            source = use_data.get("source", "")
            # Resolve the source to a module path
            # The source is like "submod::item" — we want the module part
            if "::" in source:
                source_mod = source.rsplit("::", 1)[0]
                # Convert to dot notation and prefix with crate
                crate_root = module_path.split(".")[0]
                source_mod_path = f"{crate_root}.{source_mod.replace('::', '.')}"
                if source_mod_path in hierarchy and source_mod_path != module_path:
                    hierarchy[module_path]["imports_from"].add(source_mod_path)
                    hierarchy[source_mod_path]["imports_to"].add(module_path)

        elif "module" in item_inner:
            child_name = item.get("name", "")
            if child_name:
                child_path = f"{module_path}.{child_name}"
                _collect_use_edges(index, item, child_path, hierarchy)


def _set_visibility(
    index: dict,
    module_item: dict,
    module_path: str,
    graph: ProjectGraph,
) -> None:
    """Set is_exported on module nodes based on rustdoc visibility."""
    inner = module_item.get("inner", {})
    if not isinstance(inner, dict) or "module" not in inner:
        return

    for item_id in inner["module"].get("items", []):
        item = index.get(str(item_id))
        if item is None:
            continue

        item_inner = item.get("inner", {})
        if not isinstance(item_inner, dict) or "module" not in item_inner:
            continue

        child_name = item.get("name", "")
        if not child_name:
            continue
        child_path = f"{module_path}.{child_name}"

        node = graph.hierarchy.get(child_path)
        if node is not None:
            vis = item.get("visibility", "default")
            node.is_exported = vis == "public"

        _set_visibility(index, item, child_path, graph)


def _add_source_import_edges(
    crate_name: str, src_dir: Path, graph: ProjectGraph
) -> None:
    """Parse ``use crate::...`` statements from source files to build import edges.

    This fills in the ``imports_to`` / ``imports_from`` edges that rustdoc
    JSON doesn't provide (it only exposes ``pub use`` re-exports, not
    internal ``use`` statements).
    """
    if not src_dir.is_dir():
        return

    # Build set of known module paths for this crate
    crate_modules: set[str] = set()
    for node_id in graph.hierarchy:
        if node_id == crate_name or node_id.startswith(f"{crate_name}."):
            crate_modules.add(node_id)

    # Map source files to their module paths
    file_to_module: dict[Path, str] = {}
    for rs_file in src_dir.rglob("*.rs"):
        mod_path = _source_file_to_module_path(rs_file, src_dir, crate_name)
        if mod_path and mod_path in crate_modules:
            file_to_module[rs_file] = mod_path

    # Pattern for `use crate::path::to::module` and `use crate::path::{...}`
    use_crate_re = re.compile(r"^use\s+crate::(\S+)")

    edge_count = 0
    for rs_file, source_module in file_to_module.items():
        try:
            content = rs_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for line in content.splitlines():
            line = line.strip()
            m = use_crate_re.match(line)
            if not m:
                continue

            use_path = m.group(1).rstrip(";")

            # Resolve target module: walk from longest to shortest prefix
            # e.g. `use crate::spatial::kd_tree::KDTree` should resolve to
            # `crate_name.spatial.kd_tree`
            parts = use_path.replace("::", ".").split(".")
            # Strip curly-brace group imports: `a::b::{c, d}` → parts up to `{`
            clean_parts = []
            for p in parts:
                if p.startswith("{"):
                    break
                clean_parts.append(p)
            parts = clean_parts

            # Try progressively shorter prefixes to find a known module
            target_module = None
            for length in range(len(parts), 0, -1):
                candidate = f"{crate_name}.{'.'.join(parts[:length])}"
                if candidate in crate_modules:
                    target_module = candidate
                    break

            if target_module and target_module != source_module:
                # Don't add edges to self or to ancestor/descendant
                if not source_module.startswith(
                    f"{target_module}."
                ) and not target_module.startswith(f"{source_module}."):
                    node = graph.hierarchy.get(source_module)
                    tgt_node = graph.hierarchy.get(target_module)
                    if node and tgt_node:
                        if target_module not in node.imports_to:
                            node.imports_to = sorted(
                                set(node.imports_to) | {target_module}
                            )
                        if source_module not in tgt_node.imports_from:
                            tgt_node.imports_from = sorted(
                                set(tgt_node.imports_from) | {source_module}
                            )
                        edge_count += 1

    if edge_count:
        # Re-aggregate imports bottom-up for parent nodes
        _reaggregate_imports(crate_name, graph, crate_modules)

    logger.debug(
        "Rust source imports for '%s': %d edges from %d files",
        crate_name,
        edge_count,
        len(file_to_module),
    )


def _source_file_to_module_path(
    rs_file: Path, src_dir: Path, crate_name: str
) -> str | None:
    """Convert a .rs file path to its dot-separated module path.

    ``src/spatial/kd_tree.rs`` → ``crate_name.spatial.kd_tree``
    ``src/spatial/mod.rs``     → ``crate_name.spatial``
    ``src/lib.rs``             → ``crate_name``
    """
    try:
        rel = rs_file.relative_to(src_dir)
    except ValueError:
        return None

    parts = list(rel.with_suffix("").parts)
    if not parts:
        return None

    # lib.rs → crate root
    if parts == ["lib"]:
        return crate_name

    # mod.rs → parent directory is the module
    if parts[-1] == "mod":
        parts = parts[:-1]

    if not parts:
        return crate_name

    return f"{crate_name}.{'.'.join(parts)}"


def _reaggregate_imports(
    crate_name: str, graph: ProjectGraph, crate_modules: set[str]
) -> None:
    """Re-aggregate imports bottom-up after adding source-level edges."""
    order: list[str] = []
    stack = [crate_name]
    visited: set[str] = set()
    while stack:
        node_id = stack[-1]
        node = graph.hierarchy.get(node_id)
        children = node.children if node else []
        unvisited = [c for c in children if c not in visited and c in crate_modules]
        if unvisited:
            stack.extend(unvisited)
        else:
            stack.pop()
            if node_id not in visited:
                visited.add(node_id)
                order.append(node_id)

    for node_id in order:
        node = graph.hierarchy.get(node_id)
        if node is None:
            continue
        if node.children:
            all_to: set[str] = set(node.imports_to)
            all_from: set[str] = set(node.imports_from)
            for child_id in node.children:
                child = graph.hierarchy.get(child_id)
                if child:
                    all_to.update(child.imports_to)
                    all_from.update(child.imports_from)
            node.imports_to = sorted(
                imp for imp in all_to if not imp.startswith(f"{node_id}.")
            )
            node.imports_from = sorted(all_from)


def _aggregate_imports(root_id: str, hierarchy: dict[str, dict[str, set]]) -> None:
    """Aggregate imports bottom-up from leaves to parents."""
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


def _compute_crate_level_imports(crate_names: list[str], graph: ProjectGraph) -> None:
    """Derive crate→crate import edges from module-level edges."""
    crate_set = set(crate_names)

    # Map module paths to their owning crate
    mod_to_crate: dict[str, str] = {}
    for crate_name in crate_names:
        for node_id in graph.hierarchy:
            if node_id == crate_name or node_id.startswith(f"{crate_name}."):
                mod_to_crate[node_id] = crate_name

    for crate_name in crate_names:
        crate_node = graph.hierarchy.get(crate_name)
        if crate_node is None:
            continue

        target_crates: set[str] = set()
        source_crates: set[str] = set()

        # Walk all modules under this crate
        for node_id, node in graph.hierarchy.items():
            if mod_to_crate.get(node_id) != crate_name:
                continue
            for imp in node.imports_to:
                tgt_crate = mod_to_crate.get(imp)
                if tgt_crate and tgt_crate != crate_name and tgt_crate in crate_set:
                    target_crates.add(tgt_crate)
            for imp in node.imports_from:
                src_crate = mod_to_crate.get(imp)
                if src_crate and src_crate != crate_name and src_crate in crate_set:
                    source_crates.add(src_crate)

        crate_node.imports_to = sorted(target_crates)
        crate_node.imports_from = sorted(source_crates)
