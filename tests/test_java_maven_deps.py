"""Tests for Maven dependency extraction."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from codezoom.model import ProjectGraph

pytest.importorskip("jgo")

from codezoom.extractors.java.maven_deps import _extract_single_module

_JUNIT = Path.home() / ".m2/repository/junit/junit/4.13.2/junit-4.13.2.pom"


@pytest.mark.skipif(not _JUNIT.exists(), reason="junit 4.13.2 not in ~/.m2")
def test_unmanaged_dependency_does_not_abort(tmp_path):
    (tmp_path / "pom.xml").write_text(
        textwrap.dedent(
            """\
            <project>
              <modelVersion>4.0.0</modelVersion>
              <groupId>org.example</groupId>
              <artifactId>demo</artifactId>
              <version>1.0</version>
              <dependencies>
                <dependency>
                  <groupId>org.example</groupId>
                  <artifactId>unmanaged</artifactId>
                </dependency>
                <dependency>
                  <groupId>junit</groupId>
                  <artifactId>junit</artifactId>
                  <version>4.13.2</version>
                </dependency>
              </dependencies>
            </project>
            """
        )
    )
    graph = ProjectGraph(project_name="demo")

    _extract_single_module(tmp_path, graph)

    names = {d.name for d in graph.external_deps}
    assert "junit:junit" in names
    assert "org.example:unmanaged" not in names
