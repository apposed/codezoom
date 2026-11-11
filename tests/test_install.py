"""Tests for optional-extra install hints."""

from __future__ import annotations

import sys

import pytest

from codezoom import _install
from codezoom._install import install_hint


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An installed (non-checkout) codezoom in a fresh environment."""
    prefix = tmp_path / "env"
    prefix.mkdir()
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "executable", str(prefix / "bin" / "python"))
    monkeypatch.setattr(_install, "_source_checkout", lambda: None)
    monkeypatch.setattr(_install.shutil, "which", lambda name: None)
    return prefix


def test_hint_pip(env):
    assert install_hint("java") == f"{env}/bin/python -m pip install 'codezoom[java]'"


def test_hint_uv_venv_without_pip(env, monkeypatch):
    monkeypatch.setattr(_install.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(_install.shutil, "which", lambda name: "/usr/bin/uv")
    assert (
        install_hint("java")
        == f"uv pip install --python {env}/bin/python 'codezoom[java]'"
    )


def test_hint_uv_tool(env):
    (env / "uv-receipt.toml").touch()
    assert install_hint("python") == "uv tool install 'codezoom[python]'"


def test_hint_pipx(env):
    (env / "pipx_metadata.json").touch()
    assert install_hint("rust") == "pipx install --force 'codezoom[rust]'"


def test_hint_uv_tool_editable_checkout(env, tmp_path, monkeypatch):
    checkout = tmp_path / "codezoom"
    checkout.mkdir()
    monkeypatch.setattr(_install, "_source_checkout", lambda: checkout)
    (env / "uv-receipt.toml").touch()
    assert install_hint("java") == f"uv tool install --editable '{checkout}[java]'"


def test_hint_checkout_venv(tmp_path, monkeypatch):
    checkout = tmp_path / "codezoom"
    venv = checkout / ".venv"
    venv.mkdir(parents=True)
    (checkout / "uv.lock").touch()
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(_install, "_source_checkout", lambda: checkout.resolve())

    monkeypatch.chdir(checkout)
    assert install_hint("java") == "uv sync --all-extras"

    monkeypatch.chdir(tmp_path)
    assert (
        install_hint("java") == f"uv sync --project {checkout.resolve()} --all-extras"
    )


def test_source_checkout_detected():
    checkout = _install._source_checkout()
    assert checkout is not None
    assert (checkout / "src" / "codezoom" / "_install.py").exists()
