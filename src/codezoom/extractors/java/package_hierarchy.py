"""Extract Java package hierarchy via jdeps."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

from codezoom.model import NodeData, ProjectGraph

logger = logging.getLogger(__name__)


class JavaPackageHierarchyExtractor:
    """Populate hierarchy with Java package tree and inter-package imports."""

    def can_handle(self, project_dir: Path) -> bool:
        return (
            (project_dir / "pom.xml").exists()
            or (project_dir / "build.gradle").exists()
            or (project_dir / "build.gradle.kts").exists()
        )

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        jdeps_path = shutil.which("jdeps")
        if not jdeps_path:
            logger.warning(
                "jdeps not found on PATH — skipping package hierarchy. "
                "Ensure a JDK is installed."
            )
            return

        from codezoom.extractors.java import _find_module_classes

        module_classes = _find_module_classes(project_dir)
        if not module_classes:
            logger.warning(
                "Compiled classes not found — run `mvn compile` or "
                "`gradle build` first. Skipping package hierarchy."
            )
            return

        all_classes_dirs = [d for dirs in module_classes.values() for d in dirs]
        is_multi_module = not (len(module_classes) == 1 and "" in module_classes)

        if is_multi_module:
            _build_multi_module_hierarchy(
                jdeps_path, module_classes, project_dir, graph
            )
        else:
            edges = _run_jdeps(jdeps_path, all_classes_dirs)
            _build_hierarchical_data(edges, all_classes_dirs, graph)

        javap_path = shutil.which("javap")
        if javap_path:
            class_deps = _scan_class_deps(javap_path, all_classes_dirs)

            # Derive package-level import edges from javap cross-package refs
            # and merge into hierarchy (fills gaps when jdeps is unhelpful).
            _merge_javap_imports(class_deps, graph)

            for pkg, deps in class_deps.items():
                if pkg in graph.hierarchy:
                    graph.hierarchy[pkg].class_deps = {
                        k: sorted(v) for k, v in deps.items()
                    }
        else:
            logger.warning("javap not found — skipping class-level deps")

        # For multi-module projects, re-compute module-level imports after
        # javap merge may have added new package edges.
        if is_multi_module:
            _recompute_module_imports(module_classes, graph)


def _run_jdeps(
    jdeps_path: str,
    classes_dirs: list[Path],
    module_path: list[Path] | None = None,
) -> list[tuple[str, str]]:
    """Run jdeps per-dir and return merged (source_pkg, target_pkg) edges.

    Runs jdeps separately for each classes dir to isolate JPMS failures
    (e.g. unsatisfied ``requires``) to individual modules.  Dirs that fail
    are logged and skipped; if all fail, an empty list is returned.

    If *module_path* is given, it is passed via ``--module-path`` so that
    jdeps can resolve JPMS ``requires`` directives (sibling modules and
    external dependency JARs).
    """
    # jdeps output format varies by JDK version:
    #
    # JDK 8 (grouped):
    #   <source_pkg>                          (<location>)
    #      -> <target_pkg>                    <location>
    #
    # JDK 11+ (one-line):
    #   <source_pkg>                          -> <target_pkg>     <location>
    #
    # We handle both with a combined regex plus the grouped fallback.
    # We only keep edges where the target is internal to the project.
    oneline_pattern = re.compile(r"^\s+(\S+)\s+->\s+(\S+)\s+(\S+)")
    source_pattern = re.compile(r"^\s{3}(\S+)\s+\(")
    arrow_pattern = re.compile(r"^\s+->\s+(\S+)\s+(.*)")

    # Build the set used to decide whether an edge target is internal.
    # Without module-path: match on the directory basename (typically "classes").
    # With module-path: jdeps reports JPMS module names as the location,
    # so we match on internal packages discovered from the filesystem.
    if module_path:
        internal_packages: set[str] = set()
        for classes_dir in classes_dirs:
            for class_file in classes_dir.rglob("*.class"):
                pkg_path = class_file.relative_to(classes_dir).parent
                if pkg_path != Path("."):
                    internal_packages.add(
                        str(pkg_path).replace("/", ".").replace("\\", ".")
                    )

        def _is_internal(_location: str, tgt_pkg: str) -> bool:
            return tgt_pkg in internal_packages
    else:
        classes_labels = {d.name for d in classes_dirs}  # typically {"classes"}

        def _is_internal(_location: str, _tgt_pkg: str) -> bool:
            return _location in classes_labels

    edges: list[tuple[str, str]] = []

    # Build --module-path argument if provided.
    mp_args: list[str] = []
    if module_path:
        mp_str = ":".join(str(p) for p in module_path)
        mp_args = ["--module-path", mp_str, "--multi-release", "base"]

    for classes_dir in classes_dirs:
        result = subprocess.run(
            [jdeps_path, *mp_args, "-verbose:package", str(classes_dir)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            msg = stderr or stdout
            logger.debug("jdeps failed for %s: %s", classes_dir, msg)
            continue

        current_source: str | None = None
        for line in result.stdout.splitlines():
            # Try one-line format first (JDK 11+)
            om = oneline_pattern.match(line)
            if om:
                src, tgt, location = om.group(1), om.group(2), om.group(3)
                if _is_internal(location, tgt):
                    edges.append((src, tgt))
                continue
            # Grouped format (JDK 8)
            sm = source_pattern.match(line)
            if sm:
                current_source = sm.group(1)
                continue
            am = arrow_pattern.match(line)
            if am and current_source is not None:
                tgt = am.group(1)
                location = am.group(2).strip()
                if _is_internal(location, tgt):
                    edges.append((current_source, tgt))

    all_pkgs = {s for s, _ in edges} | {t for _, t in edges}
    logger.debug(
        "jdeps: %d internal edges across %d packages",
        len(edges),
        len(all_pkgs),
    )

    return edges


def _scan_class_deps(
    javap_path: str, classes_dirs: list[Path]
) -> dict[str, dict[str, set[str]]]:
    """Parse constant-pool ``Class`` entries via ``javap -v`` for class deps.

    Returns ``{package: {source_class: {target_node_ids}}}`` where each
    target is either a plain class name (same-package class→class edge)
    or a full package name (cross-package class→package edge).

    Unlike ``jdeps``, this reliably finds *all* class references in bytecode,
    including those in interface default methods.
    """
    class_files: list[Path] = []
    for classes_dir in classes_dirs:
        class_files.extend(sorted(classes_dir.rglob("*.class")))
    if not class_files:
        return {}

    # Determine the set of project-internal FQCNs (using / separator).
    project_fqcns: set[str] = set()
    for cf in class_files:
        for classes_dir in classes_dirs:
            try:
                rel = cf.relative_to(classes_dir).with_suffix("")
                project_fqcns.add(str(rel))  # e.g. "org/apposed/appose/Service"
                break
            except ValueError:
                continue

    result = subprocess.run(
        [javap_path, "-v", *(str(f) for f in class_files)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning("javap failed: %s", result.stderr)
        return {}

    # Parse output.  We track which class file we're in via "Classfile"
    # lines, and extract Class constant-pool entries.
    classfile_pattern = re.compile(r"^Classfile (.+)$")
    class_entry_pattern = re.compile(r"^\s+#\d+ = Class\s+#\d+\s+// (.+)$")

    # package -> {src_class: {target_node_ids}}
    pkg_class_deps: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )

    current_fqcn: str | None = None  # slash-separated, e.g. "org/apposed/appose/Appose"
    edge_count = 0

    for line in result.stdout.splitlines():
        cm = classfile_pattern.match(line)
        if cm:
            # Extract FQCN from the file path — try each classes_dir
            path = Path(cm.group(1))
            current_fqcn = None
            for classes_dir in classes_dirs:
                try:
                    current_fqcn = str(path.relative_to(classes_dir).with_suffix(""))
                    break
                except ValueError:
                    continue
            continue

        em = class_entry_pattern.match(line)
        if em and current_fqcn is not None:
            tgt_internal = em.group(1)  # e.g. "org/apposed/appose/Service"

            # Only keep project-internal references (ignore JDK, libs).
            # Inner classes (Foo$Bar) map to top-level (Foo) which must
            # be in project_fqcns after stripping.
            tgt_top = tgt_internal.split("$")[0]
            if tgt_top not in project_fqcns:
                continue

            # Split into package and class name (using / separator).
            if "/" not in current_fqcn or "/" not in tgt_internal:
                continue
            src_pkg = current_fqcn.rsplit("/", 1)[0].replace("/", ".")
            src_class = current_fqcn.rsplit("/", 1)[1].split("$")[0]
            tgt_pkg = tgt_internal.rsplit("/", 1)[0].replace("/", ".")
            tgt_class = tgt_internal.rsplit("/", 1)[1].split("$")[0]

            if src_pkg == tgt_pkg:
                # Same-package: class→class edge (skip self-edges)
                if src_class != tgt_class:
                    pkg_class_deps[src_pkg][src_class].add(tgt_class)
                    edge_count += 1
            else:
                # Cross-package: class→package edge
                pkg_class_deps[src_pkg][src_class].add(tgt_pkg)
                edge_count += 1

    logger.debug(
        "javap class-level: %d edges across %d packages",
        edge_count,
        len(pkg_class_deps),
    )

    return pkg_class_deps


def _merge_javap_imports(
    class_deps: dict[str, dict[str, set[str]]],
    graph: ProjectGraph,
) -> None:
    """Merge cross-package edges from javap into the hierarchy.

    ``class_deps`` maps ``{src_pkg: {src_class: {target_id, ...}}}``.
    A target that exists as a key in ``graph.hierarchy`` is a package
    (cross-package edge); otherwise it's a class name (same-package).

    This fills in ``imports_to`` / ``imports_from`` edges that jdeps may
    have missed (e.g. on JPMS-modular projects where jdeps outputs
    module-level info instead of package-level edges).
    """
    new_edges: set[tuple[str, str]] = set()
    for src_pkg, deps in class_deps.items():
        for targets in deps.values():
            for tgt in targets:
                if tgt in graph.hierarchy and tgt != src_pkg:
                    new_edges.add((src_pkg, tgt))

    if not new_edges:
        return

    added = 0
    for src_pkg, tgt_pkg in new_edges:
        src_node = graph.hierarchy.get(src_pkg)
        tgt_node = graph.hierarchy.get(tgt_pkg)
        if src_node is None or tgt_node is None:
            continue
        if tgt_pkg not in src_node.imports_to:
            src_node.imports_to = sorted(set(src_node.imports_to) | {tgt_pkg})
            added += 1
        if src_pkg not in tgt_node.imports_from:
            tgt_node.imports_from = sorted(set(tgt_node.imports_from) | {src_pkg})

    if added:
        logger.debug("javap-derived imports: %d new package edges", added)

        # Re-aggregate imports bottom-up for parent nodes.
        if not graph.root_node_ids:
            return

        order: list[str] = []
        stack = list(graph.root_node_ids)
        visited: set[str] = set()
        while stack:
            node_id = stack[-1]
            node = graph.hierarchy.get(node_id)
            children = node.children if node else []
            unvisited = [c for c in children if c not in visited]
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
                    imp for imp in all_to if not imp.startswith(node_id)
                )
                node.imports_from = sorted(all_from)


def _recompute_module_imports(
    module_classes: dict[str, list[Path]],
    graph: ProjectGraph,
) -> None:
    """Re-derive module→module imports from package-level edges.

    Called after _merge_javap_imports to ensure module nodes reflect
    the latest package-level edges as inter-module dependencies.
    """
    module_names = sorted(module_classes.keys())
    module_set = set(module_names)

    # Build pkg→module mapping.
    pkg_to_module: dict[str, str] = {}
    for module_name, dirs in module_classes.items():
        for classes_dir in dirs:
            for class_file in classes_dir.rglob("*.class"):
                pkg_path = class_file.relative_to(classes_dir).parent
                if pkg_path != Path("."):
                    pkg = str(pkg_path).replace("/", ".").replace("\\", ".")
                    pkg_to_module[pkg] = module_name

    for module_name in module_names:
        mod_node = graph.hierarchy.get(module_name)
        if mod_node is None:
            continue
        mod_pkgs = _collect_descendant_packages(module_name, graph, module_set)
        target_modules: set[str] = set()
        source_modules: set[str] = set()
        for pkg_id in mod_pkgs:
            pkg_node = graph.hierarchy.get(pkg_id)
            if pkg_node is None:
                continue
            for imp in pkg_node.imports_to:
                tgt_mod = pkg_to_module.get(imp)
                if tgt_mod and tgt_mod != module_name and tgt_mod in module_set:
                    target_modules.add(tgt_mod)
            for imp in pkg_node.imports_from:
                src_mod = pkg_to_module.get(imp)
                if src_mod and src_mod != module_name and src_mod in module_set:
                    source_modules.add(src_mod)
        mod_node.imports_to = sorted(target_modules)
        mod_node.imports_from = sorted(source_modules)


def _collect_descendant_packages(
    module_name: str, graph: ProjectGraph, module_set: set[str]
) -> set[str]:
    """Collect all package node IDs under a module node (excluding module nodes)."""
    result: set[str] = set()
    stack = list(graph.hierarchy[module_name].children)
    while stack:
        node_id = stack.pop()
        if node_id in module_set:
            continue
        result.add(node_id)
        node = graph.hierarchy.get(node_id)
        if node:
            stack.extend(node.children)
    return result


def _resolve_dependency_jars(project_dir: Path, modules: list[str]) -> list[Path]:
    """Resolve external dependency JARs for a multi-module Maven project.

    Uses jgo to resolve each module's dependencies and returns a
    deduplicated list of JAR paths from the local Maven repository.
    """
    try:
        from jgo.maven import POM, MavenContext, Model
    except ImportError:
        logger.debug("jgo not installed — cannot resolve dependency JARs")
        return []

    jar_paths: set[Path] = set()
    ctx = MavenContext()

    for module in modules:
        pom_path = project_dir / module / "pom.xml"
        if not pom_path.exists():
            continue
        try:
            pom = POM(pom_path)
            model = Model(pom, ctx)
            deps, _ = model.dependencies()
        except (OSError, ValueError, KeyError, AttributeError) as e:
            logger.debug("Could not resolve deps for %s: %s", module, e)
            continue

        for dep in deps:
            if dep.scope not in (None, "compile", "runtime"):
                continue
            try:
                jar = dep.artifact.resolve()
                if jar.exists():
                    jar_paths.add(jar)
            except Exception as e:
                logger.debug(
                    "Could not resolve artifact %s:%s: %s",
                    dep.groupId,
                    dep.artifactId,
                    e,
                )

    logger.debug("Resolved %d dependency JARs for module-path", len(jar_paths))
    return sorted(jar_paths)


def _build_multi_module_hierarchy(
    jdeps_path: str,
    module_classes: dict[str, list[Path]],
    project_dir: Path,
    graph: ProjectGraph,
) -> None:
    """Build hierarchy with per-module subtrees for multi-module projects."""
    all_classes_dirs = [d for dirs in module_classes.values() for d in dirs]
    module_names = sorted(module_classes.keys())

    # Resolve dependency JARs so jdeps can satisfy JPMS requires directives.
    dep_jars = _resolve_dependency_jars(project_dir, module_names)
    module_path = all_classes_dirs + dep_jars

    # Run jdeps once with ALL classes dirs to capture cross-module edges.
    all_edges = _run_jdeps(jdeps_path, all_classes_dirs, module_path=module_path)

    # Build a mapping from package -> module name for edge assignment.
    pkg_to_module: dict[str, str] = {}
    for module_name, dirs in module_classes.items():
        for classes_dir in dirs:
            for class_file in classes_dir.rglob("*.class"):
                pkg_path = class_file.relative_to(classes_dir).parent
                if pkg_path != Path("."):
                    pkg = str(pkg_path).replace("/", ".").replace("\\", ".")
                    pkg_to_module[pkg] = module_name

    # Also assign packages from jdeps edges to modules.
    for src, tgt in all_edges:
        if src not in pkg_to_module:
            # Try to find which module owns this package via filesystem.
            for module_name, dirs in module_classes.items():
                for classes_dir in dirs:
                    pkg_as_path = Path(src.replace(".", "/"))
                    if (classes_dir / pkg_as_path).is_dir():
                        pkg_to_module[src] = module_name
                        break
        if tgt not in pkg_to_module:
            for module_name, dirs in module_classes.items():
                for classes_dir in dirs:
                    pkg_as_path = Path(tgt.replace(".", "/"))
                    if (classes_dir / pkg_as_path).is_dir():
                        pkg_to_module[tgt] = module_name
                        break

    # Each module is a top-level root — no single wrapper node.
    module_names = sorted(module_classes.keys())
    graph.root_node_ids = module_names

    for module_name in module_names:
        dirs = module_classes[module_name]

        # Collect packages for this module from filesystem.
        module_packages: set[str] = set()
        for classes_dir in dirs:
            for class_file in classes_dir.rglob("*.class"):
                pkg_path = class_file.relative_to(classes_dir).parent
                if pkg_path != Path("."):
                    module_packages.add(
                        str(pkg_path).replace("/", ".").replace("\\", ".")
                    )

        # Collect edges where source belongs to this module.
        module_edges: list[tuple[str, str]] = []
        for src, tgt in all_edges:
            if pkg_to_module.get(src) == module_name:
                module_edges.append((src, tgt))

        if not module_packages:
            graph.hierarchy[module_name] = NodeData()
            continue

        # Compute module-local root as longest common prefix of this module's packages.
        parts_list = [p.split(".") for p in sorted(module_packages)]
        common: list[str] = []
        for segments in zip(*parts_list):
            if len(set(segments)) == 1:
                common.append(segments[0])
            else:
                break

        module_root_pkg = ".".join(common) if common else sorted(module_packages)[0]

        # Build hierarchy for this module's packages.
        hierarchy: dict[str, dict[str, set]] = defaultdict(
            lambda: {"children": set(), "imports_from": set(), "imports_to": set()}
        )
        hierarchy[module_root_pkg] = {
            "children": set(),
            "imports_from": set(),
            "imports_to": set(),
        }

        for pkg in module_packages:
            parts = pkg.split(".")
            for i in range(len(common), len(parts)):
                parent = ".".join(parts[:i]) if i > 0 else module_root_pkg
                child = ".".join(parts[: i + 1])
                if parent != child:
                    hierarchy[parent]["children"].add(child)
                hierarchy[child]  # ensure exists

        # Record import edges (including cross-module edges).
        for src, tgt in module_edges:
            if src != tgt and src in hierarchy:
                hierarchy[src]["imports_to"].add(tgt)
            if tgt in hierarchy and src != tgt:
                hierarchy[tgt]["imports_from"].add(src)

        # Also record incoming edges from other modules.
        for src, tgt in all_edges:
            if (
                pkg_to_module.get(tgt) == module_name
                and pkg_to_module.get(src) != module_name
            ):
                if tgt in hierarchy:
                    hierarchy[tgt]["imports_from"].add(src)

        # Aggregate imports bottom-up within this module.
        order: list[str] = []
        stack = [module_root_pkg]
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
                node["imports_to"] = {
                    imp for imp in all_to if not imp.startswith(node_id)
                }
                node["imports_from"] = all_from

        # Write module's packages into graph.hierarchy.
        for node_id, raw in hierarchy.items():
            existing = graph.hierarchy.get(node_id)
            graph.hierarchy[node_id] = NodeData(
                children=sorted(raw["children"]),
                imports_to=sorted(raw["imports_to"]),
                imports_from=sorted(raw["imports_from"]),
                symbols=existing.symbols if existing else None,
            )

        # Create module node with the module root package as its child.
        # Package-level imports stay on the package nodes; module-level
        # imports are computed below after all modules are built.
        graph.hierarchy[module_name] = NodeData(
            children=[module_root_pkg],
        )

    # Compute module-level imports: translate cross-module package edges
    # into module→module edges so the top-level graph shows inter-module
    # dependencies.
    module_set = set(module_names)
    for module_name in module_names:
        mod_node = graph.hierarchy[module_name]
        # Gather all packages owned by this module.
        mod_pkgs = _collect_descendant_packages(module_name, graph, module_set)
        # Collect all cross-module package imports from this module's packages.
        target_modules: set[str] = set()
        source_modules: set[str] = set()
        for pkg_id in mod_pkgs:
            pkg_node = graph.hierarchy.get(pkg_id)
            if pkg_node is None:
                continue
            for imp in pkg_node.imports_to:
                tgt_mod = pkg_to_module.get(imp)
                if tgt_mod and tgt_mod != module_name and tgt_mod in module_set:
                    target_modules.add(tgt_mod)
            for imp in pkg_node.imports_from:
                src_mod = pkg_to_module.get(imp)
                if src_mod and src_mod != module_name and src_mod in module_set:
                    source_modules.add(src_mod)
        mod_node.imports_to = sorted(target_modules)
        mod_node.imports_from = sorted(source_modules)

    total_nodes = sum(1 for k in graph.hierarchy if k not in module_set)
    logger.debug(
        "multi-module hierarchy: %d modules, %d package nodes",
        len(module_names),
        total_nodes,
    )


def _build_hierarchical_data(
    edges: list[tuple[str, str]],
    classes_dirs: list[Path],
    graph: ProjectGraph,
) -> None:
    """Build hierarchy inside *graph* from jdeps package edges and filesystem."""
    # Collect all internal packages mentioned in edges.
    all_packages: set[str] = set()
    for src, tgt in edges:
        all_packages.add(src)
        all_packages.add(tgt)

    # Also discover packages from the filesystem — jdeps only reports packages
    # that have cross-package edges, missing leaf packages with no internal deps.
    for classes_dir in classes_dirs:
        for class_file in classes_dir.rglob("*.class"):
            pkg_path = class_file.relative_to(classes_dir).parent
            if pkg_path != Path("."):
                all_packages.add(str(pkg_path).replace("/", ".").replace("\\", "."))

    if not all_packages:
        return

    # Compute root_node_id as longest common package prefix.
    parts_list = [p.split(".") for p in sorted(all_packages)]
    common: list[str] = []
    for segments in zip(*parts_list):
        if len(set(segments)) == 1:
            common.append(segments[0])
        else:
            break

    root_id = ".".join(common) if common else sorted(all_packages)[0]
    graph.root_node_ids = [root_id]

    # Build raw hierarchy data.
    hierarchy: dict[str, dict[str, set]] = defaultdict(
        lambda: {"children": set(), "imports_from": set(), "imports_to": set()}
    )
    hierarchy[root_id] = {
        "children": set(),
        "imports_from": set(),
        "imports_to": set(),
    }

    # Register all packages and their parent→child relationships.
    for pkg in all_packages:
        parts = pkg.split(".")
        # Walk from root down to this package, creating intermediate nodes.
        for i in range(len(common), len(parts)):
            parent = ".".join(parts[:i]) if i > 0 else root_id
            child = ".".join(parts[: i + 1])
            if parent != child:
                hierarchy[parent]["children"].add(child)
            hierarchy[child]  # ensure exists

    # Record inter-package import edges.
    for src, tgt in edges:
        if src != tgt:
            hierarchy[src]["imports_to"].add(tgt)
            hierarchy[tgt]["imports_from"].add(src)

    # Aggregate imports bottom-up: process leaves first, then parents.
    # Build processing order via iterative post-order traversal.
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

    # Write into graph.hierarchy (preserving any existing symbols data).
    for node_id, raw in hierarchy.items():
        existing = graph.hierarchy.get(node_id)
        graph.hierarchy[node_id] = NodeData(
            children=sorted(raw["children"]),
            imports_to=sorted(raw["imports_to"]),
            imports_from=sorted(raw["imports_from"]),
            symbols=existing.symbols if existing else None,
        )

    logger.debug("hierarchy: root=%r, %d nodes", root_id, len(hierarchy))
    for nid in sorted(hierarchy):
        n = hierarchy[nid]
        logger.debug(
            "  %s: %d children, %d imports_to",
            nid,
            len(n["children"]),
            len(n["imports_to"]),
        )
