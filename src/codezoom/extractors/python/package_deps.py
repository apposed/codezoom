"""Extract external package dependencies from pyproject.toml/pixi.toml + lock files."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from codezoom.extractors.python import is_python_project
from codezoom.model import ExternalDep, ProjectGraph

logger = logging.getLogger(__name__)


class PackageDepsExtractor:
    """Populate external_deps and external_deps_graph from project metadata."""

    def can_handle(self, project_dir: Path) -> bool:
        return is_python_project(project_dir)

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        direct_deps, dep_graph = _extract_python_dependencies(project_dir)

        # Collect all deps (direct + transitive)
        all_deps: set[str] = set(direct_deps)
        visited: set[str] = set()

        def collect_transitive(pkg_name: str) -> None:
            if pkg_name in visited:
                return
            visited.add(pkg_name)
            if pkg_name in dep_graph:
                for dep in dep_graph[pkg_name]:
                    all_deps.add(dep)
                    collect_transitive(dep)

        for dep in direct_deps:
            collect_transitive(dep)

        direct_set = set(direct_deps)
        graph.external_deps = [
            ExternalDep(name=d, is_direct=(d in direct_set)) for d in sorted(all_deps)
        ]
        graph.external_deps_graph = dep_graph


def _extract_python_dependencies(
    project_root: Path,
) -> tuple[list[str], dict[str, list[str]]]:
    """Return (direct_deps, dependency_graph) from project metadata + lock file."""
    # Pixi projects take priority (pixi manages the environment)
    if (project_root / "pixi.toml").exists():
        return _extract_pixi_dependencies(project_root)
    return _extract_uv_dependencies(project_root)


# ---------------------------------------------------------------------------
# pixi path
# ---------------------------------------------------------------------------


def _extract_pixi_direct_deps(project_root: Path) -> list[str]:
    """Read direct dependency names from pixi.toml."""
    import tomllib

    pixi_path = project_root / "pixi.toml"
    try:
        with open(pixi_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Could not parse pixi.toml: %s", e)
        return []

    names: set[str] = set()

    def _collect_keys(section: dict | None) -> None:
        if not isinstance(section, dict):
            return
        for key in section:
            names.add(key.lower())

    # Top-level [dependencies] and [pypi-dependencies]
    _collect_keys(data.get("dependencies"))
    _collect_keys(data.get("pypi-dependencies"))

    # [feature.*.dependencies] and [feature.*.pypi-dependencies]
    for _feat_name, feat in data.get("feature", {}).items():
        if isinstance(feat, dict):
            _collect_keys(feat.get("dependencies"))
            _collect_keys(feat.get("pypi-dependencies"))

    # [target.*.dependencies] and [target.*.pypi-dependencies]
    for _tgt_name, tgt in data.get("target", {}).items():
        if isinstance(tgt, dict):
            _collect_keys(tgt.get("dependencies"))
            _collect_keys(tgt.get("pypi-dependencies"))

    return sorted(names)


def _parse_pixi_lock(project_root: Path) -> dict[str, list[str]]:
    """Build a dependency graph from pixi.lock (YAML)."""
    import yaml

    lock_path = project_root / "pixi.lock"
    if not lock_path.exists():
        return {}

    try:
        with open(lock_path) as f:
            lock_data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        logger.warning("Could not parse pixi.lock: %s", e)
        return {}

    if not isinstance(lock_data, dict):
        return {}

    packages = lock_data.get("packages", [])
    if not isinstance(packages, list):
        return {}

    dep_graph: dict[str, set[str]] = {}

    for pkg in packages:
        if not isinstance(pkg, dict):
            continue

        # PyPI packages have an explicit name field
        if "pypi" in pkg:
            name = pkg.get("name", "")
            if not isinstance(name, str) or not name:
                continue
            name = name.lower()
            deps: set[str] = dep_graph.setdefault(name, set())
            for req in pkg.get("requires_dist", []):
                dep_name = _parse_requirement_name(req)
                if dep_name:
                    deps.add(dep_name)

        # Conda packages: extract name from URL
        elif "conda" in pkg:
            url = pkg.get("conda", "")
            if not isinstance(url, str):
                continue
            name = _conda_name_from_url(url)
            if not name:
                continue
            deps = dep_graph.setdefault(name, set())
            for dep_spec in pkg.get("depends", []):
                if isinstance(dep_spec, str):
                    dep_name = dep_spec.split()[0].lower()
                    if dep_name:
                        deps.add(dep_name)

    # Convert sets to sorted lists, dropping empty entries
    return {k: sorted(v) for k, v in dep_graph.items() if v}


# Regex for the filename segment of a conda URL: <name>-<version>-<build>.<ext>
_CONDA_FILENAME_RE = re.compile(
    r"^(?P<name>.+?)-(?P<version>[^-]+-[^-]+)\.(tar\.bz2|conda)$"
)


def _conda_name_from_url(url: str) -> str:
    """Extract the package name from a conda download URL."""
    filename = url.rsplit("/", 1)[-1]
    m = _CONDA_FILENAME_RE.match(filename)
    if m:
        return m.group("name").lower()
    return ""


def _parse_requirement_name(req: str) -> str:
    """Extract the bare package name from a PEP 508 requirement string."""
    # Strip extras, version constraints, markers
    name = (
        req.split("[")[0]
        .split(">")[0]
        .split("<")[0]
        .split("=")[0]
        .split(";")[0]
        .split("!")[0]
        .split("~")[0]
        .strip()
    )
    return name.lower() if name else ""


def _generate_pixi_lock(project_root: Path) -> None:
    """Generate pixi.lock file if pixi is available."""
    try:
        result = subprocess.run(
            ["pixi", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            logger.info("pixi not available, skipping lock file generation")
            return

        logger.info("Generating pixi.lock for dependency resolution...")
        result = subprocess.run(
            ["pixi", "lock"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            logger.info("Successfully generated pixi.lock")
        else:
            logger.warning(
                "Failed to generate pixi.lock: %s",
                result.stderr.strip() if result.stderr else "unknown error",
            )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("Could not generate pixi.lock: %s", e)
    except Exception as e:
        logger.warning("Unexpected error generating pixi.lock: %s", e)


def _extract_pixi_dependencies(
    project_root: Path,
) -> tuple[list[str], dict[str, list[str]]]:
    """Return (direct_deps, dependency_graph) from pixi.toml + pixi.lock."""
    direct_deps = _extract_pixi_direct_deps(project_root)

    lock_path = project_root / "pixi.lock"
    if not lock_path.exists():
        _generate_pixi_lock(project_root)

    dep_graph = _parse_pixi_lock(project_root)
    return direct_deps, dep_graph


# ---------------------------------------------------------------------------
# uv path (existing)
# ---------------------------------------------------------------------------


def _extract_uv_dependencies(
    project_root: Path,
) -> tuple[list[str], dict[str, list[str]]]:
    """Return (direct_deps, dependency_graph) from pyproject.toml + uv.lock."""
    import tomllib

    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        return [], {}

    # --- direct deps from pyproject.toml ---
    direct_deps: list[str] = []
    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)

        for dep in data.get("project", {}).get("dependencies", []):
            if not isinstance(dep, str):
                continue
            pkg_name = (
                dep.split("[")[0]
                .split(">")[0]
                .split("<")[0]
                .split("=")[0]
                .split(";")[0]
                .strip()
            )
            if pkg_name:
                direct_deps.append(pkg_name.lower())
    except (OSError, tomllib.TOMLDecodeError, KeyError) as e:
        logger.warning("Could not parse pyproject.toml: %s", e)

    # --- transitive deps from uv.lock ---
    dep_graph: dict[str, list[str]] = {}
    uv_lock_path = project_root / "uv.lock"

    # Generate uv.lock if it doesn't exist
    if not uv_lock_path.exists():
        _generate_uv_lock(project_root)

    if uv_lock_path.exists():
        try:
            with open(uv_lock_path, "rb") as f:
                lock_data = tomllib.load(f)

            packages = lock_data.get("package", [])
            if isinstance(packages, list):
                for pkg_info in packages:
                    raw_name = pkg_info.get("name", "")
                    if not isinstance(raw_name, str):
                        continue
                    pkg_name = raw_name.lower()
                    if not pkg_name:
                        continue
                    pkg_deps: list[str] = []
                    dependencies = pkg_info.get("dependencies", [])
                    if isinstance(dependencies, list):
                        for dep in dependencies:
                            if isinstance(dep, dict):
                                raw_dep_name = dep.get("name", "")
                                if not isinstance(raw_dep_name, str):
                                    continue
                                dep_name = raw_dep_name.lower()
                                if dep_name and dep_name not in pkg_deps:
                                    pkg_deps.append(dep_name)
                    if pkg_deps:
                        dep_graph[pkg_name] = pkg_deps
        except (OSError, tomllib.TOMLDecodeError, KeyError) as e:
            logger.warning("Could not parse uv.lock: %s", e)

    return sorted(set(direct_deps)), dep_graph


def _generate_uv_lock(project_root: Path) -> None:
    """Generate uv.lock file if uv is available."""
    try:
        # Check if uv is available
        result = subprocess.run(
            ["uv", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            logger.info("uv not available, skipping lock file generation")
            return

        logger.info("Generating uv.lock for dependency resolution...")
        result = subprocess.run(
            ["uv", "lock"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minutes should be enough
        )
        if result.returncode == 0:
            logger.info("Successfully generated uv.lock")
        else:
            logger.warning(
                "Failed to generate uv.lock: %s",
                result.stderr.strip() if result.stderr else "unknown error",
            )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("Could not generate uv.lock: %s", e)
    except Exception as e:
        logger.warning("Unexpected error generating uv.lock: %s", e)
