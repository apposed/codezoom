"""Extract Rust symbols (structs, enums, traits, functions, methods) from rustdoc JSON."""

from __future__ import annotations

import logging
from pathlib import Path

from codezoom.extractors.rust._rustdoc import get_rustdoc_json
from codezoom.model import NodeData, ProjectGraph, SymbolData

logger = logging.getLogger(__name__)


class RustAstSymbolsExtractor:
    """Populate hierarchy nodes with Rust symbol data from rustdoc JSON."""

    def can_handle(self, project_dir: Path) -> bool:
        return (project_dir / "Cargo.toml").exists()

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        for crate_name in graph.root_node_ids:
            doc = get_rustdoc_json(project_dir, crate_name)
            if doc is None:
                continue
            _extract_crate_symbols(doc, crate_name, graph)


def _extract_crate_symbols(doc: dict, crate_name: str, graph: ProjectGraph) -> None:
    """Extract symbols from a single crate's rustdoc JSON into graph."""
    index = doc.get("index", {})
    root_id = str(doc.get("root", ""))
    root_item = index.get(root_id)
    if root_item is None:
        return

    # First pass: collect symbols per module
    symbols_by_module: dict[str, dict[str, SymbolData]] = {}
    _walk_module_symbols(index, root_item, crate_name, symbols_by_module)

    # Second pass: collect impl methods and attach to their types
    paths = doc.get("paths", {})
    _attach_impl_methods(index, paths, crate_name, symbols_by_module)

    # Merge into graph
    for module_path, symbols in symbols_by_module.items():
        if not symbols:
            continue
        node = graph.hierarchy.get(module_path)
        if node is None:
            node = NodeData()
            graph.hierarchy[module_path] = node
        if node.symbols is None:
            node.symbols = {}
        node.symbols.update(symbols)

    symbol_count = sum(len(s) for s in symbols_by_module.values())
    logger.debug(
        "Rust symbols for '%s': %d modules, %d symbols",
        crate_name,
        len(symbols_by_module),
        symbol_count,
    )


def _rust_visibility(vis: str) -> str:
    """Map rustdoc visibility to codezoom visibility."""
    if vis == "public":
        return "public"
    if vis == "restricted":
        return "package"  # pub(crate), pub(super), etc.
    return "private"  # "default" = private


def _get_line(item: dict) -> int | None:
    """Extract source line number from a rustdoc item."""
    span = item.get("span")
    if span and "begin" in span:
        return span["begin"][0]
    return None


def _walk_module_symbols(
    index: dict,
    module_item: dict,
    module_path: str,
    symbols_by_module: dict[str, dict[str, SymbolData]],
) -> None:
    """Walk a module and collect its direct symbols (functions, structs, enums, traits)."""
    inner = module_item.get("inner", {})
    if not isinstance(inner, dict) or "module" not in inner:
        return

    mod_data = inner["module"]
    items = mod_data.get("items", [])
    module_symbols: dict[str, SymbolData] = {}

    for item_id in items:
        item = index.get(str(item_id))
        if item is None:
            continue

        name = item.get("name")
        if not name:
            continue

        item_inner = item.get("inner", {})
        if not isinstance(item_inner, dict):
            continue

        vis = _rust_visibility(item.get("visibility", "default"))

        if "function" in item_inner:
            module_symbols[name] = SymbolData(
                name=name,
                kind="function",
                line=_get_line(item),
                visibility=vis,
            )

        elif "struct" in item_inner:
            module_symbols[name] = SymbolData(
                name=name,
                kind="class",
                line=_get_line(item),
                visibility=vis,
            )

        elif "enum" in item_inner:
            module_symbols[name] = SymbolData(
                name=name,
                kind="class",
                line=_get_line(item),
                visibility=vis,
            )

        elif "trait" in item_inner:
            # Collect required/provided methods from trait items
            trait_methods: dict[str, SymbolData] = {}
            for trait_item_id in item_inner["trait"].get("items", []):
                trait_item = index.get(str(trait_item_id))
                if trait_item is None:
                    continue
                ti_name = trait_item.get("name")
                ti_inner = trait_item.get("inner", {})
                if ti_name and isinstance(ti_inner, dict) and "function" in ti_inner:
                    trait_methods[ti_name] = SymbolData(
                        name=ti_name,
                        kind="method",
                        line=_get_line(trait_item),
                        visibility=_rust_visibility(
                            trait_item.get("visibility", "default")
                        ),
                    )

            module_symbols[name] = SymbolData(
                name=name,
                kind="class",
                line=_get_line(item),
                children=trait_methods,
                visibility=vis,
            )

        elif "module" in item_inner:
            # Recurse into submodules
            child_path = f"{module_path}.{name}"
            _walk_module_symbols(index, item, child_path, symbols_by_module)

    if module_symbols:
        symbols_by_module[module_path] = module_symbols


