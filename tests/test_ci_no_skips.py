"""scripts/ci_no_skips.py: CI fails when any test was skipped or none ran."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "ci_no_skips.py"


@pytest.fixture
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_no_skips", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("xml, code", [
    ('<testsuites><testsuite tests="146" skipped="0"/></testsuites>', 0),
    ('<testsuites><testsuite tests="146" skipped="73"/></testsuites>', 1),
    ('<testsuite tests="5" skipped="1"/>', 1),
    ('<testsuites><testsuite tests="3" skipped="0"/><testsuite tests="2" skipped="1"/></testsuites>', 1),
    ('<testsuites><testsuite tests="0" skipped="0"/></testsuites>', 1),
])
def test_skips_or_no_tests_fail(gate: ModuleType, tmp_path: Path, xml: str, code: int) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(xml)
    assert gate.main(str(report)) == code
