"""Suggest how to add optional extras, matching how codezoom was installed."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from shlex import quote


def install_hint(extra: str) -> str:
    """Return a shell command that adds *extra* to the running codezoom."""
    prefix = Path(sys.prefix).resolve()
    checkout = _source_checkout()

    # The checkout's own environment, e.g. created by `uv sync`.
    if checkout and prefix.is_relative_to(checkout) and (checkout / "uv.lock").exists():
        # Note: uv sync removes extras that are not requested, so ask for all.
        project = (
            ""
            if Path.cwd().resolve() == checkout
            else f" --project {quote(str(checkout))}"
        )
        return f"uv sync{project} --all-extras"

    if checkout:
        spec = f"--editable {quote(f'{checkout}[{extra}]')}"
    else:
        spec = quote(f"codezoom[{extra}]")

    if (prefix / "uv-receipt.toml").exists():
        return f"uv tool install {spec}"
    if (prefix / "pipx_metadata.json").exists():
        return f"pipx install --force {spec}"

    python = quote(sys.executable)
    pip_spec = spec.replace("--editable", "-e", 1)
    if importlib.util.find_spec("pip") is None and shutil.which("uv"):
        return f"uv pip install --python {python} {pip_spec}"
    return f"{python} -m pip install {pip_spec}"


def _source_checkout() -> Path | None:
    """Return the source checkout codezoom is running from, if any."""
    pkg_dir = Path(__file__).resolve().parent
    if pkg_dir.parent.name != "src":
        return None
    root = pkg_dir.parent.parent
    pyproject = root / "pyproject.toml"
    try:
        if 'name = "codezoom"' in pyproject.read_text():
            return root
    except OSError:
        pass
    return None
