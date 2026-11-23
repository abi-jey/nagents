"""Print actionable summaries while retaining each scanner's original JSON."""

from __future__ import annotations

import argparse
import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Report = dict[str, Any]


@dataclass(frozen=True)
class Scan:
    checked: int
    findings: list[str]
    details: tuple[str, ...] = ()


def advisory(ids: list[str], fixes: list[str]) -> str:
    patched = ", ".join(sorted(set(fixes))) or "not reported"
    return f"{', '.join(sorted(set(ids)))}; patched: {patched}"


def osv_report(report: Report) -> Scan:
    packages = [package for result in report["results"] for package in result["packages"]]
    findings = []
    for package in packages:
        identity = package["package"]
        groups = package.get("groups", ())
        if bool(package.get("vulnerabilities")) != bool(groups):
            raise ValueError("OSV vulnerability details do not match the report")
        for group in groups:
            fixes = [
                event["fixed"]
                for vulnerability in package["vulnerabilities"]
                if vulnerability["id"] in group["ids"]
                for affected in vulnerability.get("affected", ())
                if affected.get("package", {}).get("purl") == identity["purl"].split("@", 1)[0]
                for affected_range in affected.get("ranges", ())
                for event in affected_range["events"]
                if "fixed" in event
            ]
            severity = group.get("max_severity") or "not scored"
            desc = advisory(group["ids"] + group.get("aliases", []), fixes)
            findings.append(f"{identity['name']}=={identity['version']} (CVSS {severity}): {desc}")
    return Scan(len(packages), findings)


def pypi_report(report: Report) -> Scan:
    packages = report["dependencies"]
    skipped = [f"{package['name']}: {package['skip_reason']}" for package in packages if "skip_reason" in package]
    if skipped:
        raise ValueError(f"Incomplete PyPI coverage ({len(skipped)} skipped): {'; '.join(skipped)}")
    findings = [
        f"{package['name']}=={package['version']}: "
        + advisory([vulnerability["id"], *vulnerability.get("aliases", [])], vulnerability["fix_versions"])
        for package in packages
        for vulnerability in package["vulns"]
    ]
    return Scan(len(packages), findings)


def npm_report(report: Report) -> Scan:
    if "error" in report:
        raise ValueError(f"npm registry audit error: {report['error']}")
    info = report["metadata"]
    findings = []
    for name, issue in report["vulnerabilities"].items():
        via = ", ".join(
            entry if isinstance(entry, str) else (entry.get("url") or str(entry["source"])) for entry in issue["via"]
        )
        fix = issue["fixAvailable"]
        patched = (
            f"{fix['name']}@{fix['version']}" if isinstance(fix, dict) else ("available" if fix else "not reported")
        )
        findings.append(f"{name} {issue['range']} ({issue['severity']}): {via}; fix: {patched}")
    counts = info["vulnerabilities"]
    if counts["total"] != len(findings):
        raise ValueError("npm finding count does not match the report")
    severities = ", ".join(f"{name}: {counts[name]}" for name in ("critical", "high", "moderate", "low", "info"))
    return Scan(info["dependencies"]["total"], findings, (f"npm severity counts: {severities}",))


def cargo_report(report: Report) -> Scan:
    database = report["database"]
    if database["advisory-count"] <= 0 or not database["last-commit"]:
        raise ValueError("No RustSec advisory database recorded")
    settings = report["settings"]
    if any(settings[key] for key in ("ignore", "severity", "target_arch", "target_os")):
        raise ValueError("Unexpected RustSec advisory filters")
    notes = [
        f"RustSec DB: {database['advisory-count']} advisories, commit {database['last-commit']}, "
        f"updated {database['last-updated']}"
    ]
    findings = []
    for issue in report["vulnerabilities"]["list"]:
        package = issue["package"]
        info = issue["advisory"]
        desc = advisory([info["id"], *info["aliases"]], issue["versions"]["patched"])
        findings.append(f"{package['name']}=={package['version']}: {desc}")
    if report["vulnerabilities"]["count"] != len(findings):
        raise ValueError("RustSec vulnerability count does not match the report")
    for kind, issues in report["warnings"].items():
        for issue in issues:
            package = issue["package"]
            info = issue.get("advisory") or {}
            note = f"RustSec {kind}: {package['name']}=={package['version']} {info.get('id', '')}".rstrip()
            (findings if issue.get("denied") else notes).append(note)
    return Scan(report["lockfile"]["dependency-count"], findings, tuple(notes))


REPORTERS = {"osv": osv_report, "pip-audit": pypi_report, "npm": npm_report, "cargo-audit": cargo_report}


def bullet(text: str) -> None:
    # Scanner data also goes into a GitHub job summary; render it as one line of text.
    print("- " + html.escape(" ".join(text.split())).replace("::", "&#58;&#58;"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=REPORTERS)
    parser.add_argument("scope")
    parser.add_argument("exit_code", type=int)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    print(f"\n### {args.tool}: {args.scope}\n")
    try:
        result = REPORTERS[args.tool](json.loads(args.report.read_text(encoding="utf-8")))
        if type(result.checked) is not int or result.checked <= 0:
            raise ValueError("No package entries audited")
    except (KeyError, OSError, TypeError, ValueError) as error:
        bullet(f"ERROR · exit {args.exit_code} · {error}")
        return 1

    status = "FINDINGS" if result.findings else ("ERROR" if args.exit_code else "PASS")
    bullet(f"{status} · {result.checked} package entries · {len(result.findings)} findings · exit {args.exit_code}")
    for detail in [*result.details, *result.findings]:
        bullet(detail)
    return int(bool(result.findings) or args.exit_code != 0)


if __name__ == "__main__":
    raise SystemExit(main())
