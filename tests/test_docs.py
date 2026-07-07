"""The docs' Python examples are held to the project's own standards:
each ```python block must be a valid module that passes ruff and mypy --strict,
using this project's configuration — exactly like the README (see test_readme).

Blocks that are intentionally not runnable modules (reference signatures,
snippets referencing undefined names) opt out with an HTML comment, invisible in
the rendered docs:

- ``<!-- docs-test: skip -->`` right before a fence skips that one block;
- ``<!-- docs-test: skip-file ... -->`` anywhere skips every block in the file.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent
_DOCS = _REPO_ROOT / "docs"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_BIN = Path(sys.executable).parent

_PYTHON_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)
_SKIP_FILE = "<!-- docs-test: skip-file"
_SKIP_BLOCK = "<!-- docs-test: skip -->"


def _checkable_blocks(text: str) -> list[str]:
    """The ```python blocks of a doc that are meant to be valid modules."""
    if _SKIP_FILE in text:
        return []
    return [
        match.group(1)
        for match in _PYTHON_BLOCK.finditer(text)
        if not text[: match.start()].rstrip().endswith(_SKIP_BLOCK)
    ]


def _write_blocks(target: Path) -> list[Path]:
    files = []
    for doc in sorted(_DOCS.glob("*.md")):
        for index, block in enumerate(_checkable_blocks(doc.read_text("utf-8"))):
            path = target / f"{doc.stem}_example_{index}.py"
            path.write_text(block, encoding="utf-8")
            files.append(path)
    return files


def test_docs_have_examples() -> None:
    blocks = [
        block
        for doc in _DOCS.glob("*.md")
        for block in _checkable_blocks(doc.read_text("utf-8"))
    ]
    assert blocks, "no checkable ```python examples found under docs/"


def test_docs_examples_pass_ruff(tmp_path: Path) -> None:
    files = _write_blocks(tmp_path)
    result = subprocess.run(
        [str(_BIN / "ruff"), "check", "--config", str(_PYPROJECT), *map(str, files)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_docs_examples_pass_mypy_strict(tmp_path: Path) -> None:
    files = _write_blocks(tmp_path)
    result = subprocess.run(
        [
            str(_BIN / "mypy"),
            "--strict",
            "--config-file",
            str(_PYPROJECT),
            *map(str, files),
        ],
        capture_output=True,
        text=True,
        # Resolve the package's real (typed) source rather than the editable
        # install, so examples are checked against actual types, not Any.
        env={**os.environ, "MYPYPATH": str(_REPO_ROOT / "src")},
    )
    assert result.returncode == 0, result.stdout + result.stderr


if not (_BIN / "ruff").exists() or not (_BIN / "mypy").exists():  # pragma: no cover
    pytest.skip("ruff/mypy not installed in this environment", allow_module_level=True)
