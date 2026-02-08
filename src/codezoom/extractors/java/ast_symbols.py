"""Extract classes, interfaces, enums, and methods from Java source via tree-sitter."""

from __future__ import annotations

import logging
from pathlib import Path

from codezoom.model import NodeData, ProjectGraph, SymbolData

logger = logging.getLogger(__name__)

# Files to skip when walking Java sources.
_SKIP_FILES = {"package-info.java", "module-info.java"}

# tree-sitter node types that represent Java type declarations.
_TYPE_DECL_TYPES = {"class_declaration", "interface_declaration", "enum_declaration"}

# tree-sitter node types that represent method-like declarations.
_METHOD_DECL_TYPES = {"method_declaration", "constructor_declaration"}


class JavaAstSymbolsExtractor:
    """Populate hierarchy leaf nodes with Java symbol data."""

    def can_handle(self, project_dir: Path) -> bool:
        return (project_dir / "pom.xml").exists()

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        try:
            import tree_sitter_java as tsjava
            from tree_sitter import Language, Parser
        except ImportError:
            logger.warning(
                "tree-sitter / tree-sitter-java not installed — "
                "skipping Java symbol extraction. "
                "Install with: pip install codezoom[java]"
            )
            return

        src_dir = project_dir / "src" / "main" / "java"
        if not src_dir.is_dir():
            return

        language = Language(tsjava.language())
        parser = Parser(language)

        file_count = 0
        symbol_count = 0
        for java_file in src_dir.rglob("*.java"):
            if java_file.name in _SKIP_FILES:
                continue

            # Compute package name from directory structure.
            relative = java_file.parent.relative_to(src_dir)
            package_name = str(relative).replace("/", ".").replace("\\", ".")
            if package_name == ".":
                package_name = ""

            symbols = _extract_symbols(parser, java_file)
            if not symbols:
                continue

            file_count += 1
            symbol_count += len(symbols)

            # Merge into the hierarchy node for this package.
            node = graph.hierarchy.get(package_name)
            if node is None:
                node = NodeData()
                graph.hierarchy[package_name] = node
            if node.symbols is None:
                node.symbols = {}
            node.symbols.update(symbols)

        logger.debug("Java AST: %d files, %d symbols", file_count, symbol_count)


def _extract_symbols(parser, java_file: Path) -> dict[str, SymbolData] | None:
    """Extract type and method symbols from a single Java file."""
    try:
        source = java_file.read_bytes()
        tree = parser.parse(source)
    except Exception:
        return None

    results: dict[str, SymbolData] = {}

    for node in tree.root_node.children:
        if node.type in _TYPE_DECL_TYPES:
            symbol = _extract_type(node, source)
            if symbol:
                results[symbol.name] = symbol

    return results or None


def _extract_type(node, source: bytes) -> SymbolData | None:
    """Extract a class/interface/enum declaration and its methods."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None

    name = name_node.text.decode("utf-8")
    line = node.start_point[0] + 1  # 1-indexed

    # Extract visibility modifier
    visibility = _extract_visibility(node)

    # Extract superclass and interfaces.
    inherits: list[str] = []

    superclass = node.child_by_field_name("superclass")
    if superclass is not None:
        # superclass node wraps a type_identifier
        for child in superclass.children:
            if child.type == "type_identifier":
                inherits.append(child.text.decode("utf-8"))

    interfaces = node.child_by_field_name("interfaces")
    if interfaces is not None:
        for child in interfaces.children:
            if child.type == "type_identifier":
                inherits.append(child.text.decode("utf-8"))

    # Extract methods from the class body.
    methods: dict[str, SymbolData] = {}
    body = node.child_by_field_name("body")
    if body is not None:
        for child in body.children:
            if child.type in _METHOD_DECL_TYPES:
                method = _extract_method(child, source)
                if method:
                    methods[method.name] = method

    return SymbolData(
        name=name,
        kind="class",
        line=line,
        inherits=inherits,
        children=methods,
        visibility=visibility,
    )


def _extract_method(node, source: bytes) -> SymbolData | None:
    """Extract a method/constructor declaration and its calls."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None

    name = name_node.text.decode("utf-8")
    line = node.start_point[0] + 1

    # Extract visibility modifier
    visibility = _extract_visibility(node)

    # Collect method invocations within the method body.
    calls: set[str] = set()
    body = node.child_by_field_name("body")
    if body is not None:
        _collect_calls(body, calls)

    return SymbolData(
        name=name,
        kind="method",
        line=line,
        calls=sorted(calls),
        visibility=visibility,
    )


def _collect_calls(node, calls: set[str]) -> None:
    """Recursively collect method_invocation names from a subtree."""
    if node.type == "method_invocation":
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            calls.add(name_node.text.decode("utf-8"))
    for child in node.children:
        _collect_calls(child, calls)


def _extract_visibility(node) -> str:
    """
    Extract visibility modifier from a type or method declaration.
    Returns: "public", "protected", "private", or "package" (package-private).
    """
    # Look for modifiers node
    modifiers = node.child_by_field_name("modifiers")
    if modifiers is None:
        return "package"  # Default is package-private in Java

    # Check for visibility modifiers
    for child in modifiers.children:
        if child.type == "public":
            return "public"
        elif child.type == "protected":
            return "protected"
        elif child.type == "private":
            return "private"

    return "package"  # No explicit visibility modifier = package-private
