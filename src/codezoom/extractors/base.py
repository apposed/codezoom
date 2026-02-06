"""Extractor protocol — all extractors conform to this interface."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from codezoom.model import ProjectGraph


class Extractor(Protocol):
    """Protocol for project structure extractors."""

    def can_handle(self, project_dir: Path) -> bool:
        """Return True if this extractor applies to the given project."""
        ...

    def extract(self, project_dir: Path, graph: ProjectGraph) -> None:
        """Populate *graph* with data extracted from *project_dir*."""
        ...
