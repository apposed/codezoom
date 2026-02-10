"""Extract classes, interfaces, enums, and methods from Java bytecode via javap."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

from codezoom.model import NodeData, ProjectGraph, SymbolData

logger = logging.getLogger(__name__)


class JavaAstSymbolsExtractor:
    """Populate hierarchy leaf nodes with Java symbol data from compiled bytecode."""

    def can_handle(self, project_dir: Path) -> bool:
        return (project_dir / "pom.xml").exists()

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        javap_path = shutil.which("javap")
        if not javap_path:
            logger.warning(
                "javap not found on PATH — skipping Java symbol extraction. "
                "Ensure a JDK is installed."
            )
            return

        classes_dir = project_dir / "target" / "classes"
        if not classes_dir.is_dir():
            logger.warning(
                "target/classes not found — run `mvn compile` first. "
                "Skipping Java symbol extraction."
            )
            return

        # Extract class and method declarations from bytecode
        symbols_by_package = _extract_symbols_from_bytecode(javap_path, classes_dir)

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
            "Java bytecode: %d packages, %d symbols",
            len(symbols_by_package),
            symbol_count,
        )

        # Extract method calls from bytecode
        method_calls = _extract_method_calls_from_bytecode(javap_path, classes_dir)
        _merge_method_calls(graph, method_calls)


def _visibility_from_flags(flags_str: str) -> str:
    """Determine visibility from ACC_* flags string."""
    if "ACC_PUBLIC" in flags_str:
        return "public"
    if "ACC_PROTECTED" in flags_str:
        return "protected"
    if "ACC_PRIVATE" in flags_str:
        return "private"
    return "package"


def _visibility_from_modifiers(modifiers: str) -> str:
    """Determine visibility from Java modifier keywords."""
    if "public" in modifiers:
        return "public"
    if "protected" in modifiers:
        return "protected"
    if "private" in modifiers:
        return "private"
    return "package"


def _resolve_class_identity(
    classfile: Path,
    classes_dir: Path,
    fqcn_with_dollar: str,
) -> tuple[str, str]:
    """Determine (package, class_name) from a classfile path and FQCN.

    Returns (package, class_name).
    """
    try:
        rel_path = classfile.relative_to(classes_dir)
        package_path = rel_path.parent
        package = str(package_path).replace("/", ".").replace("\\", ".")
        if package == ".":
            package = ""
        class_name = classfile.stem.replace("$", ".")
    except ValueError:
        # Fallback if relative path fails
        if "." in fqcn_with_dollar:
            package = fqcn_with_dollar.rsplit(".", 1)[0]
            class_name = fqcn_with_dollar.rsplit(".", 1)[1].replace("$", ".")
        else:
            package = ""
            class_name = fqcn_with_dollar.replace("$", ".")
    return package, class_name


def _save_current_class(
    symbols_by_package: dict[str, dict[str, SymbolData]],
    package: str | None,
    class_name: str | None,
    class_line: int | None,
    inherits: list[str],
    methods: dict[str, SymbolData],
    visibility: str,
) -> None:
    """Save a completed class into the symbols dict."""
    if class_name and package is not None:
        symbols_by_package[package][class_name] = SymbolData(
            name=class_name,
            kind="class",
            line=class_line,
            inherits=inherits,
            children=methods,
            visibility=visibility,
        )


def _nest_inner_classes(symbols_by_package: dict[str, dict[str, SymbolData]]) -> None:
    """Move inner classes (Foo.Bar) to be children of their parent classes."""
    for symbols in symbols_by_package.values():
        inner_classes = {
            name: sym
            for name, sym in symbols.items()
            if "." in name and sym.kind == "class"
        }

        for inner_name, inner_symbol in inner_classes.items():
            parent_name = inner_name.rsplit(".", 1)[0]
            if parent_name in symbols:
                symbols[parent_name].children[inner_name] = inner_symbol

        for inner_name in inner_classes:
            del symbols[inner_name]


# Compiled regex patterns for javap -v parsing
_CLASSFILE_RE = re.compile(r"^Classfile (.+)$")
_THIS_CLASS_RE = re.compile(r"^\s+this_class:\s+#\d+\s+//\s+(.+)$")
_SUPER_CLASS_RE = re.compile(r"^\s+super_class:\s+#\d+\s+//\s+(.+)$")
_METHOD_DECL_RE = re.compile(
    r"^\s+((?:public |protected |private |static |final |synchronized |native |abstract )+)?([^(]+)\(([^)]*)\);$"
)
_FLAGS_RE = re.compile(r"^\s+flags:\s+\(0x[0-9a-f]+\)\s+(.+)$")
_LINE_NUMBER_RE = re.compile(r"^\s+line\s+(\d+):\s+\d+$")


def _extract_symbols_from_bytecode(
    javap_path: str, classes_dir: Path
) -> dict[str, dict[str, SymbolData]]:
    """Extract class and method declarations from bytecode using javap -v -l.

    Returns: {package: {class_name: SymbolData}}
    """
    class_files = sorted(classes_dir.rglob("*.class"))
    if not class_files:
        return {}

    result = subprocess.run(
        [javap_path, "-v", "-l", "-p", *(str(f) for f in class_files)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning("javap -v -l failed: %s", result.stderr)
        return {}

    # package -> {class_name -> SymbolData}
    symbols_by_package: dict[str, dict[str, SymbolData]] = defaultdict(dict)

    current_classfile = None
    current_package = None
    current_class_name = None
    current_class_visibility = "package"
    current_class_flags_pending = None
    current_class_line = None
    current_inherits = []
    current_methods = {}

    current_method_name = None
    current_method_visibility = "package"
    current_method_line = None
    in_line_number_table = False

    for line in result.stdout.splitlines():
        # New class file
        cfm = _CLASSFILE_RE.match(line)
        if cfm:
            _save_current_class(
                symbols_by_package,
                current_package,
                current_class_name,
                current_class_line,
                current_inherits,
                current_methods,
                current_class_visibility,
            )

            # Reset for new class
            current_classfile = Path(cfm.group(1))
            current_class_line = None
            current_class_flags_pending = None
            current_inherits = []
            current_methods = {}
            current_method_name = None
            in_line_number_table = False
            continue

        # Extract full class name
        tcm = _THIS_CLASS_RE.match(line)
        if tcm:
            fqcn_with_dollar = tcm.group(1).replace("/", ".")
            if current_classfile:
                current_package, current_class_name = _resolve_class_identity(
                    current_classfile, classes_dir, fqcn_with_dollar
                )

            if current_class_flags_pending:
                current_class_visibility = _visibility_from_flags(
                    current_class_flags_pending
                )
                current_class_flags_pending = None
            continue

        # Extract superclass
        scm = _SUPER_CLASS_RE.match(line)
        if scm:
            super_class = scm.group(1).split("/")[-1]
            if super_class != "Object":
                current_inherits.append(super_class)
            continue

        # Extract class-level flags for visibility
        if (
            not current_class_name
            and not current_methods
            and "flags:" in line
            and current_classfile
        ):
            fm = _FLAGS_RE.match(line)
            if fm:
                current_class_flags_pending = fm.group(1)
            continue

        # Method declaration
        mdm = _METHOD_DECL_RE.match(line)
        if mdm and current_class_name:
            current_method_name = None

            modifiers = mdm.group(1).strip() if mdm.group(1) else ""
            method_name_part = mdm.group(2).strip().split()[-1]
            params_str = mdm.group(3).strip()

            if "." in method_name_part:
                method_name_part = method_name_part.split(".")[-1]

            if params_str:
                params = [p.strip().split(".")[-1] for p in params_str.split(",")]
                method_sig = f"{method_name_part}({','.join(params)})"
            else:
                method_sig = f"{method_name_part}()"

            current_method_visibility = _visibility_from_modifiers(modifiers)
            current_method_name = method_sig
            current_method_line = None
            in_line_number_table = False
            continue

        # Check method flags for ACC_BRIDGE (compiler-generated bridge methods)
        if current_method_name and not in_line_number_table and "flags:" in line:
            fm = _FLAGS_RE.match(line)
            if fm and "ACC_BRIDGE" in fm.group(1):
                current_method_name = None
                continue

        # LineNumberTable section
        if "LineNumberTable:" in line:
            in_line_number_table = True
            continue

        # Extract line number (first line of method)
        if in_line_number_table and current_method_name:
            lnm = _LINE_NUMBER_RE.match(line)
            if lnm and current_method_line is None:
                current_method_line = int(lnm.group(1))
                current_methods[current_method_name] = SymbolData(
                    name=current_method_name,
                    kind="method",
                    line=current_method_line,
                    calls=[],
                    visibility=current_method_visibility,
                )
                current_method_name = None
                in_line_number_table = False
            continue

    # Save last class
    _save_current_class(
        symbols_by_package,
        current_package,
        current_class_name,
        current_class_line,
        current_inherits,
        current_methods,
        current_class_visibility,
    )

    _nest_inner_classes(symbols_by_package)

    logger.debug(
        "Extracted symbols from bytecode for %d packages", len(symbols_by_package)
    )
    return symbols_by_package


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

    try:
        param_part = jvm_sig[1 : jvm_sig.index(")")]
    except ValueError:
        return ""  # Malformed signature: missing closing paren

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
            try:
                end = param_part.index(";", i)
            except ValueError:
                break  # Malformed signature: missing semicolon
            fqcn = param_part[i + 1 : end]
            # Take just the class name (last part after /)
            class_name = fqcn.split("/")[-1]
            params.append(class_name + "[]" * array_depth)
            i = end + 1
        elif c in "BCDFIJSZ":
            # Primitive type
            primitives = {
                "B": "byte",
                "C": "char",
                "D": "double",
                "F": "float",
                "I": "int",
                "J": "long",
                "S": "short",
                "Z": "boolean",
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

    class_pattern = re.compile(
        r"^(?:public |protected |private )?(?:static |final |abstract )*(?:class|interface|enum) ([^\s<{]+)"
    )
    method_pattern = re.compile(
        r"^\s+(?:public |protected |private |static |final |synchronized |native |abstract )*([^(]+)\(([^)]*)\);$"
    )
    invoke_pattern = re.compile(
        r"invoke(?:virtual|special|static|interface)\s+#\d+\s+//.*Method ([^:]+):(.+)"
    )

    # package -> class -> method_sig -> [called_sigs]
    calls_data: dict[str, dict[str, dict[str, list[str]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )

    current_package = ""
    current_class = ""
    current_method = ""
    # Track methods we've already seen to avoid collecting calls from bridge methods
    seen_methods: set[tuple[str, str, str]] = set()

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
                params = [
                    p.strip().split(".")[-1].split("[]")[0]
                    + ("[]" if "[]" in p else "")
                    for p in params_str.split(",")
                ]
                method_sig = f"{method_name}({','.join(params)})"
            else:
                method_sig = f"{method_name}()"

            # Check if we've already seen this method (to skip bridge methods)
            method_key = (current_package, current_class, method_sig)
            if method_key in seen_methods:
                # Skip duplicate method (likely a bridge method)
                current_method = ""
            else:
                seen_methods.add(method_key)
                current_method = method_sig
            continue

        # Extract method invocations
        im = invoke_pattern.search(line)
        if im and current_class and current_method:
            called_method_name = im.group(1).split("/")[-1].replace("$", ".")
            called_jvm_sig = im.group(2)

            # Convert JVM signature to Java parameter list
            params = _jvm_sig_to_java(called_jvm_sig)
            called_sig = f"{called_method_name}({params})"

            calls_data[current_package][current_class][current_method].append(
                called_sig
            )

    logger.debug(
        "Extracted method calls from bytecode for %d packages", len(calls_data)
    )
    return calls_data


def _merge_method_calls(
    graph: ProjectGraph, calls_data: dict[str, dict[str, dict[str, list[str]]]]
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
