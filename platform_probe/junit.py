"""A report as JUnit XML, one test suite per group, for CI systems that read it."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .runner import Report, Status


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.3f}"


def junit(report: Report) -> bytes:
    root = ET.Element(
        "testsuites",
        name="platform-probe",
        tests=str(len(report.results)),
        failures=str(report.failed),
        skipped=str(report.skipped),
        time=_seconds(report.duration_ms),
    )
    suites: dict[str, tuple[ET.Element, dict[str, int]]] = {}
    for res in report.results:
        if res.group not in suites:
            suite = ET.SubElement(root, "testsuite", name=res.group, tests="0", failures="0", skipped="0")
            suites[res.group] = (suite, {"tests": 0, "failures": 0, "skipped": 0})
        suite, counts = suites[res.group]
        case = ET.SubElement(
            suite, "testcase", name=res.name, classname="platform-probe." + res.id, time=_seconds(res.duration_ms)
        )
        if res.status == Status.FAIL:
            ET.SubElement(case, "failure", message=res.detail).text = res.hint
            counts["failures"] += 1
        elif res.status == Status.SKIP:
            ET.SubElement(case, "skipped", message=res.detail)
            counts["skipped"] += 1
        if res.detail:
            ET.SubElement(case, "system-out").text = res.detail
        counts["tests"] += 1
    for suite, counts in suites.values():
        for k, v in counts.items():
            suite.set(k, str(v))
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
