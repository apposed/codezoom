"""Rust extractors — shared helpers."""

from __future__ import annotations

from pathlib import Path

from codezoom.extractors.rust.ast_symbols import RustAstSymbolsExtractor
from codezoom.extractors.rust.cargo_deps import RustCargoDepsExtractor
from codezoom.extractors.rust.module_hierarchy import RustModuleHierarchyExtractor

__all__ = [
    "RustCargoDepsExtractor",
    "RustModuleHierarchyExtractor",
    "RustAstSymbolsExtractor",
    "is_rust_project",
]


def is_rust_project(project_dir: Path) -> bool:
    """Return True if *project_dir* contains a Cargo.toml."""
    return (project_dir / "Cargo.toml").exists()
