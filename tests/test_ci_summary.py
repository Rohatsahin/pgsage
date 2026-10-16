import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parent.parent


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_summary", ROOT / "scripts" / "ci_summary.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_summary"] = module
    spec.loader.exec_module(module)
    return module


ci = load()


JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="5">
<testcase classname="tests.test_alpha" name="test_fast" time="0.010"/>
<testcase classname="tests.test_alpha" name="test_slow" time="3.500"/>
<testcase classname="tests.test_beta" name="test_broken" time="0.200">
<failure message="assert 1 == 2">Traceback line one
assert 1 == 2</failure></testcase>
<testcase classname="tests.test_beta" name="test_skipped" time="0.0"><skipped message="needs docker"/></testcase>
<testcase classname="tests.test_beta.TestGroup" name="test_in_class" time="0.050"/>
</testsuite></testsuites>
"""


PASSING = (
    '<testsuites><testsuite><testcase classname="tests.test_a" name="t" time="0.1"/>'
    "</testsuite></testsuites>"
)


GREEN = {
    "install": "success",
    "lint": "success",
    "format": "success",
    "types": "success",
    "tests": "success",
}


def write(tmp_path: Path, xml: str = JUNIT) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(xml)
    return path


def parsed(tmp_path: Path, xml: str = JUNIT):
    report, readable = ci.parse_junit(write(tmp_path, xml))
    assert readable
    return report


def test_parse_counts_statuses_and_files(tmp_path: Path) -> None:
    report = parsed(tmp_path)
    assert (report.count("passed"), report.count("failed"), report.count("skipped")) == (3, 1, 1)
    assert {c.file for c in report.cases} == {"tests/test_alpha.py", "tests/test_beta.py"}
    assert report.seconds == pytest.approx(3.76)


def test_summary_has_emoji_banner_counts_files_and_slowest(tmp_path: Path) -> None:
    text = ci.render({**GREEN, "tests": "failure"}, parsed(tmp_path), "3.12", True)
    assert "❌ needs attention" in text
    assert "✅ **3** passed · ❌ **1** failed · ⏭️ **1** skipped" in text
    assert "| ❌ `tests/test_beta.py` | 1 | 1 | 1 |" in text
    assert "🐢 5 slowest tests" in text
    assert text.index("test_slow") < text.index("test_fast")


def test_failures_show_the_message_and_open_by_default(tmp_path: Path) -> None:
    text = ci.render({**GREEN, "tests": "failure"}, parsed(tmp_path), "3.12", True)
    assert "<details open><summary>❌ <code>tests/test_beta.py::test_broken</code>" in text
    assert "assert 1 == 2" in text


def test_all_green_banner_and_verdict(tmp_path: Path) -> None:
    report = parsed(tmp_path, PASSING)
    assert ci.verdict(GREEN, report) is True
    assert "✅ all green" in ci.render(GREEN, report, "3.13", True)


def test_a_failed_step_fails_the_verdict_even_if_every_test_passed(tmp_path: Path) -> None:
    report = parsed(tmp_path, PASSING)
    assert ci.verdict({**GREEN, "lint": "failure"}, report) is False
    assert ci.verdict({**GREEN, "types": "cancelled"}, report) is False


def test_a_failed_image_pull_does_not_fail_the_verdict(tmp_path: Path) -> None:
    report = parsed(tmp_path, PASSING)
    assert ci.verdict({**GREEN, "pull": "failure"}, report) is True


def test_missing_report_is_explained_not_crashing(tmp_path: Path) -> None:
    report, readable = ci.parse_junit(tmp_path / "nope.xml")
    text = ci.render({"install": "success", "tests": "failure"}, report, "3.12", readable)
    assert "No readable test report" in text
    assert ci.verdict({"tests": "failure"}, report) is False


def test_a_truncated_report_is_reported_not_a_crash(tmp_path: Path) -> None:
    report, readable = ci.parse_junit(
        write(tmp_path, '<testsuites><testsuite><testcase classname="t')
    )
    assert readable is False
    assert report.cases == []


def test_workflow_commands_annotate_failures_and_successes(tmp_path: Path) -> None:
    commands = ci.workflow_commands({"lint": "failure", "tests": "failure"}, parsed(tmp_path))
    assert any(c.startswith("::error title=🧹 Lint") for c in commands)
    assert "::error title=🧪 Tests::1 test(s) failed" in commands
    clean = parsed(tmp_path, PASSING)
    assert any(
        c.startswith("::notice title=🧪 Tests::1 passed") for c in ci.workflow_commands({}, clean)
    )


def test_long_failure_messages_are_trimmed(tmp_path: Path) -> None:
    body = "\n".join(f"line {i}" for i in range(100))
    xml = (
        '<testsuites><testsuite><testcase classname="tests.test_a" name="t" time="1">'
        f'<failure message="m">{body}</failure></testcase></testsuite></testsuites>'
    )
    text = ci.render(GREEN, parsed(tmp_path, xml), "3.12", True)
    assert "more lines" in text
    assert "line 99" not in text


def test_a_multi_megabyte_line_cannot_blow_the_summary_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    huge = "x" * 3_000_000
    xml = (
        '<testsuites><testsuite><testcase classname="tests.test_a" name="t" time="1">'
        f'<failure message="m">{huge}</failure></testcase></testsuite></testsuites>'
    )
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("STEP_TESTS", "failure")
    ci.main(["ci_summary.py", str(write(tmp_path, xml))])
    assert summary.stat().st_size < 1_048_576
    assert "…" in summary.read_text()


def test_the_total_summary_is_capped_even_with_many_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = "".join(
        f'<testcase classname="tests.test_a" name="t{i}" time="1">'
        f'<failure message="m">{"y" * 250}\n'
        + "\n".join("z" * 290 for _ in range(30))
        + "</failure></testcase>"
        for i in range(200)
    )
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("STEP_TESTS", "failure")
    ci.main(
        [
            "ci_summary.py",
            str(write(tmp_path, f"<testsuites><testsuite>{cases}</testsuite></testsuites>")),
        ]
    )
    assert summary.stat().st_size < 1_048_576


def test_nested_classes_keep_their_names_and_test_directories_resolve(tmp_path: Path) -> None:
    xml = (
        "<testsuites><testsuite>"
        '<testcase classname="tests.test_a.TestOuter" name="test_x" time="0.1"/>'
        '<testcase classname="tests.test_a.TestOuter.TestInner" name="test_x" time="0.1"/>'
        '<testcase classname="tests.test_data.test_foo" name="test_y" time="0.1"/>'
        "</testsuite></testsuites>"
    )
    report = parsed(tmp_path, xml)
    assert [(c.file, c.name) for c in report.cases] == [
        ("tests/test_a.py", "TestOuter::test_x"),
        ("tests/test_a.py", "TestOuter.TestInner::test_x"),
        ("tests/test_data/test_foo.py", "test_y"),
    ]


def test_special_characters_in_test_names_are_escaped(tmp_path: Path) -> None:
    xml = (
        '<testsuites><testsuite><testcase classname="tests.test_a" name="t[&lt;b&gt;&amp;]" time="1">'
        '<failure message="m">boom</failure></testcase></testsuite></testsuites>'
    )
    text = ci.render(GREEN, parsed(tmp_path, xml), "3.12", True)
    assert "&lt;b&gt;&amp;" in text
    assert "<b>" not in text


def test_code_fences_in_failure_output_cannot_break_the_markdown(tmp_path: Path) -> None:
    xml = (
        '<testsuites><testsuite><testcase classname="tests.test_a" name="t" time="1">'
        '<failure message="m">before ``` after</failure></testcase></testsuite></testsuites>'
    )
    text = ci.render(GREEN, parsed(tmp_path, xml), "3.12", True)
    body = text.split("```text", 1)[1].split("```", 1)[0]
    assert "before" in body and "after" in body


def test_main_writes_the_step_summary_and_returns_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("PYTHON_VERSION", "3.12")
    for key, value in {"INSTALL": "success", "LINT": "success", "TESTS": "failure"}.items():
        monkeypatch.setenv(f"STEP_{key}", value)
    code = ci.main(["ci_summary.py", str(write(tmp_path))])
    assert code == 1
    assert "Python 3.12" in summary.read_text()
    assert "::error" in capsys.readouterr().out
