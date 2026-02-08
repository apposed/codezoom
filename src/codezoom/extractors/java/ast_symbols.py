"""Extract classes, interfaces, enums, and methods from Java source via tree-sitter."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from collections import defaultdict
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

        # Extract method calls from bytecode using javap
        javap_path = shutil.which("javap")
        classes_dir = project_dir / "target" / "classes"
        if javap_path and classes_dir.is_dir():
            method_calls = _extract_method_calls_from_bytecode(javap_path, classes_dir)
            _merge_method_calls(graph, method_calls)
        else:
            if not javap_path:
                logger.warning("javap not found — method call edges will be incomplete")
            if not classes_dir.is_dir():
                logger.warning("target/classes not found — run `mvn compile` first for accurate method calls")


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


def _extract_type(node, source: bytes, parent_name: str = "") -> SymbolData | None:
    """Extract a class/interface/enum declaration and its methods."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None

    simple_name = name_node.text.decode("utf-8")
    # Qualified name for inner classes
    name = f"{parent_name}.{simple_name}" if parent_name else simple_name
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

    # Extract methods and nested classes from the class body.
    children: dict[str, SymbolData] = {}
    body = node.child_by_field_name("body")
    if body is not None:
        for child in body.children:
            if child.type in _METHOD_DECL_TYPES:
                method = _extract_method(child, source)
                if method:
                    children[method.name] = method
            elif child.type in _TYPE_DECL_TYPES:
                # Recursively extract nested classes
                nested_class = _extract_type(child, source, parent_name=name)
                if nested_class:
                    children[nested_class.name] = nested_class

    return SymbolData(
        name=name,
        kind="class",
        line=line,
        inherits=inherits,
        children=children,
        visibility=visibility,
    )


