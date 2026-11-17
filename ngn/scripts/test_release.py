"""Negative release-gate regressions; real package/extracted-install tests run in CI."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import release
import yaml


class WorkflowTests(unittest.TestCase):
    def test_native_ci_keeps_full_pr_main_and_manual_gates_without_duplicate_feature_push_runs(self) -> None:
        workflow = yaml.load((release.ROOT / ".github/workflows/ngn-ci.yml").read_text(), Loader=yaml.BaseLoader)
        triggers = workflow["on"]
        self.assertEqual(triggers["push"]["branches"], ["main"])
        self.assertIn("pull_request", triggers)
        self.assertIn("workflow_dispatch", triggers)
        steps = workflow["jobs"]["acceptance"]["steps"]
        self.assertTrue(any("check.sh all" in step.get("run", "") for step in steps))
        self.assertTrue(any("install_smoke.py" in step.get("run", "") for step in steps))


class OutputTests(unittest.TestCase):
    def test_bundled_package_creates_every_new_output_ancestor_private_under_both_umasks(self) -> None:
        code = """import json, stat, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[2])
import release
root = Path(sys.argv[1])
output = root / 'new/nested/artifacts'
with (patch.object(release.subprocess, 'check_output', side_effect=['', 'ngn ' + release.version_check()]),
      patch.object(release.subprocess, 'run', side_effect=RuntimeError('stop-after-output'))):
    try:
        release.package(output, root / 'unused-binary', bundled=True)
    except RuntimeError as error:
        assert str(error) == 'stop-after-output'
    else:
        raise AssertionError('Expected to stop before building wheels')
print(json.dumps([stat.S_IMODE(path.stat().st_mode) for path in (root / 'new', root / 'new/nested', output)]))
"""
        for mask in ("0002", "0022"):
            with self.subTest(mask=mask), tempfile.TemporaryDirectory() as temporary:
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'umask "$1"; shift; exec "$@"',
                        "ngn-output-test",
                        mask,
                        sys.executable,
                        "-c",
                        code,
                        temporary,
                        str(Path(__file__).resolve().parent),
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(json.loads(result.stdout), [stat.S_IRWXU] * 3)


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

    def test_publication_requires_a_self_contained_bundle_not_the_legacy_native_archive(self) -> None:
        with self.assertRaisesRegex(AssertionError, "self-contained runtime bundle"):
            release.verify(self.output, require_bundle=True)

    def test_boolean_or_unknown_schema_cannot_satisfy_the_release_gate(self) -> None:
        for schema in (True, 0, 3):
            with self.subTest(schema=schema):
                self.manifest["schema_version"] = schema
                self.save_manifest()
                with self.assertRaisesRegex(AssertionError, "exact tested commit"):
                    release.verify(self.output)

    def test_development_bundle_cannot_be_relabelled_as_an_official_published_runtime(self) -> None:
        metadata = {
            "bundle_schema": 1,
            "ngn_version": self.version,
            "adapter_version": self.version,
            "nagents_version": self.manifest["nagents_version"],
            "source_commit": self.manifest["commit"],
            "runtime_protocol": 1,
            "platform": "linux-x86_64",
            "runner": ["runtime/bin/python3", "-I", "-B", "-m", "ngn_nagents"],
            "official": False,
        }
        release.validate_bundle_metadata(metadata, self.manifest)
        with self.assertRaisesRegex(AssertionError, "matching published core"):
            release.validate_bundle_metadata(metadata, self.manifest, require_published=True)
        for key in ("ngn_version", "adapter_version", "nagents_version", "source_commit", "platform"):
            with self.subTest(key=key):
                foreign = {**metadata, key: "foreign"}
                with self.assertRaisesRegex(AssertionError, "provenance mismatch"):
                    release.validate_bundle_metadata(foreign, self.manifest)

    def test_bundle_archive_name_is_fixed_and_cannot_point_outside_assets(self) -> None:
        self.manifest["schema_version"] = 2
        self.manifest["bundle"] = "../escape.tar.gz"
        self.save_manifest()
        with self.assertRaisesRegex(AssertionError, "Invalid bundled archive name"):
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

    def test_shared_dev_release_bundle_contains_both_exact_source_examples_only(self) -> None:
        source = self.output / "source"
        source.mkdir()
        names = (
            "LICENSE",
            "README.md",
            "CHANGELOG.md",
            "config.example.json",
            "config.chatgpt.example.json",
        )
        for name in names:
            (source / name).write_bytes((release.NGN / name).read_bytes())
        (source / "docs").mkdir()
        (source / "docs/chatgpt-auth.md").write_bytes((release.NGN / "docs/chatgpt-auth.md").read_bytes())
        (source / "config.local.json").write_text("must not be bundled")
        (source / "auth").mkdir()
        (source / "auth/openai.json").write_text("synthetic private sentinel; must not be bundled")
        binary = self.output / "binary"
        binary.write_bytes(b"fixture executable")
        archive = self.output / "bundle.tar.gz"
        release.binary_archive(archive, binary, source)
        with tarfile.open(archive) as bundled:
            self.assertEqual(set(bundled.getnames()), {"ngn", "docs", "docs/chatgpt-auth.md", *names})
            for name in names:
                member = bundled.extractfile(name)
                assert member is not None
                self.assertEqual(member.read(), (release.NGN / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
