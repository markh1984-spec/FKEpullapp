"""Keep the code runnable on the Python a Mac already has.

macOS ships Python 3.9 with the Command Line Tools, and that is what the
launcher will pick up unless someone has installed a newer one. Using syntax
from a later version would mean a crash on first run for exactly the person
least equipped to debug it.
"""

import ast
from pathlib import Path

import pytest

MINIMUM = (3, 9)
ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted((ROOT / "fkepull").glob("*.py"))


def test_there_are_sources_to_check():
    assert SOURCES, "expected to find the package's modules"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_source_parses_on_the_oldest_python_we_support(path):
    source = path.read_text(encoding="utf-8")
    try:
        ast.parse(source, filename=str(path), feature_version=MINIMUM)
    except SyntaxError as exc:
        pytest.fail(
            f"{path.name} needs Python newer than "
            f"{MINIMUM[0]}.{MINIMUM[1]}: {exc.msg} (line {exc.lineno})"
        )


def test_toml_is_readable_without_the_stdlib_module():
    """3.11 has tomllib built in; older versions need the tomli package."""
    source = (ROOT / "fkepull" / "config.py").read_text(encoding="utf-8")
    assert "import tomli as tomllib" in source, "no fallback for Python 3.10 and older"

    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "tomli" in requirements and 'python_version < "3.11"' in requirements


def test_annotations_are_postponed_everywhere_they_need_to_be():
    """`X | None` in a signature is only legal on 3.9 with this import."""
    for path in SOURCES:
        source = path.read_text(encoding="utf-8")
        if " | None" in source or "] | " in source:
            assert "from __future__ import annotations" in source, (
                f"{path.name} uses 3.10-style type unions without postponed annotations"
            )
