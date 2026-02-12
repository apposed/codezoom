"""Extract external dependencies from Gradle build files."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from codezoom.model import ExternalDep, ProjectGraph

logger = logging.getLogger(__name__)

# Dependency configurations we care about (compile/runtime scope).
_DEP_CONFIGS = {
    "implementation",
    "api",
    "shadow",
    "compileOnly",
    "runtimeOnly",
    "compileOnlyApi",
}

# Regex for string-literal dependencies: configuration("group:artifact:version")
_STRING_DEP_RE = re.compile(
    r"^\s*(" + "|".join(_DEP_CONFIGS) + r')\s*\(\s*"([^"]+)"\s*\)',
)

# Regex for version-catalog references: configuration(libs.foo.bar)
_CATALOG_DEP_RE = re.compile(
    r"^\s*(" + "|".join(_DEP_CONFIGS) + r")\s*\(\s*(libs\.[a-zA-Z0-9_.]+)\s*\)",
)

# Regex for version-catalog bundle references: configuration(libs.bundles.foo)
_BUNDLE_RE = re.compile(
    r"^\s*("
    + "|".join(_DEP_CONFIGS)
    + r")\s*\(\s*libs\.bundles\.([a-zA-Z0-9_.]+)\s*\)",
)


def _is_gradle_project(project_dir: Path) -> bool:
    return (project_dir / "build.gradle.kts").exists() or (
        project_dir / "build.gradle"
    ).exists()


def _find_build_file(project_dir: Path) -> Path | None:
    for name in ("build.gradle.kts", "build.gradle"):
        p = project_dir / name
        if p.exists():
            return p
    return None


def _parse_version_catalog(
    catalog_path: Path,
) -> tuple[
    dict[str, str],  # library alias -> "group:artifact:version"
    dict[str, list[str]],  # bundle name -> [library aliases]
]:
    """Parse a Gradle version catalog (libs.versions.toml).

    Returns (libraries, bundles) where libraries maps alias to coordinate
    and bundles maps bundle name to list of library aliases.
    """
    libraries: dict[str, str] = {}
    bundles: dict[str, list[str]] = {}
    versions: dict[str, str] = {}

    if not catalog_path.exists():
        return libraries, bundles

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    try:
        with open(catalog_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, Exception) as e:
        logger.debug("Could not parse version catalog %s: %s", catalog_path, e)
        return libraries, bundles

    # Parse [versions]
    for alias, ver in data.get("versions", {}).items():
        if isinstance(ver, str):
            versions[alias] = ver
        elif isinstance(ver, dict):
            versions[alias] = ver.get("ref", ver.get("version", ""))

    # Parse [libraries]
    for alias, spec in data.get("libraries", {}).items():
        if isinstance(spec, dict):
            module = spec.get("module", "")
            ver_ref = spec.get("version", {})
            if isinstance(ver_ref, dict):
                ver = versions.get(ver_ref.get("ref", ""), "")
            elif isinstance(ver_ref, str):
                ver = ver_ref
            else:
                ver = ""
            coord = f"{module}:{ver}" if ver else module
            libraries[alias] = coord

    # Parse [bundles]
    for bundle_name, members in data.get("bundles", {}).items():
        if isinstance(members, list):
            bundles[bundle_name] = members

    return libraries, bundles


def _resolve_catalog_ref(
    ref: str,
    libraries: dict[str, str],
    bundles: dict[str, list[str]],
) -> list[str]:
    """Resolve a libs.xxx reference to dependency coordinates.

    ref is like "libs.qupath.fxtras" or "libs.bundles.qupath".
    """
    # Strip "libs." prefix
    parts = ref.split(".", 1)
    if len(parts) < 2 or parts[0] != "libs":
        return [ref]

    remainder = parts[1]

    # Check if it's a bundle reference
    if remainder.startswith("bundles."):
        bundle_name = remainder[len("bundles.") :]
        # Normalize: Gradle catalogs use kebab-case in TOML but camelCase in code
        member_aliases = bundles.get(bundle_name, [])
        if not member_aliases:
            # Try with hyphens converted to dots
            for bn, members in bundles.items():
                if bn.replace("-", ".") == bundle_name or bn.replace(
                    "-", ""
                ) == bundle_name.replace(".", ""):
                    member_aliases = members
                    break

        coords = []
        for alias in member_aliases:
            if alias in libraries:
                coords.append(libraries[alias])
            else:
                coords.append(alias)
        return coords if coords else [ref]

    # It's a library reference — try direct match and common normalizations
    alias = remainder
    if alias in libraries:
        return [libraries[alias]]

    # Try with dots replaced by hyphens (Gradle accessor convention)
    for lib_alias, coord in libraries.items():
        normalized = lib_alias.replace("-", ".")
        if normalized == alias or lib_alias.replace("-", "") == alias.replace(".", ""):
            return [coord]

    return [ref]


class GradleDepsExtractor:
    """Populate external_deps from Gradle build files."""

    def can_handle(self, project_dir: Path) -> bool:
        return _is_gradle_project(project_dir)

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        build_file = _find_build_file(project_dir)
        if build_file is None:
            return

        try:
            content = build_file.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("Could not read %s: %s", build_file, e)
            return

        # Try to find and parse version catalog
        libraries: dict[str, str] = {}
        bundles: dict[str, list[str]] = {}

        # Check local project first, then common parent locations
        catalog_candidates = [
            project_dir / "gradle" / "libs.versions.toml",
        ]
        # Walk up to find a parent catalog (common in composite builds)
        parent = project_dir.parent
        for _ in range(3):
            catalog_candidates.append(parent / "gradle" / "libs.versions.toml")
            parent = parent.parent

        for catalog_path in catalog_candidates:
            if catalog_path.exists():
                libraries, bundles = _parse_version_catalog(catalog_path)
                logger.debug("Using version catalog: %s", catalog_path)
                break

        direct_names: set[str] = set()

        for line in content.splitlines():
            # Skip test dependencies
            stripped = line.strip()
            if stripped.startswith("testImplementation") or stripped.startswith(
                "testRuntimeOnly"
            ):
                continue

            # String literal deps: implementation("group:artifact:version")
            m = _STRING_DEP_RE.match(stripped)
            if m:
                coord = m.group(2)
                # Normalize: strip version for display, keep group:artifact
                parts = coord.split(":")
                if len(parts) >= 2:
                    name = f"{parts[0]}:{parts[1]}"
                    direct_names.add(name)
                continue

            # Bundle references: shadow(libs.bundles.qupath)
            bm = _BUNDLE_RE.match(stripped)
            if bm:
                ref = f"libs.bundles.{bm.group(2)}"
                resolved = _resolve_catalog_ref(ref, libraries, bundles)
                for coord in resolved:
                    parts = coord.split(":")
                    if len(parts) >= 2:
                        direct_names.add(f"{parts[0]}:{parts[1]}")
                continue

            # Catalog library references: shadow(libs.gson)
            cm = _CATALOG_DEP_RE.match(stripped)
            if cm:
                ref = cm.group(2)
                resolved = _resolve_catalog_ref(ref, libraries, bundles)
                for coord in resolved:
                    parts = coord.split(":")
                    if len(parts) >= 2:
                        direct_names.add(f"{parts[0]}:{parts[1]}")
                continue

        graph.external_deps = [
            ExternalDep(name=name, is_direct=True) for name in sorted(direct_names)
        ]

        logger.debug("Gradle deps: %d direct dependencies", len(direct_names))
