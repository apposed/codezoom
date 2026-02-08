"""Language-agnostic data model for project structure graphs."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SymbolData:
    """A function, class, or method within a module."""

    name: str
    kind: str  # "function", "class", "method"
    line: int | None = None
    calls: list[str] = field(default_factory=list)
    inherits: list[str] = field(default_factory=list)
    children: dict[str, SymbolData] = field(default_factory=dict)
    visibility: str | None = None  # "public", "protected", "package", "private"


@dataclass
class NodeData:
    """A node in the project hierarchy (package, module, etc.)."""

    children: list[str] = field(default_factory=list)
    imports_to: list[str] = field(default_factory=list)
    imports_from: list[str] = field(default_factory=list)
    symbols: dict[str, SymbolData] | None = None
    class_deps: dict[str, list[str]] | None = None
    is_exported: bool = True  # For Java: whether package is exported in module-info.java


@dataclass
class ExternalDep:
    """An external package dependency."""

    name: str
    is_direct: bool


@dataclass
class ProjectGraph:
    """Complete project structure produced by extractors."""

    project_name: str
    root_node_id: str
    hierarchy: dict[str, NodeData] = field(default_factory=dict)
    external_deps: list[ExternalDep] = field(default_factory=list)
    external_deps_graph: dict[str, list[str]] = field(default_factory=dict)
