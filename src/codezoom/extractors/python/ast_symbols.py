"""Extract functions, classes, and methods from Python source via AST."""

from __future__ import annotations

import ast
from pathlib import Path

from codezoom.model import NodeData, ProjectGraph, SymbolData


class AstSymbolsExtractor:
    """Populate hierarchy leaf nodes with symbol (function/class/method) data."""

    def can_handle(self, project_dir: Path) -> bool:
        return (project_dir / "pyproject.toml").exists()

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        src_dir = _find_source_dir(project_dir, graph.root_node_id)
        if src_dir is None:
            return

        for py_file in src_dir.rglob("*.py"):
            if py_file.name == "__init__.py":
                continue

            relative = py_file.relative_to(src_dir.parent)
            module_name = (
                str(relative).replace("/", ".").replace("\\", ".").removesuffix(".py")
            )

            symbols = _extract_symbols(py_file)
            if symbols:
                # Ensure parent packages exist in hierarchy
                _ensure_parents_exist(graph, module_name)

                node = graph.hierarchy.get(module_name)
                if node is None:
                    node = NodeData()
                    graph.hierarchy[module_name] = node
                node.symbols = symbols


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
    root_id = graph.root_node_id

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


def _extract_symbols(file_path: Path) -> dict[str, SymbolData] | None:
    """Return symbol data for top-level functions and classes in *file_path*."""
    try:
        tree = ast.parse(file_path.read_text())
    except Exception:
        return None

    results: dict[str, SymbolData] = {}

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

    return results or None
