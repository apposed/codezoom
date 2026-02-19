"""Shared rustdoc JSON generation and caching."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared rustdoc JSON cache
# ---------------------------------------------------------------------------

_rustdoc_cache: dict[str, dict] = {}
_nightly_checked: dict[str, bool | None] = {}  # project_dir -> available


def get_rustdoc_json(
    project_dir: Path, crate_name: str, target_name: str | None = None
) -> dict | None:
    """Return parsed rustdoc JSON for *crate_name*, generating if needed.

    *target_name* is the lib target name (may differ from package name).
    If not given, defaults to *crate_name* with hyphens replaced by underscores.

    Results are cached in-memory so that both the hierarchy and symbol
    extractors can share the same data without re-running rustdoc.
    """
    cache_key = f"{project_dir}::{crate_name}"
    if cache_key in _rustdoc_cache:
        return _rustdoc_cache[cache_key]

    if target_name is None:
        target_name = crate_name.replace("-", "_")

    doc = _generate_rustdoc_json(project_dir, crate_name, target_name)
    if doc is not None:
        _rustdoc_cache[cache_key] = doc
    return doc


def _check_nightly(project_dir: Path) -> bool:
    """Check if nightly toolchain is available (cached per project)."""
    key = str(project_dir)
    if key in _nightly_checked:
        return bool(_nightly_checked[key])

    rustup = shutil.which("rustup")
    if not rustup:
        logger.warning(
            "rustup not found — skipping rustdoc JSON generation. "
            "Install rustup to enable Rust symbol extraction."
        )
        _nightly_checked[key] = False
        return False

    try:
        result = subprocess.run(
            [rustup, "run", "nightly", "rustc", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("Could not check nightly toolchain: %s", e)
        _nightly_checked[key] = False
        return False

    if result.returncode != 0:
        logger.warning(
            "Rust nightly toolchain not installed — skipping rustdoc JSON. "
            "Install with: rustup toolchain install nightly"
        )
        _nightly_checked[key] = False
        return False

    _nightly_checked[key] = True
    return True


def _generate_rustdoc_json(
    project_dir: Path, crate_name: str, target_name: str
) -> dict | None:
    """Run cargo +nightly rustdoc and return parsed JSON, or None on failure."""
    json_path = project_dir / "target" / "doc" / f"{target_name}.json"

    if not _check_nightly(project_dir):
        return None

    # Generate rustdoc JSON
    logger.info("Generating rustdoc JSON for crate '%s'...", crate_name)
    cmd = [
        "cargo",
        "+nightly",
        "rustdoc",
        "-p",
        crate_name,
        "--lib",
        "--",
        "--output-format",
        "json",
        "-Z",
        "unstable-options",
        "--document-private-items",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(project_dir),
            timeout=300,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("Could not run cargo rustdoc for '%s': %s", crate_name, e)
        return None

    if result.returncode != 0:
        logger.warning(
            "cargo rustdoc failed for '%s': %s",
            crate_name,
            result.stderr.strip() if result.stderr else "unknown error",
        )
        return None

    if not json_path.exists():
        logger.warning("rustdoc JSON not found at %s after generation", json_path)
        return None

    try:
        with open(json_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not parse rustdoc JSON: %s", e)
        return None
