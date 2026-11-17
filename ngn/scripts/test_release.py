"""Negative release-gate regressions; real package/extracted-install tests run in CI."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import release


class ReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.version = release.version_check()
        for name in (
            f"ngn-v{self.version}-linux-x86_64.tar.gz",
            f"ngn-v{self.version}-source.tar.gz",
            f"ngn_nagents-{self.version}-py3-none-any.whl",
        ):
            (self.output / name).write_bytes(b"untrusted fixture; must fail before extraction")
        self.manifest = {
            "schema_version": 1,
            "commit": release.commit(),
            "ngn_version": self.version,
            "nagents_version": release.tomllib.loads((release.ROOT / "pyproject.toml").read_text())["project"][
                "version"
            ],
            "runtime_protocol": 1,
            "platform": "linux-x86_64",
            "artifacts": {path.name: release.digest(path) for path in self.output.iterdir()},
        }
        self.save_manifest()

    def save_manifest(self) -> None:
        (self.output / "release-manifest.json").write_text(json.dumps(self.manifest))
        files = [path for path in self.output.iterdir() if path.name != "SHA256SUMS"]
        (self.output / "SHA256SUMS").write_text("".join(f"{release.digest(path)}  {path.name}\n" for path in files))

    def test_tag_must_match_component_versions(self) -> None:
        with self.assertRaisesRegex(ValueError, "tag does not match"):
            release.version_check("v0.100.999999")

    def test_foreign_commit_is_rejected(self) -> None:
        self.manifest["commit"] = "0" * 40
        self.save_manifest()
        with self.assertRaisesRegex(AssertionError, "exact tested commit"):
            release.verify(self.output)

    def test_foreign_python_core_is_rejected(self) -> None:
        self.manifest["nagents_version"] = "foreign"
        self.save_manifest()
        with self.assertRaisesRegex(AssertionError, "Python core provenance"):
            release.verify(self.output)

    def test_unsupported_platform_is_rejected(self) -> None:
        self.manifest["platform"] = "windows"
        self.save_manifest()
        with self.assertRaisesRegex(AssertionError, "Unsupported release platform"):
            release.verify(self.output)

    def test_corrupt_wheel_is_rejected_before_extraction(self) -> None:
        wheel = next(self.output.glob("*.whl"))
        wheel.write_bytes(b"corrupted")
        with self.assertRaisesRegex(AssertionError, "Corrupt artifact"):
            release.verify(self.output)

    def test_unexpected_manifest_paths_are_rejected(self) -> None:
        self.manifest["artifacts"] = {"../escape": "untrusted"}
        self.save_manifest()
        with self.assertRaisesRegex(AssertionError, "unexpected release assets"):
            release.verify(self.output)

    def test_checksums_cannot_omit_the_manifest(self) -> None:
        checksum = self.output / "SHA256SUMS"
        checksum.write_text(
            "".join(
                line for line in checksum.read_text().splitlines(keepends=True) if "release-manifest.json" not in line
            )
        )
        with self.assertRaises(AssertionError):
            release.verify(self.output)

    def test_dirty_candidate_is_not_packaged(self) -> None:
        with (
            patch.object(release.subprocess, "check_output", return_value=" M README.md\n"),
            self.assertRaisesRegex(AssertionError, "Commit the complete candidate"),
        ):
            release.package(self.output, self.output / "untrusted-binary")


if __name__ == "__main__":
    unittest.main()
