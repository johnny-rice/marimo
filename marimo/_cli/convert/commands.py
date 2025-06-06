# Copyright 2024 Marimo. All rights reserved.
from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from marimo._cli.convert.utils import load_external_file
from marimo._cli.print import echo
from marimo._cli.utils import prompt_to_overwrite
from marimo._convert.converters import MarimoConvert
from marimo._utils.paths import maybe_make_dirs


@click.argument("filename", required=True)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Output file to save the converted notebook to. "
        "If not provided, the converted notebook will be printed to stdout."
    ),
)
def convert(
    filename: str,
    output: Optional[Path],
) -> None:
    r"""Convert a Jupyter notebook or Markdown file to a marimo notebook.

    The argument may be either a path to a local .ipynb/.md file,
    or an .ipynb/.md file hosted on GitHub.

    Example usage:

        marimo convert your_nb.ipynb -o your_nb.py

    or

        marimo convert your_nb.md -o your_nb.py

    Jupyter notebook conversion will strip out all outputs. Markdown cell
    conversion with occur on the presence of `{python}` code fences.
    After conversion, you can open the notebook in the editor:

        marimo edit your_nb.py

    Since marimo is different from traditional notebooks, once in the editor,
    you may need to fix errors like multiple definition errors or cycle
    errors.
    """

    ext = Path(filename).suffix
    if ext not in (".ipynb", ".md", ".qmd"):
        raise click.UsageError("File must be an .ipynb or .md file")

    text = load_external_file(filename, ext)
    if ext == ".ipynb":
        notebook = MarimoConvert.from_ipynb(text).to_py()
    else:
        assert ext in (".md", ".qmd")
        notebook = MarimoConvert.from_md(text).to_py()

    if output:
        output_path = Path(output)
        if prompt_to_overwrite(output_path):
            # Make dirs if needed
            maybe_make_dirs(output)
            Path(output).write_text(notebook, encoding="utf-8")
            echo(f"Converted notebook saved to {output}")
    else:
        echo(notebook)
