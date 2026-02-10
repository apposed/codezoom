# codezoom

Multi-level code structure explorer — interactive drill-down HTML visualizations.

codezoom generates a standalone HTML file that lets you explore a project's
structure at multiple levels of detail:

1. **External dependencies** — direct and transitive packages from `pyproject.toml` + `uv.lock`
2. **Package hierarchy** — sub-packages and modules (via [pydeps](https://github.com/thebjorn/pydeps) or file-tree fallback)
3. **Module internals** — functions and classes extracted from the AST
4. **Class internals** — methods and their call relationships

Click any node to drill down. Use breadcrumb navigation to go back up.

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

```bash
codezoom /path/to/project                     # auto-detect, output to codezoom.html
codezoom /path/to/project -o output.html      # custom output path
codezoom /path/to/project --name "My Project" # custom display name
codezoom /path/to/project --open              # open in browser after generating
```

Also works as a module:

```bash
python -m codezoom /path/to/project
```

## Requirements

- Python 3.11+
- No mandatory runtime dependencies beyond the standard library
- Optional: [pydeps](https://github.com/thebjorn/pydeps) for richer module-level
  import analysis (falls back to file-tree scanning without it)

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

| Language | Status |
|----------|--------|
| Python   | Supported |
| Java     | Supported |

## License

[UNLICENSE](UNLICENSE) - All copyright disclaimed.
