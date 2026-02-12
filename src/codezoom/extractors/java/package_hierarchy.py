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

        from codezoom.extractors.java import _find_classes_dir

        classes_dir = _find_classes_dir(project_dir)
        if classes_dir is None:
            logger.warning(
                "Compiled classes not found — run `mvn compile` or "
                "`gradle build` first. Skipping package hierarchy."
            )
            return

        edges = _run_jdeps(jdeps_path, classes_dir)
        if edges is None:
            return

        _build_hierarchical_data(edges, classes_dir, graph)

        javap_path = shutil.which("javap")
        if javap_path:
            class_deps = _scan_class_deps(javap_path, classes_dir)
            for pkg, deps in class_deps.items():
                if pkg in graph.hierarchy:
                    graph.hierarchy[pkg].class_deps = {
                        k: sorted(v) for k, v in deps.items()
                    }
        else:
            logger.warning("javap not found — skipping class-level deps")


def _run_jdeps(jdeps_path: str, classes_dir: Path) -> list[tuple[str, str]] | None:
    """Run jdeps and return a list of (source_pkg, target_pkg) edges."""
    result = subprocess.run(
        [jdeps_path, "-verbose:package", str(classes_dir)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning("jdeps failed: %s", result.stderr)
        return None

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
    # We only keep edges where the target location is "classes" (project-internal).
    oneline_pattern = re.compile(r"^\s+(\S+)\s+->\s+(\S+)\s+(\S+)")
    source_pattern = re.compile(r"^\s{3}(\S+)\s+\(")
    arrow_pattern = re.compile(r"^\s+->\s+(\S+)\s+(.*)")
    classes_label = classes_dir.name  # typically "classes"

    edges: list[tuple[str, str]] = []
    current_source: str | None = None

    for line in result.stdout.splitlines():
        # Try one-line format first (JDK 11+)
        om = oneline_pattern.match(line)
        if om:
            src, tgt, location = om.group(1), om.group(2), om.group(3)
            if location == classes_label:
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
            if location == classes_label:
                edges.append((current_source, tgt))

    all_pkgs = {s for s, _ in edges} | {t for _, t in edges}
    logger.debug(
        "jdeps: %d internal edges across %d packages",
        len(edges),
        len(all_pkgs),
    )

    return edges


def _scan_class_deps(
    javap_path: str, classes_dir: Path
) -> dict[str, dict[str, set[str]]]:
    """Parse constant-pool ``Class`` entries via ``javap -v`` for class deps.

    Returns ``{package: {source_class: {target_node_ids}}}`` where each
    target is either a plain class name (same-package class→class edge)
    or a full package name (cross-package class→package edge).

    Unlike ``jdeps``, this reliably finds *all* class references in bytecode,
    including those in interface default methods.
    """
    class_files = sorted(classes_dir.rglob("*.class"))
    if not class_files:
        return {}

    # Determine the set of project-internal FQCNs (using / separator).
    project_fqcns: set[str] = set()
    for cf in class_files:
        rel = cf.relative_to(classes_dir).with_suffix("")
        project_fqcns.add(str(rel))  # e.g. "org/apposed/appose/Service"

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
            # Extract FQCN from the file path
            path = Path(cm.group(1))
            try:
                current_fqcn = str(path.relative_to(classes_dir).with_suffix(""))
            except ValueError:
                current_fqcn = None
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


def _build_hierarchical_data(
    edges: list[tuple[str, str]],
    classes_dir: Path,
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
    graph.root_node_id = root_id

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
