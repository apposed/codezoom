"""Extract functions, classes, and methods from Python source via AST."""

from __future__ import annotations

import ast
import copy
import warnings
from pathlib import Path

from codezoom.extractors.python import is_python_project
from codezoom.model import NodeData, ProjectGraph, SymbolData


class AstSymbolsExtractor:
    """Populate hierarchy leaf nodes with symbol (function/class/method) data."""

    def can_handle(self, project_dir: Path) -> bool:
        return is_python_project(project_dir)

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        src_dir = _find_source_dir(project_dir, graph.root_node_ids[0])
        if src_dir is None:
            return

        for py_file in src_dir.rglob("*.py"):
            if py_file.name == "__init__.py":
                continue

            relative = py_file.relative_to(src_dir.parent)
            module_name = (
                str(relative).replace("/", ".").replace("\\", ".").removesuffix(".py")
            )

            result = _extract_symbols(py_file)
            if result:
                symbols, class_deps = result

                # Ensure parent packages exist in hierarchy
                _ensure_parents_exist(graph, module_name)

                node = graph.hierarchy.get(module_name)
                if node is None:
                    node = NodeData()
                    graph.hierarchy[module_name] = node

                # Downgrade all symbols to private if the module itself is private
                if not graph.hierarchy[module_name].is_exported:
                    for sym in symbols.values():
                        sym.visibility = "private"
                        for child in sym.children.values():
                            child.visibility = "private"

                node.symbols = symbols
                if class_deps:
                    node.class_deps = class_deps

        # Second pass: surface __all__ re-exports onto package nodes
        for init_file in src_dir.rglob("__init__.py"):
            relative = init_file.parent.relative_to(src_dir.parent)
            package_name = str(relative).replace("/", ".").replace("\\", ".")

            _extract_reexports(init_file, package_name, graph)


def _find_source_dir(project_dir: Path, root_node_id: str) -> Path | None:
    candidate = project_dir / "src" / root_node_id
    if candidate.is_dir():
        return candidate
    candidate = project_dir / root_node_id
    if candidate.is_dir():
        return candidate
    return None


def _ensure_parents_exist(graph: ProjectGraph, module_name: str) -> None:
    """Ensure all parent packages exist and children relationships are set up."""
    parts = module_name.split(".")
    root_id = graph.root_node_ids[0]

    # Build intermediate package nodes
    for i in range(1, len(parts)):
        parent_name = ".".join(parts[:i]) if i > 1 else root_id
        child_name = ".".join(parts[: i + 1])

        # Ensure parent exists
        if parent_name not in graph.hierarchy:
            graph.hierarchy[parent_name] = NodeData()

        # Ensure child exists
        if child_name not in graph.hierarchy:
            # Check if module/package is private based on naming convention
            child_parts = child_name.split(".")
            last_part = child_parts[-1]
            is_exported = not last_part.startswith("_")

            graph.hierarchy[child_name] = NodeData(is_exported=is_exported)

        # Add child to parent's children list
        parent_node = graph.hierarchy[parent_name]
        if child_name not in parent_node.children:
            parent_node.children.append(child_name)