def _extract_method(node, source: bytes) -> SymbolData | None:
    """Extract a method/constructor declaration and its calls."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        # Constructors don't have a name field, use the class name
        if node.type == "constructor_declaration":
            # For constructors, we'll use a special marker
            simple_name = "<init>"
        else:
            return None
    else:
        simple_name = name_node.text.decode("utf-8")

    line = node.start_point[0] + 1

    # Extract visibility modifier
    visibility = _extract_visibility(node)

    # Extract parameter types to create a unique signature
    params_node = node.child_by_field_name("parameters")
    param_types = _extract_parameter_types(params_node, source)

    # Create unique name with signature: methodName(Type1,Type2,...)
    signature = f"({','.join(param_types)})"
    unique_name = f"{simple_name}{signature}"

    # Note: Method calls are extracted from bytecode in a separate pass
    # to get accurate signatures including overload resolution

    return SymbolData(
        name=unique_name,
        kind="method",
        line=line,
        calls=[],  # Will be populated from bytecode analysis
        visibility=visibility,
    )


def _extract_parameter_types(params_node, source: bytes) -> list[str]:
    """Extract parameter type names from a formal_parameters node."""
    if params_node is None:
        return []

    param_types = []
    for child in params_node.children:
        if child.type == "formal_parameter" or child.type == "spread_parameter":
            # Get the type node
            type_node = child.child_by_field_name("type")
            if type_node is not None:
                # Extract the type text (handles primitives, objects, arrays, generics)
                type_text = type_node.text.decode("utf-8")
                # Simplify generic types to just the base type for readability
                # e.g., "List<String>" -> "List"
                if "<" in type_text:
                    type_text = type_text.split("<")[0]
                param_types.append(type_text)

    return param_types




def _extract_visibility(node) -> str:
    """
    Extract visibility modifier from a type or method declaration.
    Returns: "public", "protected", "private", or "package" (package-private).
    """
    # Find modifiers node among children (it's not a named field in tree-sitter-java)
    modifiers = None
    for child in node.children:
        if child.type == "modifiers":
            modifiers = child
            break

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


def _jvm_sig_to_java(jvm_sig: str) -> str:
    """Convert JVM method signature to readable Java parameter list.

    Examples:
        (I)D -> int
        (Ljava/lang/String;)V -> String
        (ID[Ljava/lang/Object;)V -> int,double,Object[]
    """
    # Extract parameter part (between parentheses)
    if not jvm_sig.startswith("("):
        return ""

    param_part = jvm_sig[1:jvm_sig.index(")")]
    if not param_part:
        return ""  # No parameters

    params = []
    i = 0
    while i < len(param_part):
        array_depth = 0
        while i < len(param_part) and param_part[i] == "[":
            array_depth += 1
            i += 1

        if i >= len(param_part):
            break

        c = param_part[i]
        if c == "L":
            # Object type: Lpackage/Class;
            end = param_part.index(";", i)
            fqcn = param_part[i+1:end]
            # Take just the class name (last part after /)
            class_name = fqcn.split("/")[-1]
            params.append(class_name + "[]" * array_depth)
            i = end + 1
        elif c in "BCDFIJSZ":
            # Primitive type
            primitives = {
                "B": "byte", "C": "char", "D": "double", "F": "float",
                "I": "int", "J": "long", "S": "short", "Z": "boolean"
            }
            params.append(primitives[c] + "[]" * array_depth)
            i += 1
        else:
            i += 1

    return ",".join(params)


def _extract_method_calls_from_bytecode(
    javap_path: str, classes_dir: Path
) -> dict[str, dict[str, dict[str, list[str]]]]:
    """Extract method calls from compiled bytecode using javap -c.

    Returns: {package: {class: {method_sig: [called_method_sigs]}}}
    """
    class_files = sorted(classes_dir.rglob("*.class"))
    if not class_files:
        return {}

    result = subprocess.run(
        [javap_path, "-c", "-p", *(str(f) for f in class_files)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning("javap -c failed: %s", result.stderr)
        return {}

    # Parse javap -c output
    # Format:
    #   Compiled from "Foo.java"
    #   public class pkg.Foo {
    #     public void method(int):
    #       Code:
    #          0: invokevirtual #7  // Method bar:(I)V

    classfile_pattern = re.compile(r"^Compiled from \"(.+)\"$")
    class_pattern = re.compile(r"^(?:public |protected |private )?(?:static |final |abstract )*(?:class|interface|enum) ([^\s<{]+)")
    method_pattern = re.compile(r"^\s+(?:public |protected |private |static |final |synchronized |native |abstract )*([^(]+)\(([^)]*)\);$")
    invoke_pattern = re.compile(r"invoke(?:virtual|special|static|interface)\s+#\d+\s+//.*Method ([^:]+):(.+)")

    # package -> class -> method_sig -> [called_sigs]
    calls_data: dict[str, dict[str, dict[str, list[str]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )

    current_package = ""
    current_class = ""
    current_method = ""

    for line in result.stdout.splitlines():
        # Track current class
        cm = class_pattern.match(line)
        if cm:
            fqcn = cm.group(1).replace("$", ".")  # Handle inner classes
            if "." in fqcn:
                current_package = fqcn.rsplit(".", 1)[0]
                current_class = fqcn.rsplit(".", 1)[1]
            else:
                current_package = ""
                current_class = fqcn
            continue

        # Track current method
        mm = method_pattern.match(line)
        if mm and current_class:
            method_name = mm.group(1).strip().split()[-1]  # Get method name
            params_str = mm.group(2).strip()

            # Build method signature
            if params_str:
                # Simplify parameter types (remove package prefixes)
                params = [p.strip().split(".")[-1].split("[]")[0] + ("[]" if "[]" in p else "")
                         for p in params_str.split(",")]
                current_method = f"{method_name}({','.join(params)})"
            else:
                current_method = f"{method_name}()"
            continue

        # Extract method invocations
        im = invoke_pattern.search(line)
        if im and current_class and current_method:
            called_method_name = im.group(1).split("/")[-1].replace("$", ".")
            called_jvm_sig = im.group(2)

            # Convert JVM signature to Java parameter list
            params = _jvm_sig_to_java(called_jvm_sig)
            called_sig = f"{called_method_name}({params})"

            calls_data[current_package][current_class][current_method].append(called_sig)

    logger.debug("Extracted method calls from bytecode for %d packages", len(calls_data))
    return calls_data


def _merge_method_calls(
    graph: ProjectGraph,
    calls_data: dict[str, dict[str, dict[str, list[str]]]]
) -> None:
    """Merge bytecode-extracted method calls into graph symbols."""
    for package, classes in calls_data.items():
        if package not in graph.hierarchy:
            continue

        node = graph.hierarchy[package]
        if not node.symbols:
            continue

        for class_name, methods in classes.items():
            # Handle inner classes (A.B format)
            if class_name not in node.symbols:
                continue

            class_symbol = node.symbols[class_name]
            if not class_symbol.children:
                continue

            for method_sig, calls in methods.items():
                if method_sig in class_symbol.children:
                    # Update the calls list with bytecode-extracted calls
                    class_symbol.children[method_sig].calls = sorted(set(calls))
