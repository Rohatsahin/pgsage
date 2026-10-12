from __future__ import annotations

import html
import os
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

STEPS = (
    ("install", "📦 Install dependencies"),
    ("pull", "🐘 Pull Postgres image"),
    ("lint", "🧹 Lint (ruff check)"),
    ("format", "🎨 Format (ruff format)"),
    ("types", "🔎 Types (pyright strict)"),
    ("tests", "🧪 Tests (pytest)"),
)
OUTCOME_ICON = {"success": "✅", "failure": "❌", "cancelled": "🚫", "skipped": "⏭️"}
SLOWEST = 5
MAX_FAILURES_SHOWN = 10
MAX_MESSAGE_LINES = 12
MAX_LINE_LENGTH = 300
MAX_SUMMARY_BYTES = 900_000
NON_GATING_STEPS = {"pull"}


@dataclass
class Case:
    file: str
    name: str
    seconds: float
    status: str
    message: str = ""


@dataclass
class Report:
    cases: list[Case] = field(default_factory=lambda: list[Case]())

    def count(self, status: str) -> int:
        return sum(1 for c in self.cases if c.status == status)

    @property
    def seconds(self) -> float:
        return sum(c.seconds for c in self.cases)


def _split_classname(classname: str) -> tuple[str, str]:
    parts = classname.split(".")
    module = max((i for i, p in enumerate(parts) if p.startswith("test_")), default=None)
    if module is None:
        return classname, ""
    return "/".join(parts[: module + 1]) + ".py", ".".join(parts[module + 1 :])


def parse_junit(path: Path) -> tuple[Report, bool]:
    report = Report()
    if not path.exists():
        return report, False
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return report, False
    for case in root.iter("testcase"):
        file, nested = _split_classname(case.get("classname", ""))
        name = case.get("name", "?")
        status, message = "passed", ""
        for child in case:
            if child.tag in ("failure", "error"):
                status, message = "failed", (child.get("message") or "") + "\n" + (child.text or "")
            elif child.tag == "skipped":
                status, message = "skipped", child.get("message") or ""
        report.cases.append(
            Case(
                file,
                f"{nested}::{name}" if nested else name,
                float(case.get("time", "0") or 0),
                status,
                message,
            )
        )
    return report, True


def verdict(outcomes: dict[str, str], report: Report) -> bool:
    steps_ok = all(
        outcome in ("success", "skipped")
        for key, outcome in outcomes.items()
        if key not in NON_GATING_STEPS
    )
    return steps_ok and report.count("failed") == 0


def _trim(message: str) -> str:
    lines = [
        line.rstrip()[:MAX_LINE_LENGTH] + ("…" if len(line.rstrip()) > MAX_LINE_LENGTH else "")
        for line in message.strip().splitlines()
        if line.strip()
    ]
    shown = lines[:MAX_MESSAGE_LINES]
    if len(lines) > len(shown):
        shown.append(f"... {len(lines) - len(shown)} more lines")
    return "\n".join(shown)


def render(outcomes: dict[str, str], report: Report, python: str, junit_found: bool) -> str:
    ok = verdict(outcomes, report)
    banner = "✅ all green" if ok else "❌ needs attention"
    out = [f"## 🧪 pgsage CI · Python {python} · {banner}", ""]
    out += ["| Step | Result |", "| --- | --- |"]
    for key, label in STEPS:
        outcome = outcomes.get(key)
        if outcome is None:
            continue
        out.append(f"| {label} | {OUTCOME_ICON.get(outcome, '❔')} {outcome} |")
    out.append("")
    if not junit_found:
        out += [
            "> ⚠️ No readable test report was produced; the tests step did not run to completion.",
            "",
        ]
        return "\n".join(out)
    out.append("### 🧪 Tests")
    out.append(
        f"✅ **{report.count('passed')}** passed · ❌ **{report.count('failed')}** failed · "
        f"⏭️ **{report.count('skipped')}** skipped · ⏱️ {report.seconds:.1f}s of test time"
    )
    out.append("")
    by_file: dict[str, list[Case]] = defaultdict(list)
    for case in report.cases:
        by_file[case.file].append(case)
    out.append("<details><summary>📄 Results by file</summary>")
    out += ["", "| File | ✅ | ❌ | ⏭️ | ⏱️ |", "| --- | ---: | ---: | ---: | ---: |"]
    for file, cases in sorted(by_file.items()):
        counts = [sum(1 for c in cases if c.status == s) for s in ("passed", "failed", "skipped")]
        flag = "❌ " if counts[1] else ""
        out.append(
            f"| {flag}`{file}` | {counts[0]} | {counts[1]} | {counts[2]} | "
            f"{sum(c.seconds for c in cases):.2f}s |"
        )
    out += ["", "</details>", ""]
    slow = sorted(report.cases, key=lambda c: c.seconds, reverse=True)[:SLOWEST]
    out.append(f"<details><summary>🐢 {len(slow)} slowest tests</summary>")
    out += ["", "| Test | Time |", "| --- | ---: |"]
    out += [f"| `{c.file}::{c.name}` | {c.seconds:.2f}s |".replace("<", "&lt;") for c in slow]
    out += ["", "</details>", ""]
    failures = [c for c in report.cases if c.status == "failed"]
    if failures:
        out.append(f"### ❌ {len(failures)} failing test(s)")
        for case in failures[:MAX_FAILURES_SHOWN]:
            out += [
                f"<details open><summary>❌ <code>{html.escape(case.file)}::{html.escape(case.name)}</code></summary>",
                "",
                "```text",
                _trim(case.message).replace("```", "` ` `"),
                "```",
                "",
                "</details>",
                "",
            ]
        if len(failures) > MAX_FAILURES_SHOWN:
            out.append(f"…and {len(failures) - MAX_FAILURES_SHOWN} more.")
    return "\n".join(out)


def workflow_commands(outcomes: dict[str, str], report: Report) -> list[str]:
    commands: list[str] = []
    for key, label in STEPS:
        if outcomes.get(key) == "failure":
            commands.append(f"::error title={label}::{label} failed, see the step log")
    failed = report.count("failed")
    if failed:
        commands.append(f"::error title=🧪 Tests::{failed} test(s) failed")
    elif report.cases:
        commands.append(
            f"::notice title=🧪 Tests::{report.count('passed')} passed in {report.seconds:.1f}s"
        )
    return commands


def main(argv: list[str]) -> int:
    junit = Path(argv[1]) if len(argv) > 1 else Path("reports/junit.xml")
    python = os.environ.get("PYTHON_VERSION", "?")
    outcomes = {
        key: value
        for key, _ in STEPS
        if (value := os.environ.get(f"STEP_{key.upper()}")) is not None
    }
    report, readable = parse_junit(junit)
    markdown = render(outcomes, report, python, readable)
    if len(markdown.encode()) > MAX_SUMMARY_BYTES:
        markdown = (
            markdown.encode()[:MAX_SUMMARY_BYTES].decode(errors="ignore") + "\n\n…summary truncated"
        )
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
    else:
        print(markdown)
    for command in workflow_commands(outcomes, report):
        print(command)
    return 0 if verdict(outcomes, report) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