def _extract_reexports(init_file: Path, package_name: str, graph: ProjectGraph) -> None:
    """Parse __init__.py and attach __all__ re-exports as symbols on the package node."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(init_file.read_text())
    except (OSError, SyntaxError):
        return

    # Collect __all__ names
    all_names: set[str] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__all__"
            and isinstance(node.value, ast.List)
        ):
            for elt in node.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    all_names.add(elt.value)

    if not all_names:
        return

    # Build map: name -> source submodule (dotted path) from relative imports
    # e.g. `from ._core import Artifact` -> {"Artifact": "pkg._core"}
    import_map: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0 or node.module is None:
            continue  # only relative imports matter here
        # Resolve relative module: level=1 means same package
        source_module = package_name + "." + node.module
        for alias in node.names:
            import_map[alias.asname or alias.name] = source_module

    # Attach re-exported symbols to the package node
    package_node = graph.hierarchy.get(package_name)
    if package_node is None:
        package_node = NodeData()
        graph.hierarchy[package_name] = package_node
    if package_node.symbols is None:
        package_node.symbols = {}

    for name in sorted(all_names):
        source_module = import_map.get(name)
        if source_module is None:
            continue
        source_node = graph.hierarchy.get(source_module)
        if source_node is None or source_node.symbols is None:
            continue
        sym = source_node.symbols.get(name)
        if sym is None:
            continue
        # Deep-copy so mutations don't affect the private-module copy
        reexport = copy.deepcopy(sym)
        reexport.visibility = "public"
        reexport.origin = source_module
        package_node.symbols[name] = reexport

    # Propagate class_deps: for each re-exported class that has deps in its
    # source module, keep only the deps that are also re-exported here.
    pkg_class_deps: dict[str, list[str]] = {}
    for name in sorted(all_names):
        source_module = import_map.get(name)
        if source_module is None:
            continue
        source_node = graph.hierarchy.get(source_module)
        if source_node is None or source_node.class_deps is None:
            continue
        src_deps = source_node.class_deps.get(name)
        if not src_deps:
            continue
        local_deps = [d for d in src_deps if d in all_names]
        if local_deps:
            pkg_class_deps[name] = local_deps

    if pkg_class_deps:
        if package_node.class_deps is None:
            package_node.class_deps = {}
        package_node.class_deps.update(pkg_class_deps)


class _CallExtractor(ast.NodeVisitor):
    """Collect names called within a function/method body."""

    def __init__(self) -> None:
        self.called_names: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            self.called_names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                self.called_names.add(node.func.attr)
        self.generic_visit(node)


def _collect_annotation_names(annotation: ast.expr) -> set[str]:
    """Recursively collect all Name ids referenced in a type annotation.

    Handles plain names (``Foo``), subscripts (``list[Foo]``), binary-or
    unions (``Foo | None``), and tuples of types.
    """
    names: set[str] = set()
    if isinstance(annotation, ast.Name):
        names.add(annotation.id)
    elif isinstance(annotation, ast.Attribute):
        names.add(annotation.attr)
    elif isinstance(annotation, ast.Subscript):
        names.update(_collect_annotation_names(annotation.value))
        names.update(_collect_annotation_names(annotation.slice))
    elif isinstance(annotation, ast.BinOp):  # X | Y (PEP 604 union)
        names.update(_collect_annotation_names(annotation.left))
        names.update(_collect_annotation_names(annotation.right))
    elif isinstance(annotation, ast.Tuple):
        for elt in annotation.elts:
            names.update(_collect_annotation_names(elt))
    return names


def _get_python_visibility(name: str) -> str:
    """
    Determine visibility based on Python naming conventions.
    Python only has two levels: public and private (by convention).

    __dunder__ -> "public" (special methods are part of the public API)
    __private or _private -> "private" (internal use)
    public -> "public"
    """
    if name.startswith("__") and name.endswith("__"):
        return "public"  # Special methods like __init__ are part of the public API
    elif name.startswith("_"):
        return "private"  # Both _weak and __strong are private by convention
    else:
        return "public"


def _extract_symbols(
    file_path: Path,
) -> tuple[dict[str, SymbolData], dict[str, list[str]]] | None:
    """Return (symbols, class_deps) for top-level functions and classes in *file_path*.

    *class_deps* maps each class name to a sorted list of other top-level symbol
    names in the same module that it depends on — via method calls or class-body
    type annotations (e.g. dataclass field types).
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(file_path.read_text())
    except (OSError, SyntaxError):
        return None

    results: dict[str, SymbolData] = {}
    # Annotation names collected from class bodies (dataclass fields, etc.)
    class_body_annotations: dict[str, set[str]] = {}

    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            ext = _CallExtractor()
            ext.visit(node)
            results[node.name] = SymbolData(
                name=node.name,
                kind="function",
                line=node.lineno,
                calls=sorted(ext.called_names),
                visibility=_get_python_visibility(node.name),
            )

        elif isinstance(node, ast.ClassDef):
            bases: list[str] = []
            for base in node.bases:
                if isinstance(base, ast.Name):
                    bases.append(base.id)
                elif isinstance(base, ast.Attribute):
                    bases.append(base.attr)

            methods: dict[str, SymbolData] = {}
            ann_names: set[str] = set()
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    ext = _CallExtractor()
                    ext.visit(item)
                    methods[item.name] = SymbolData(
                        name=item.name,
                        kind="method",
                        line=item.lineno,
                        calls=sorted(ext.called_names),
                        visibility=_get_python_visibility(item.name),
                    )
                elif isinstance(item, ast.AnnAssign):
                    # Collect type names from class-body annotations (dataclass
                    # fields, ClassVar, etc.) as structural dependencies.
                    ann_names.update(_collect_annotation_names(item.annotation))

            if ann_names:
                class_body_annotations[node.name] = ann_names

            # Class-level calls (decorators, class-var assignments, etc.)
            ext = _CallExtractor()
            for item in node.body:
                if not isinstance(item, ast.FunctionDef):
                    ext.visit(item)

            results[node.name] = SymbolData(
                name=node.name,
                kind="class",
                line=node.lineno,
                calls=sorted(ext.called_names),
                inherits=bases,
                children=methods,
                visibility=_get_python_visibility(node.name),
            )

    if not results:
        return None

    # Compute class_deps: for each class, union method calls and class-body
    # annotation names, then filter to other top-level symbols in this module.
    # This mirrors what jdeps provides for Java.
    class_deps: dict[str, list[str]] = {}
    for sym_name, sym in results.items():
        if sym.kind != "class":
            continue
        all_refs: set[str] = set(class_body_annotations.get(sym_name, ()))
        for method in sym.children.values():
            all_refs.update(method.calls)
        deps = sorted(n for n in all_refs if n in results and n != sym_name)
        if deps:
            class_deps[sym_name] = deps

    return results, class_deps
