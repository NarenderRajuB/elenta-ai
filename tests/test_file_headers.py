# Enforces REQ-091: every Python source file opens with a header comment block
# stating its purpose and place in the system.
#
# "Header" means the first non-BOM line is a comment. This applies to every file,
# including __init__.py, so there are no exceptions to remember. The rule itself is
# tested against synthetic inputs so a broken checker cannot pass silently.

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCE_FILES = sorted(p for d in ("app", "tests", "scripts") for p in (ROOT / d).rglob("*.py"))


def has_header_comment(text: str) -> bool:
    lines = text.lstrip("\ufeff").splitlines()
    return bool(lines) and lines[0].startswith("#")


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_positive_every_source_file_has_header(path):
    assert has_header_comment(path.read_text(encoding="utf-8")), f"{path.name} has no header comment block"


def test_negative_code_first_line_rejected():
    assert not has_header_comment("import os\n# comment later\n")


def test_negative_docstring_only_rejected():
    # The brief asks for a header comment block; a docstring is not treated as one.
    assert not has_header_comment('"""Module docstring."""\n')


@pytest.mark.parametrize("text", ["", "\n# comment after blank line\n"])
def test_edge_empty_file_or_leading_blank_line_rejected(text):
    assert not has_header_comment(text)


def test_edge_utf8_bom_before_comment_accepted():
    assert has_header_comment("\ufeff# header\n")
