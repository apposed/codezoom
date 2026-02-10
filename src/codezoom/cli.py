"""Command-line interface for codezoom."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from codezoom.pipeline import run


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="codezoom",
        description="Multi-level code structure explorer — interactive drill-down HTML visualizations.",
    )
    parser.add_argument(
        "project_dir",
        type=Path,
        help="Path to the project to visualize",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output HTML file path (default: codezoom.html)",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Project display name (default: auto-detect from pyproject.toml)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        dest="open_browser",
        help="Open the generated HTML in a browser",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose (debug) output",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    if args.verbose:
        logging.getLogger("codezoom").setLevel(logging.DEBUG)

    run(
        args.project_dir,
        output=args.output,
        name=args.name,
        open_browser=args.open_browser,
    )