def _attach_impl_methods(
    index: dict,
    paths: dict,
    crate_name: str,
    symbols_by_module: dict[str, dict[str, SymbolData]],
) -> None:
    """Find impl blocks in the index and attach methods to their type symbols.

    Impl blocks in rustdoc JSON are not nested under module items — they
    exist at the top level of the index. We use the ``paths`` dict to find
    the module path for the type each impl is for.
    """
    # Build a lookup: type_id -> (module_path, type_name) for crate-local types
    type_locations: dict[str, tuple[str, str]] = {}
    for type_id, path_info in paths.items():
        if path_info.get("crate_id") != 0:
            continue  # Skip external crate types
        path_parts = path_info.get("path", [])
        kind = path_info.get("kind", "")
        if kind not in ("struct", "enum", "trait") or len(path_parts) < 2:
            continue
        type_name = path_parts[-1]
        module_path = ".".join(path_parts[:-1])
        type_locations[type_id] = (module_path, type_name)

    for item_id, item in index.items():
        item_inner = item.get("inner", {})
        if not isinstance(item_inner, dict) or "impl" not in item_inner:
            continue

        impl_data = item_inner["impl"]

        # Skip synthetic impls (auto-derived Send, Sync, etc.)
        if impl_data.get("is_synthetic"):
            continue

        # Find the type this impl is for
        for_type = impl_data.get("for", {})
        type_id, type_name = _resolve_type_id_and_name(for_type)
        if not type_name or type_id is None:
            continue

        # Look up the module path from paths dict
        location = type_locations.get(str(type_id))
        if location is None:
            continue
        module_path, _ = location

        # Find the symbol
        mod_symbols = symbols_by_module.get(module_path)
        if mod_symbols is None:
            continue
        type_symbol = mod_symbols.get(type_name)
        if type_symbol is None:
            continue

        # Handle trait impls: record the trait in inherits
        trait_info = impl_data.get("trait")
        if trait_info and not impl_data.get("is_negative"):
            trait_name = trait_info.get("path", "").split("::")[-1]
            # Skip standard library traits that aren't interesting
            _SKIP_TRAITS = {
                "Send",
                "Sync",
                "Unpin",
                "UnwindSafe",
                "RefUnwindSafe",
                "Any",
                "Borrow",
                "BorrowMut",
                "From",
                "Into",
                "TryFrom",
                "TryInto",
                "ToOwned",
                "ToString",
                "Clone",
                "CloneToUninit",
                "VZip",
                "Pointable",
                "IntoEither",
                "StructuralPartialEq",
            }
            if trait_name and trait_name not in _SKIP_TRAITS:
                if trait_name not in type_symbol.inherits:
                    type_symbol.inherits.append(trait_name)

        # Collect methods from this impl (only inherent impls, not trait impls)
        if not trait_info:
            for method_id in impl_data.get("items", []):
                method_item = index.get(str(method_id))
                if method_item is None:
                    continue
                method_name = method_item.get("name")
                if not method_name:
                    continue
                method_inner = method_item.get("inner", {})
                if not isinstance(method_inner, dict) or "function" not in method_inner:
                    continue

                if method_name not in type_symbol.children:
                    type_symbol.children[method_name] = SymbolData(
                        name=method_name,
                        kind="method",
                        line=_get_line(method_item),
                        visibility=_rust_visibility(
                            method_item.get("visibility", "default")
                        ),
                    )


def _resolve_type_id_and_name(for_type: dict) -> tuple[int | None, str | None]:
    """Extract the type ID and simple name from a rustdoc 'for' type descriptor."""
    if not isinstance(for_type, dict):
        return None, None

    # resolved_path: {"path": "TypeName", "id": ..., "args": ...}
    if "resolved_path" in for_type:
        rp = for_type["resolved_path"]
        path = rp.get("path", "")
        type_id = rp.get("id")
        name = path.split("::")[-1] if path else None
        return type_id, name

    return None, None
