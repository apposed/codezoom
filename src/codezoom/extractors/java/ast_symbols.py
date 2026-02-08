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
        logger.debug("Java bytecode: %d packages, %d symbols", len(symbols_by_package), symbol_count)

        # Extract method calls from bytecode
        method_calls = _extract_method_calls_from_bytecode(javap_path, classes_dir)
        _merge_method_calls(graph, method_calls)


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

    # Parse javap -v output
    # Format:
    #   Classfile /path/to/Foo.class
    #     flags: (0x0021) ACC_PUBLIC, ACC_SUPER
    #     this_class: #X                         // pkg/Foo
    #     super_class: #Y                        // java/lang/Object
    #     interfaces: 1, fields: 2, methods: 3, attributes: 1
    #   ...
    #   public void method(int);
    #     descriptor: (I)V
    #     flags: (0x0001) ACC_PUBLIC
    #     Code:
    #       ...
    #     LineNumberTable:
    #       line 10: 0

    classfile_pattern = re.compile(r"^Classfile (.+)$")
    this_class_pattern = re.compile(r"^\s+this_class:\s+#\d+\s+//\s+(.+)$")
    super_class_pattern = re.compile(r"^\s+super_class:\s+#\d+\s+//\s+(.+)$")
    interface_pattern = re.compile(r"^\s+#\d+ = Class\s+#\d+\s+//\s+(.+)$")
    method_decl_pattern = re.compile(r"^\s+(public |protected |private |static |final |synchronized |native |abstract |)+([^(]+)\(([^)]*)\);$")
    descriptor_pattern = re.compile(r"^\s+descriptor:\s+(.+)$")
    flags_pattern = re.compile(r"^\s+flags:\s+\(0x[0-9a-f]+\)\s+(.+)$")
    line_number_pattern = re.compile(r"^\s+line\s+(\d+):\s+\d+$")

    # package -> {class_name -> SymbolData}
    symbols_by_package: dict[str, dict[str, SymbolData]] = defaultdict(dict)

    current_classfile = None
    current_fqcn = None
    current_package = None
    current_class_name = None
    current_class_visibility = "package"
    current_class_line = None
    current_inherits = []
    current_methods = {}

    current_method_name = None
    current_method_visibility = "package"
    current_method_line = None
    in_line_number_table = False

    for line in result.stdout.splitlines():
        # New class file
        cfm = classfile_pattern.match(line)
        if cfm:
            # Save previous class if any
            if current_class_name and current_package is not None:
                symbols_by_package[current_package][current_class_name] = SymbolData(
                    name=current_class_name,
                    kind="class",
                    line=current_class_line,
                    inherits=current_inherits,
                    children=current_methods,
                    visibility=current_class_visibility,
                )

            # Reset for new class
            current_classfile = Path(cfm.group(1))
            current_class_line = None
            current_inherits = []
            current_methods = {}
            current_method_name = None
            in_line_number_table = False
            continue

        # Extract full class name
        tcm = this_class_pattern.match(line)
        if tcm:
            # Get the fully qualified class name with $ for inner classes
            fqcn_with_dollar = tcm.group(1).replace("/", ".")

            # Inner classes use $ separator (e.g., pkg.Outer$Inner)
            # We need to find the package part (before the top-level class)
            # by looking at the directory structure
            parts = fqcn_with_dollar.split(".")

            # The package is everything except the last part(s) that form the class name
            # Class name can have $ for inner classes (e.g., "Outer$Inner")
            # Find where the class name starts (after the last directory separator in the file path)
            if current_classfile:
                # Extract package from file path
                try:
                    rel_path = current_classfile.relative_to(classes_dir)
                    package_path = rel_path.parent
                    current_package = str(package_path).replace("/", ".").replace("\\", ".")
                    if current_package == ".":
                        current_package = ""

                    # Class name is the file name without .class, with $ converted to .
                    class_file_name = current_classfile.stem  # removes .class
                    current_class_name = class_file_name.replace("$", ".")
                except ValueError:
                    # Fallback if relative path fails
                    if "." in fqcn_with_dollar:
                        current_package = fqcn_with_dollar.rsplit(".", 1)[0]
                        current_class_name = fqcn_with_dollar.rsplit(".", 1)[1].replace("$", ".")
                    else:
                        current_package = ""
                        current_class_name = fqcn_with_dollar.replace("$", ".")
            continue

        # Extract superclass
        scm = super_class_pattern.match(line)
        if scm:
            super_class = scm.group(1).split("/")[-1]
            if super_class != "Object":  # Skip java.lang.Object
                current_inherits.append(super_class)
            continue

        # Extract class-level flags for visibility
        if current_class_name and not current_methods and "flags:" in line:
            fm = flags_pattern.match(line)
            if fm:
                flags = fm.group(1)
                if "ACC_PUBLIC" in flags:
                    current_class_visibility = "public"
                elif "ACC_PROTECTED" in flags:
                    current_class_visibility = "protected"
                elif "ACC_PRIVATE" in flags:
                    current_class_visibility = "private"
                else:
                    current_class_visibility = "package"
            continue

        # Method declaration
        mdm = method_decl_pattern.match(line)
        if mdm and current_class_name:
            modifiers = mdm.group(1).strip() if mdm.group(1) else ""
            method_name_part = mdm.group(2).strip().split()[-1]  # Last word is method name
            params_str = mdm.group(3).strip()

            # Build method signature
            if params_str:
                params = [p.strip().split(".")[-1] for p in params_str.split(",")]
                current_method_name = f"{method_name_part}({','.join(params)})"
            else:
                current_method_name = f"{method_name_part}()"

            # Determine visibility from modifiers
            if "public" in modifiers:
                current_method_visibility = "public"
            elif "protected" in modifiers:
                current_method_visibility = "protected"
            elif "private" in modifiers:
                current_method_visibility = "private"
            else:
                current_method_visibility = "package"

            current_method_line = None
            in_line_number_table = False
            continue

        # LineNumberTable section
        if "LineNumberTable:" in line:
            in_line_number_table = True
            continue

        # Extract line number (first line of method)
        if in_line_number_table and current_method_name:
            lnm = line_number_pattern.match(line)
            if lnm and current_method_line is None:
                current_method_line = int(lnm.group(1))
                # Save method
                current_methods[current_method_name] = SymbolData(
                    name=current_method_name,
                    kind="method",
                    line=current_method_line,
                    calls=[],  # Will be populated later
                    visibility=current_method_visibility,
                )
                current_method_name = None
                in_line_number_table = False
            continue

    # Save last class
    if current_class_name and current_package is not None:
        symbols_by_package[current_package][current_class_name] = SymbolData(
            name=current_class_name,
            kind="class",
            line=current_class_line,
            inherits=current_inherits,
            children=current_methods,
            visibility=current_class_visibility,
        )

    # Post-process: nest inner classes as children of their parent classes
    for package_name, symbols in symbols_by_package.items():
        # Find inner classes (those with . in the name)
        inner_classes = {name: sym for name, sym in symbols.items() if "." in name and sym.kind == "class"}

        for inner_name, inner_symbol in inner_classes.items():
            # Find parent class name (everything before the last .)
            parent_name = inner_name.rsplit(".", 1)[0]

            if parent_name in symbols:
                # Move inner class to be a child of parent
                symbols[parent_name].children[inner_name] = inner_symbol

        # Remove inner classes from top level
        for inner_name in inner_classes:
            del symbols[inner_name]

    logger.debug("Extracted symbols from bytecode for %d packages", len(symbols_by_package))
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
