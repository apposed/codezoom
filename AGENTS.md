# Codezoom

This document summarizes considerations for AI agents working on the Codezoom codebase.

* For project overview, read `README.md`.

* For testing, use `bin/test.sh`, optionally with paths to specific test files.
  This script intelligently invokes pytest and/or prysk as appropriate.

* For linting, use `bin/lint.sh`, which uses `ruff`.

* Otherwise, always use `uv` to run Python code in the Codezoom environment.

The Codezoom philosophy is to lean on official tools where possible:

* Parsing Java bytecode is simpler and more robust than parsing Java source. Codezoom relies on Java projects to have been compiled by the usual mechanisms (e.g. `mvn` or `gradle`/`gradlew`).

* Codezoom uses `javap` and `jdeps` rather than reimplementing Java bytecode parsing or depending on third party libraries (`javalang`). This makes output more accurate and consistent with other tools of each language.

* An important exception is that Codezoom uses jgo to resolve Maven dependencies rather than invoking `mvn`, because jgo is well tested and robust, and more comprehensive than only parsing `mvn` output would be.
