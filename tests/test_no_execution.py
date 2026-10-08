# Static check for ADR-006 C8 / REQ-063: document content is never executed as code,
# a shell command, a template or a tool instruction.
#
# The strongest guarantee is that the application contains none of those mechanisms at
# all, so this scans every module under app/ for them. A new use must be justified and
# this test updated deliberately.

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_FILES = sorted((ROOT / "app").rglob("*.py"))

FORBIDDEN = {
    # Built-ins only: the lookbehind skips method calls such as re.compile(...).
    "eval/exec": re.compile(r"(?<![\w.])(eval|exec|compile)\s*\("),
    "shell/subprocess": re.compile(r"\b(subprocess|os\.system|os\.popen|pty|shlex)\b"),
    "dynamic import": re.compile(r"\b(__import__|importlib)\b"),
    "template engine": re.compile(r"\b(jinja2|mako|string\.Template|Template\()"),
    "str.format on variables": re.compile(r"\.format\(|\.format_map\("),
    "pickle/marshal": re.compile(r"\b(pickle|marshal)\b"),
    "tool/function calling": re.compile(r"\b(tool_choice|function_call)\b|\"tools\"\s*:"),
}


@pytest.mark.parametrize("path", APP_FILES, ids=lambda p: p.name)
def test_negative_no_execution_mechanisms_in_app(path):
    source = path.read_text(encoding="utf-8")
    found = [name for name, pattern in FORBIDDEN.items() if pattern.search(source)]
    assert found == [], f"{path.name} uses {found}"


@pytest.mark.parametrize(
    "snippet, expected",
    [
        ("eval(x)", "eval/exec"),
        ("subprocess.run(cmd)", "shell/subprocess"),
        ("Template(text)", "template engine"),
        ('"tools": []', "tool/function calling"),
        ("{}.format(doc)", "str.format on variables"),
    ],
)
def test_positive_detector_catches_each_mechanism(snippet, expected):
    # Guards against a broken pattern silently passing every file.
    assert FORBIDDEN[expected].search(snippet)


def test_edge_regex_compile_is_not_builtin_compile():
    assert not FORBIDDEN["eval/exec"].search('_WORD = re.compile(r"\\w+")')
    assert FORBIDDEN["eval/exec"].search("code = compile(src, 'f', 'exec')")


def test_edge_ordinary_words_not_flagged():
    text = "Evaluation of the executive summary; the template for the report."
    assert not any(p.search(text) for p in FORBIDDEN.values())
