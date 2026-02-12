"""Extract classes, interfaces, enums, and methods from Java source via javalang."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path

from codezoom.model import NodeData, ProjectGraph, SymbolData

logger = logging.getLogger(__name__)

# Regex patterns for fallback extraction when javalang fails on modern Java.
_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;")
_CLASS_RE = re.compile(
    r"^\s*(public\s+|protected\s+|private\s+)?"
    r"(static\s+|final\s+|abstract\s+)*"
    r"(class|interface|enum|record)\s+(\w+)"
    r"(?:\s*<[^>]*>)?"  # optional type parameters
    r"(?:\s*\([^)]*\))?"  # optional record components
    r"(?:\s+extends\s+([\w.<>,\s]+?))?"
    r"(?:\s+implements\s+([\w.<>,\s]+?))?"
    r"\s*\{"
)
_METHOD_RE = re.compile(
    r"^\s*(public\s+|protected\s+|private\s+)?"
    r"(static\s+|final\s+|abstract\s+|synchronized\s+|default\s+)*"
    r"(?:(?:<[^>]*>\s+)?)"  # optional type parameters
    r"([\w.<>,\[\]?]+)\s+"  # return type (or constructor has none)
    r"(\w+)\s*\(([^)]*)\)"  # method name and params
    r"\s*(?:throws\s+[\w.,\s]+)?"
    r"\s*[{;]"
)


def _is_gradle_project(project_dir: Path) -> bool:
    return (project_dir / "build.gradle.kts").exists() or (
        project_dir / "build.gradle"
    ).exists()


def _find_source_root(project_dir: Path) -> Path | None:
    """Find the Java source root directory."""
    src_main_java = project_dir / "src" / "main" / "java"
    if src_main_java.is_dir():
        return src_main_java
    src_main_kotlin = project_dir / "src" / "main" / "kotlin"
    if src_main_kotlin.is_dir():
        return src_main_kotlin
    return None


def _visibility_from_modifiers(modifiers: set[str] | None) -> str:
    """Determine visibility from Java modifier set."""
    if not modifiers:
        return "package"
    if "public" in modifiers:
        return "public"
    if "protected" in modifiers:
        return "protected"
    if "private" in modifiers:
        return "private"
    return "package"


def _format_param_type(param) -> str:
    """Format a parameter type to a simple string."""
    if param.type is None:
        return "?"
    name = param.type.name
    if param.type.dimensions:
        name += "[]" * len(param.type.dimensions)
    return name


def _extract_method_calls(body) -> list[str]:
    """Extract method call names from a method body using javalang tree walking."""
    import javalang

    calls: list[str] = []
    if body is None:
        return calls

    try:
        # Walk all nodes in the body looking for MethodInvocation
        for node in body:
            if node is None:
                continue
            if isinstance(node, (list, tuple)):
                calls.extend(_extract_method_calls(node))
                continue
            if not hasattr(node, "children"):
                continue

            if isinstance(node, javalang.tree.MethodInvocation):
                qualifier = node.qualifier or ""
                if qualifier:
                    calls.append(f"{qualifier}.{node.member}()")
                else:
                    calls.append(f"{node.member}()")

            # Recurse into child nodes
            for child in node.children:
                if child is None:
                    continue
                if isinstance(child, (list, tuple)):
                    calls.extend(_extract_method_calls(child))
                elif hasattr(child, "children"):
                    calls.extend(_extract_method_calls([child]))
    except (AttributeError, TypeError):
        pass

    return calls


def _extract_class_symbols(class_node, prefix: str = "") -> tuple[str, SymbolData]:
    """Extract symbol data from a class/interface/enum declaration node.

    Returns (display_name, SymbolData).
    """
    import javalang

    name = class_node.name
    display_name = f"{prefix}{name}" if prefix else name

    # Determine kind
    if isinstance(class_node, javalang.tree.InterfaceDeclaration):
        kind = "class"  # model only has "class", "function", "method"
    elif isinstance(class_node, javalang.tree.EnumDeclaration):
        kind = "class"
    else:
        kind = "class"

    # Inheritance
    inherits: list[str] = []
    if hasattr(class_node, "extends") and class_node.extends:
        if isinstance(class_node.extends, list):
            # Interface can extend multiple
            for ext in class_node.extends:
                if hasattr(ext, "name"):
                    inherits.append(ext.name)
        elif hasattr(class_node.extends, "name"):
            if class_node.extends.name != "Object":
                inherits.append(class_node.extends.name)

    if hasattr(class_node, "implements") and class_node.implements:
        for impl in class_node.implements:
            if hasattr(impl, "name"):
                inherits.append(impl.name)

    # Line number
    line = class_node.position.line if class_node.position else None

    # Visibility
    visibility = _visibility_from_modifiers(class_node.modifiers)

    # Methods and constructors
    methods: dict[str, SymbolData] = {}
    body = getattr(class_node, "body", None)
    if body is None:
        body = []

    for member in body:
        if isinstance(
            member,
            (javalang.tree.MethodDeclaration, javalang.tree.ConstructorDeclaration),
        ):
            method_name = member.name
            params = member.parameters or []
            param_types = [_format_param_type(p) for p in params]
            method_sig = f"{method_name}({','.join(param_types)})"

            method_line = member.position.line if member.position else None
            method_vis = _visibility_from_modifiers(member.modifiers)

            # Extract calls from method body
            calls = _extract_method_calls(member.body)
            # Deduplicate and sort
            calls = sorted(set(calls))

            methods[method_sig] = SymbolData(
                name=method_sig,
                kind="method",
                line=method_line,
                calls=calls,
                visibility=method_vis,
            )

    # Handle inner/nested classes — add as children
    inner_children: dict[str, SymbolData] = {}
    for member in body:
        if isinstance(
            member,
            (
                javalang.tree.ClassDeclaration,
                javalang.tree.InterfaceDeclaration,
                javalang.tree.EnumDeclaration,
            ),
        ):
            inner_display = f"{display_name}.{member.name}"
            _, inner_sym = _extract_class_symbols(member, prefix=f"{display_name}.")
            inner_children[inner_display] = inner_sym

    # Merge methods and inner classes
    all_children = {**methods, **inner_children}

    symbol = SymbolData(
        name=display_name,
        kind=kind,
        line=line,
        inherits=inherits,
        children=all_children,
        visibility=visibility,
    )

    return display_name, symbol


def _simplify_type(type_str: str) -> str:
    """Simplify a Java type like 'java.util.List<String>' to 'List'."""
    # Remove generics
    result = re.sub(r"<[^>]*>", "", type_str).strip()
    # Take just the class name (last dot-segment)
    if "." in result:
        result = result.rsplit(".", 1)[1]
    return result


def _extract_file_symbols_fallback(
    java_file: Path, source: str
) -> tuple[str | None, dict[str, SymbolData]]:
    """Regex-based fallback for files javalang can't parse (Java 14+ features).

    Extracts package, classes, and method signatures at a basic level.
    """
    package = None
    symbols: dict[str, SymbolData] = {}

    lines = source.split("\n")

    # Extract package
    for line in lines:
        m = _PACKAGE_RE.match(line)
        if m:
            package = m.group(1)
            break

    # Track class nesting via brace counting
    current_class: str | None = None
    current_class_line: int | None = None
    current_class_vis: str = "package"
    current_inherits: list[str] = []
    current_methods: dict[str, SymbolData] = {}
    brace_depth = 0
    class_brace_depth = -1
    # For multi-line declarations (e.g. record Foo(\n...\n) {)
    pending_decl: str | None = None
    pending_decl_line: int | None = None

    for line_num, line in enumerate(lines, 1):
        stripped = line.strip()

        # Skip comments
        if (
            stripped.startswith("//")
            or stripped.startswith("/*")
            or stripped.startswith("*")
        ):
            pass

        # Handle multi-line declarations: accumulate until we see "{"
        if pending_decl is not None:
            pending_decl += " " + stripped
            if "{" in stripped:
                # Try to match the accumulated declaration
                cm = _CLASS_RE.match(pending_decl)
                if cm:
                    # Process as class (handled below)
                    stripped = pending_decl
                    line_num = pending_decl_line  # type: ignore[assignment]
                pending_decl = None
                pending_decl_line = None
                if cm:
                    # Fall through to class handling below
                    pass
                else:
                    brace_depth += stripped.count("{") - stripped.count("}")
                    continue
            else:
                continue

        # Track braces (approximate — doesn't handle braces in strings/comments)
        brace_depth += stripped.count("{") - stripped.count("}")

        # Check for class/interface/enum/record declaration
        cm = _CLASS_RE.match(stripped)

        # If it looks like a class/record start but doesn't have "{" yet,
        # start accumulating for multi-line declaration
        if (
            cm is None
            and re.match(
                r"^\s*(public\s+|protected\s+|private\s+)?"
                r"(static\s+|final\s+|abstract\s+)*"
                r"(class|interface|enum|record)\s+\w+",
                stripped,
            )
            and "{" not in stripped
        ):
            pending_decl = stripped
            pending_decl_line = line_num
            continue
        if cm:
            # Save previous class if any
            if current_class is not None:
                symbols[current_class] = SymbolData(
                    name=current_class,
                    kind="class",
                    line=current_class_line,
                    inherits=current_inherits,
                    children=current_methods,
                    visibility=current_class_vis,
                )

            vis_str = (cm.group(1) or "").strip()
            current_class = cm.group(4)
            current_class_line = line_num
            current_class_vis = (
                vis_str if vis_str in ("public", "protected", "private") else "package"
            )
            current_inherits = []
            current_methods = {}
            class_brace_depth = brace_depth

            # Parse extends
            if cm.group(5):
                for ext in cm.group(5).split(","):
                    name = _simplify_type(ext.strip())
                    if name and name != "Object":
                        current_inherits.append(name)
            # Parse implements
            if cm.group(6):
                for impl in cm.group(6).split(","):
                    name = _simplify_type(impl.strip())
                    if name:
                        current_inherits.append(name)
            continue

        # Check for method declaration (only inside a class)
        if current_class is not None:
            mm = _METHOD_RE.match(stripped)
            if mm:
                vis_str = (mm.group(1) or "").strip()
                method_vis = (
                    vis_str
                    if vis_str in ("public", "protected", "private")
                    else "package"
                )
                method_name = mm.group(4)
                params_str = mm.group(5).strip()

                if params_str:
                    # Parse param types: "String name, int count" -> ["String", "int"]
                    param_types = []
                    for param in params_str.split(","):
                        parts = param.strip().split()
                        if parts:
                            # Last part is the name, everything before is type + annotations
                            ptype = _simplify_type(
                                parts[-2] if len(parts) >= 2 else parts[0]
                            )
                            param_types.append(ptype)
                    method_sig = f"{method_name}({','.join(param_types)})"
                else:
                    method_sig = f"{method_name}()"

                current_methods[method_sig] = SymbolData(
                    name=method_sig,
                    kind="method",
                    line=line_num,
                    visibility=method_vis,
                )

        # Check if we've exited the current class
        if current_class is not None and brace_depth < class_brace_depth:
            symbols[current_class] = SymbolData(
                name=current_class,
                kind="class",
                line=current_class_line,
                inherits=current_inherits,
                children=current_methods,
                visibility=current_class_vis,
            )
            current_class = None
            current_methods = {}

    # Save last class
    if current_class is not None:
        symbols[current_class] = SymbolData(
            name=current_class,
            kind="class",
            line=current_class_line,
            inherits=current_inherits,
            children=current_methods,
            visibility=current_class_vis,
        )

    if symbols:
        logger.debug(
            "Fallback extraction for %s: %d classes", java_file.name, len(symbols)
        )
    else:
        logger.debug("Fallback extraction for %s: no symbols found", java_file.name)

    return package, symbols


def _extract_file_symbols(java_file: Path) -> tuple[str | None, dict[str, SymbolData]]:
    """Parse a single Java file and return (package, {class_name: SymbolData}).

    Returns (None, {}) if parsing fails.
    """
    import javalang

    try:
        source = java_file.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.debug("Could not read %s: %s", java_file, e)
        return None, {}

    try:
        tree = javalang.parse.parse(source)
    except javalang.parser.JavaSyntaxError:
        return _extract_file_symbols_fallback(java_file, source)
    except Exception:
        return _extract_file_symbols_fallback(java_file, source)

    # Extract package
    package = None
    if tree.package:
        package = tree.package.name

    symbols: dict[str, SymbolData] = {}

    # Extract top-level type declarations
    for type_decl in tree.types:
        if type_decl is None:
            continue
        if isinstance(
            type_decl,
            (
                javalang.tree.ClassDeclaration,
                javalang.tree.InterfaceDeclaration,
                javalang.tree.EnumDeclaration,
            ),
        ):
            display_name, sym = _extract_class_symbols(type_decl)
            symbols[display_name] = sym

    return package, symbols


class GradleSourceSymbolsExtractor:
    """Populate hierarchy leaf nodes with Java symbol data from source files."""

    def can_handle(self, project_dir: Path) -> bool:
        return _is_gradle_project(project_dir)

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        try:
            import javalang  # noqa: F401
        except ImportError:
            logger.warning(
                "javalang not installed — skipping Java source symbol extraction. "
                "Install with: pip install codezoom[gradle]"
            )
            return

        source_root = _find_source_root(project_dir)
        if source_root is None:
            logger.warning(
                "No source root found (expected src/main/java/) — "
                "skipping symbol extraction."
            )
            return

        java_files = sorted(source_root.rglob("*.java"))
        if not java_files:
            logger.warning("No .java files found under %s", source_root)
            return

        # Extract symbols from all files, grouped by package
        symbols_by_package: dict[str, dict[str, SymbolData]] = defaultdict(dict)
        parse_errors = 0

        for java_file in java_files:
            package, symbols = _extract_file_symbols(java_file)
            if package is None and not symbols:
                # File couldn't be read at all
                parse_errors += 1
                continue
            if package is None:
                # Derive from directory structure
                rel = java_file.relative_to(source_root).parent
                if rel != Path("."):
                    package = str(rel).replace("/", ".").replace("\\", ".")
                else:
                    package = "(default)"

            symbols_by_package[package].update(symbols)

        # Merge into graph hierarchy
        for package_name, symbols in symbols_by_package.items():
            node = graph.hierarchy.get(package_name)
            if node is None:
                node = NodeData()
                graph.hierarchy[package_name] = node
            if node.symbols is None:
                node.symbols = {}
            node.symbols.update(symbols)

        symbol_count = sum(len(syms) for syms in symbols_by_package.values())
        logger.debug(
            "Java source: %d packages, %d symbols (%d parse errors)",
            len(symbols_by_package),
            symbol_count,
            parse_errors,
        )
