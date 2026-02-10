# codezoom

Multi-level code structure explorer — interactive drill-down HTML visualizations.

codezoom generates a standalone HTML file that lets you explore a project's
structure at multiple levels of detail. Click any node to drill down; use
breadcrumb navigation to go back up.

## What it visualizes

<table>
<tr>
<th>Level</th>
<th>Python</th>
<th>Java</th>
</tr>
<tr>
<td><strong>1. External dependencies</strong></td>
<td>Direct and transitive packages from <code>pyproject.toml</code> + <code>uv.lock</code></td>
<td>Direct and transitive Maven dependencies from <code>pom.xml</code> (requires <code>jgo</code>)</td>
</tr>
<tr>
<td><strong>2. Package/module hierarchy</strong></td>
<td>Sub-packages and modules via <a href="https://github.com/thebjorn/pydeps">pydeps</a> (with file-tree fallback) — includes inter-module imports</td>
<td>Package tree via <code>jdeps</code> — includes inter-package dependencies and class-level call graphs</td>
</tr>
<tr>
<td><strong>3. Module/class internals</strong></td>
<td>Functions and classes extracted from AST — shows visibility (public/private based on naming)</td>
<td>Classes, interfaces, enums, and nested classes extracted from compiled bytecode via <code>javap</code> — shows visibility (public/protected/private/package)</td>
</tr>
<tr>
<td><strong>4. Class/method internals</strong></td>
<td>Methods and their call relationships extracted from AST</td>
<td>Methods with parameter signatures and call relationships extracted from bytecode</td>
</tr>
</table>

## Examples

<table>
  <tr>
    <td align="center"><a href="https://raw.githubusercontent.com/apposed/codezoom/main/screenshots/jgo-dependencies.png"><img src="https://raw.githubusercontent.com/apposed/codezoom/main/screenshots/jgo-dependencies.png" width="400"></a><br><em>External dependencies (Python)</em></td>
    <td align="center"><a href="https://raw.githubusercontent.com/apposed/codezoom/main/screenshots/imagej-common-dependencies.png"><img src="https://raw.githubusercontent.com/apposed/codezoom/main/screenshots/imagej-common-dependencies.png" width="400"></a><br><em>External dependencies (Java)</em></td>
  </tr>
  <tr>
    <td align="center"><a href="https://raw.githubusercontent.com/apposed/codezoom/main/screenshots/jgo-submodules.png"><img src="https://raw.githubusercontent.com/apposed/codezoom/main/screenshots/jgo-submodules.png" width="400"></a><br><em>Project submodules</em></td>
    <td align="center"><a href="https://raw.githubusercontent.com/apposed/codezoom/main/screenshots/jgo-env.png"><img src="https://raw.githubusercontent.com/apposed/codezoom/main/screenshots/jgo-env.png" width="400"></a><br><em>A submodule's children</em></td>
  </tr>
  <tr>
    <td align="center"><a href="https://raw.githubusercontent.com/apposed/codezoom/main/screenshots/jgo-cli-rich-formatters.png"><img src="https://raw.githubusercontent.com/apposed/codezoom/main/screenshots/jgo-cli-rich-formatters.png" width="400"></a><br><em>Single-file view (Python)</em></td>
    <td align="center"><a href="https://raw.githubusercontent.com/apposed/codezoom/main/screenshots/imagej-common-net-imagej-DrawingTool.png"><img src="https://raw.githubusercontent.com/apposed/codezoom/main/screenshots/imagej-common-net-imagej-DrawingTool.png" width="400"></a><br><em>Single-file view (Java)</em></td>
    <td></td>
  </tr>
</table>

## Installation

<details><summary><strong>Installing codezoom with uv</strong></summary>

```shell
uv tool install codezoom
```

</details>
<details><summary><strong>Installing codezoom with pip</strong></summary>

```shell
pip install codezoom
```

</details>
<details><summary><strong>Installing codezoom from source</strong></summary>

```shell
git clone https://github.com/apposed/codezoom
uv tool install --with-editable codezoom codezoom
```

When installed in this fashion, changes to the codezoom source code will be immediately reflected when running `codezoom` from the command line.

</details>
<details><summary><strong>Using codezoom as a dependency</strong></summary>

```shell
uv add codezoom
```
or
```shell
pixi add --pypi codezoom
```
Not sure which to use? [Read this](https://jacobtomlinson.dev/posts/2025/python-package-managers-uv-vs-pixi/#so-what-do-i-use).

</details>

## Usage

Basic usage (auto-detects Python or Java):

```bash
codezoom /path/to/project                     # auto-detect, output to codezoom.html
codezoom /path/to/project -o output.html      # custom output path
codezoom /path/to/project --name "My Project" # custom display name
codezoom /path/to/project --open              # open in browser after generating
```

### Python projects

```bash
# Basic usage - requires pyproject.toml
codezoom /path/to/python/project

# For best results, install pydeps first
pip install pydeps
codezoom /path/to/python/project --open
```

### Java projects

```bash
# Compile the project first
cd /path/to/java/project
mvn compile

# Generate visualization (basic - no Maven dependencies)
codezoom . --open

# For full dependency analysis, install jgo
pip install codezoom[java]
codezoom . --open
```

Also works as a module:

```bash
python -m codezoom /path/to/project
```

## Requirements

### Core
- Python 3.11+
- No mandatory runtime dependencies beyond the standard library

### For Python projects
- Optional: [pydeps](https://github.com/thebjorn/pydeps) for richer module-level
  import analysis (falls back to file-tree scanning without it)
  ```bash
  pip install pydeps
  ```

### For Java projects
- **JDK** (not just JRE) — provides `jdeps` and `javap` for package/class analysis
- **Compiled code** — run `mvn compile` before analyzing
- Optional: `jgo` for Maven dependency extraction
  ```bash
  pip install codezoom[java]
  ```

## Per-project configuration

Projects can include a `.codezoom.toml` or a `[tool.codezoom]` section in
`pyproject.toml`:

```toml
[tool.codezoom]
exclude = ["tests", "docs", "__pycache__"]
```

The `exclude` list is passed to pydeps via `-xx` to omit modules from the
hierarchy.

## Language support

### Python
- **Detection**: Presence of `pyproject.toml`
- **Project layouts**: Both `src/` layout and flat layout
- **Dependencies**: Extracted from `pyproject.toml` and `uv.lock`
- **Hierarchy**: Module tree via `pydeps` (falls back to file tree)
- **Symbols**: Functions, classes, and methods via AST parsing
- **Call graphs**: Method calls extracted from AST
- **Visibility**: Public/private based on Python naming conventions (`_private`, `__dunder__`)
- **Configuration**: Via `.codezoom.toml` or `[tool.codezoom]` in `pyproject.toml`

### Java
- **Detection**: Presence of `pom.xml`
- **Prerequisites**:
  - JDK installed (for `jdeps` and `javap`)
  - Project compiled (`mvn compile`)
  - `pip install codezoom[java]` for dependency extraction
- **Dependencies**: Maven dependencies via `jgo` (direct + transitive with scope filtering)
- **Hierarchy**: Package tree via `jdeps` with inter-package dependencies
- **Class dependencies**: Class-to-class and class-to-package relationships via `javap`
- **Symbols**: Classes, interfaces, enums, nested classes via bytecode analysis
- **Call graphs**: Method invocations extracted from bytecode
- **Visibility**: Full Java visibility (public, protected, private, package-private)
- **Limitations**: Bridge methods are filtered out; requires compiled `.class` files

## License

[UNLICENSE](UNLICENSE) - All copyright disclaimed.
