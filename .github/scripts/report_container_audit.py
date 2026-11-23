"""Fail on new Trivy package/advisory matches without suppressing existing ones."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")


@dataclass(frozen=True)
class Finding:
    kind: str
    ecosystem: str
    package: str
    advisory: str
    severity: str
    version: str
    fixed: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.kind, self.ecosystem, self.package, self.advisory)


@dataclass(frozen=True)
class ImageScan:
    image_id: str
    inventory: Counter[str]
    findings: list[Finding]


def oci_blob(layout: Path, digest: str) -> dict[str, Any]:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Invalid OCI digest")
    algorithm, checksum = digest.split(":", 1)
    data = (layout / "blobs" / algorithm / checksum).read_bytes()
    if hashlib.sha256(data).hexdigest() != checksum:
        raise ValueError(f"OCI content does not match {digest}")
    manifest = json.loads(data)
    if not isinstance(manifest, dict):
        raise ValueError("Expected an OCI object")
    return manifest


def linux_image_id(layout: Path, digest: str) -> str:
    index = json.loads((layout / "index.json").read_text(encoding="utf-8"))
    if [desc["digest"] for desc in index["manifests"]] != [digest]:
        raise ValueError("OCI layout does not point to the build digest")
    manifest = oci_blob(layout, digest)
    if "manifests" in manifest:
        # Buildx provenance manifests are deliberately excluded from the platform scan.
        refs = [
            desc["digest"]
            for desc in manifest["manifests"]
            if (desc.get("platform") or {}).get("os") == "linux"
            and (desc.get("platform") or {}).get("architecture") == "amd64"
        ]
        if len(refs) != 1:
            raise ValueError("Expected exactly one Linux/amd64 image in the OCI index")
        manifest = oci_blob(layout, refs[0])
    config_digest: str = manifest["config"]["digest"]
    config = oci_blob(layout, config_digest)
    if (config.get("os"), config.get("architecture")) != ("linux", "amd64"):
        raise ValueError("Expected a Linux/amd64 image config")
    return config_digest


def load_scan(path: Path, expected_artifact: str) -> ImageScan:
    report = json.loads(path.read_text(encoding="utf-8"))
    if report["SchemaVersion"] != 2 or report["ArtifactType"] != "container_image":
        raise ValueError(f"{path.name}: expected a Trivy v2 container report")
    if report["ArtifactName"] != expected_artifact:
        raise ValueError(f"{path.name}: scanned an unexpected artifact")
    metadata = report["Metadata"]
    image_id = metadata["ImageID"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError(f"{path.name}: missing image config digest")

    inventory: Counter[str] = Counter()
    findings = []
    for result in report["Results"]:
        ecosystem = result["Type"]
        kind = result["Class"]
        inventory[ecosystem] += len(result.get("Packages", []))
        for item in result.get("Vulnerabilities", []):
            finding = Finding(
                kind,
                ecosystem,
                item["PkgName"],
                item["VulnerabilityID"],
                item["Severity"],
                item["InstalledVersion"],
                item.get("FixedVersion") or "",
            )
            if finding.severity not in SEVERITIES or not all((*finding.key, finding.version)):
                raise ValueError(f"{path.name}: malformed finding")
            findings.append(finding)

    # The runtime image contains Debian packages, the Python app and global npm tools.
    for ecosystem in (metadata["OS"]["Family"], "python-pkg", "node-pkg"):
        if not inventory[ecosystem]:
            raise ValueError(f"{path.name}: missing {ecosystem} package inventory")
    return ImageScan(image_id, inventory, findings)


def regressions(baseline: ImageScan, candidate: ImageScan) -> tuple[list[Finding], int]:
    # Count copies, so installing another affected copy is a regression too.
    remaining = Counter(finding.key for finding in baseline.findings)
    added = []
    for finding in candidate.findings:
        if remaining[finding.key]:
            remaining[finding.key] -= 1
        else:
            added.append(finding)
    return added, remaining.total()


def cell(text: str) -> str:
    return html.escape(" ".join(text.split())).replace("|", "&#124;").replace("::", "&#58;&#58;")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("baseline_artifact")
    parser.add_argument("candidate", type=Path)
    parser.add_argument("layout", type=Path)
    parser.add_argument("candidate_digest")
    args = parser.parse_args()
    try:
        baseline = load_scan(args.baseline, args.baseline_artifact)
        candidate = load_scan(args.candidate, str(args.layout))
        if candidate.image_id != linux_image_id(args.layout, args.candidate_digest):
            raise ValueError("Trivy did not scan the Linux/amd64 image behind the build digest")
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(f"**Trivy audit error:** {cell(str(error))}")
        return 1

    added, removed = regressions(baseline, candidate)
    for name, scan in (("Baseline", baseline), ("Candidate", candidate)):
        counts = ", ".join(f"{cell(kind)}: {count}" for kind, count in sorted(scan.inventory.items()))
        print(f"- {name} image config `{scan.image_id}` — {scan.inventory.total()} package entries ({counts}).")
    print("\n| Severity | Baseline matches | Candidate matches | New matches |")
    print("|---|---:|---:|---:|")
    previous, current, new = (
        Counter(finding.severity for finding in findings) for findings in (baseline.findings, candidate.findings, added)
    )
    for severity in SEVERITIES:
        print(f"| {severity} | {previous[severity]} | {current[severity]} | {new[severity]} |")
    print(f"| Total | {previous.total()} | {current.total()} | {new.total()} |")
    print(f"\n{removed} baseline matches no longer present. Counts are package/advisory matches, not unique CVEs.\n")
    if added:
        print("**Trivy audit failed: new findings.**\n")
        print("| Severity | Ecosystem | Package | Installed | Advisory | Fix listed by Trivy |")
        print("|---|---|---|---|---|---|")
        for finding in added:
            print(
                "| "
                + " | ".join(
                    map(
                        cell,
                        (
                            finding.severity,
                            finding.ecosystem,
                            finding.package,
                            finding.version,
                            finding.advisory,
                            finding.fixed or "none",
                        ),
                    )
                )
                + " |"
            )
        return 1
    print("**No new Trivy findings.** See both JSON reports for the full inventory and existing findings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
