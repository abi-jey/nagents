"""Negative release-gate regressions; real package/extracted-install tests run in CI."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bundle
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
                        str(Path(temporary).resolve()),
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
        self.output = Path(self.temporary.name).resolve()
        self.version = release.version_check()
        # verify() checks the host's own platform release (Linux CI, macOS runners).
        self.platform = bundle.host_platform() or release.LINUX
        self.manifest_file = release.manifest_name(self.platform)
        for name in (
            f"ngn-v{self.version}-{self.platform}.tar.gz",
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
            "platform": self.platform,
            "artifacts": {path.name: release.digest(path) for path in self.output.iterdir()},
        }
        self.save_manifest()

    def save_manifest(self) -> None:
        (self.output / self.manifest_file).write_text(json.dumps(self.manifest))
        files = [path for path in self.output.iterdir() if path.name != "SHA256SUMS"]
        (self.output / "SHA256SUMS").write_text("".join(f"{release.digest(path)}  {path.name}\n" for path in files))

    def schema_two(self) -> None:
        self.manifest["schema_version"] = 2
        self.manifest["bundle"] = f"ngn-v{self.version}-{self.platform}-bundle.tar.gz"
        for name in (
            str(self.manifest["bundle"]),
            f"ngn-installer-{self.platform}",
            "install.sh",
            f"nagents-{self.version}-py3-none-any.whl",
            f"nagents-{self.version}.tar.gz",
        ):
            (self.output / name).write_bytes(b"bounded synthetic payload; never executed")
        self.manifest["artifacts"] = {
            path.name: release.digest(path)
            for path in self.output.iterdir()
            if path.name not in {self.manifest_file, "SHA256SUMS"}
        }
        self.save_manifest()

    def test_tag_must_match_component_versions(self) -> None:
        with self.assertRaisesRegex(ValueError, "tag does not match"):
            release.version_check("v0.100.999999")

    def test_bump_moves_every_component_and_records_one_line_notes(self) -> None:
        root = self.output / "repo"
        for name in (
            "pyproject.toml",
            "ngn/Cargo.toml",
            "ngn/Cargo.lock",
            "ngn/install.sh",
            "ngn/runtime-lock.json",
            "ngn/runtime-lock.macos-arm64.json",
            "ngn/runtime-lock.macos-x86_64.json",
            "ngn/CHANGELOG.md",
            "ngn/integrations/nagents/pyproject.toml",
            "ngn/integrations/nagents/src/ngn_nagents/__init__.py",
        ):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(release.ROOT / name, root / name)
        (root / "ngn/docs/releases").mkdir(parents=True)
        major, minor, patch_level = self.version.split(".")
        target = f"{major}.{minor}.{int(patch_level) + 1}"
        with self.assertRaisesRegex(ValueError, "one non-empty line"):
            release.bump(target, "two\nlines", root)
        with self.assertRaisesRegex(ValueError, "greater than"):
            release.bump(self.version, "same version", root)
        release.bump(target, "Fix the thing.", root)
        with patch.object(release, "ROOT", root), patch.object(release, "NGN", root / "ngn"):
            self.assertEqual(release.version_check(f"v{target}"), target)
        self.assertEqual((root / f"ngn/docs/releases/{target}.md").read_text(), f"# v{target}\n\nFix the thing.\n")
        self.assertTrue(
            (root / "ngn/CHANGELOG.md").read_text().startswith(f"# Changelog\n\n## {target}\n\n- Fix the thing.\n")
        )

    def test_root_core_version_cannot_diverge_from_native_and_adapter(self) -> None:
        (self.output / "pyproject.toml").write_text('[project]\nversion = "0.100.1"\n')
        with (
            patch.object(release, "ROOT", self.output),
            self.assertRaisesRegex(AssertionError, "share the same version"),
        ):
            release.version_check()

    def test_schema_two_requires_both_core_assets_and_rejects_corruption(self) -> None:
        for name in (f"nagents-{self.version}-py3-none-any.whl", f"nagents-{self.version}.tar.gz"):
            self.schema_two()
            artifacts = dict(self.manifest["artifacts"])
            artifacts.pop(name)
            self.manifest["artifacts"] = artifacts
            self.save_manifest()
            with (
                self.subTest(name=name, failure="missing"),
                self.assertRaisesRegex(AssertionError, "unexpected release assets"),
            ):
                release.verify(self.output)
            self.schema_two()
            (self.output / name).write_bytes(b"corrupt bytes")
            with self.subTest(name=name, failure="corrupt"), self.assertRaisesRegex(AssertionError, "Corrupt artifact"):
                release.verify(self.output)

    def test_official_schema_two_verification_requires_exact_public_core_bytes(self) -> None:
        self.schema_two()
        with (
            patch.object(bundle, "verify_core_artifacts", side_effect=RuntimeError("public-core-gate")) as verify,
            self.assertRaisesRegex(RuntimeError, "public-core-gate"),
        ):
            release.verify(self.output, require_bundle=True)
        self.assertEqual(verify.call_args.kwargs["published"], True)
        self.assertEqual(
            verify.call_args.args[0],
            bundle.CoreArtifacts(
                self.output / f"nagents-{self.version}-py3-none-any.whl", self.output / f"nagents-{self.version}.tar.gz"
            ),
        )

    def test_schema_one_keeps_its_original_three_asset_contract(self) -> None:
        with (
            patch.object(release, "ZipFile", side_effect=RuntimeError("legacy-assets-accepted")),
            self.assertRaisesRegex(RuntimeError, "legacy-assets-accepted"),
        ):
            release.verify(self.output)

    def test_official_core_acquisition_uses_public_downloads_and_never_executes_sdist_build_hooks(self) -> None:
        core = bundle.CoreArtifacts(self.output / "core.whl", self.output / "core.tar.gz")
        with (
            patch.object(bundle, "download_published_core", return_value=core) as download,
            patch.object(release, "commit", return_value="a" * 40),
            patch.object(subprocess, "run") as execute,
        ):
            self.assertEqual(release.core_artifacts(self.output, published=True), core)
        execute.assert_not_called()
        self.assertEqual(download.call_args.kwargs["version"], self.version)

    def test_development_core_builds_wheel_and_sdist_without_resolving_runtime_dependencies(self) -> None:
        destination = self.output / "core"
        destination.mkdir()

        def build(arguments: list[str], *, cwd: Path, check: bool) -> None:
            (destination / f"nagents-{self.version}-py3-none-any.whl").write_bytes(b"source wheel fixture")
            (destination / f"nagents-{self.version}.tar.gz").write_bytes(b"source archive fixture")

        with (
            patch.object(subprocess, "run", side_effect=build) as execute,
            patch.object(release, "commit", return_value="a" * 40),
            patch.object(bundle, "verify_core_artifacts"),
        ):
            result = release.core_artifacts(destination, published=False)
        self.assertEqual(execute.call_args.args[0][1:5], ["-m", "build", "--wheel", "--sdist"])
        self.assertNotIn("pip", execute.call_args.args[0])
        self.assertEqual(result.sdist.name, f"nagents-{self.version}.tar.gz")

    def test_attached_wheel_binding_is_checked_before_executing_native_installer(self) -> None:
        self.schema_two()
        with (
            patch.object(bundle, "verify_bundle_core", side_effect=RuntimeError("embedded-wheel-gate")) as verify,
            self.assertRaisesRegex(RuntimeError, "embedded-wheel-gate"),
        ):
            release.verify_bundled_install(self.output, self.manifest, require_published=True)
        self.assertEqual(verify.call_args.args[1], self.output / f"nagents-{self.version}-py3-none-any.whl")
        self.assertEqual(verify.call_args.kwargs["official"], True)

    def test_bundled_packaging_retains_core_wheel_and_sdist_in_manifest_and_checksums(self) -> None:
        source = self.output / "input"
        source.mkdir()
        core = bundle.CoreArtifacts(
            source / f"nagents-{self.version}-py3-none-any.whl", source / f"nagents-{self.version}.tar.gz"
        )
        core.wheel.write_bytes(b"exact source wheel fixture")
        core.sdist.write_bytes(b"exact source archive fixture")
        binary = source / "native"
        binary.write_bytes(b"native fixture, never executed")
        destination = self.output / "artifacts"

        def run(arguments: list[str], **kwargs: object) -> None:
            if "wheel" in arguments:
                (destination / f"ngn_nagents-{self.version}-py3-none-any.whl").write_bytes(b"adapter fixture")
            elif arguments[:2] == ["git", "archive"]:
                Path(arguments[arguments.index("-o") + 1]).write_bytes(b"source snapshot fixture")
            else:
                raise AssertionError(arguments)

        def small_archive(target: Path, binary: Path, source: Path) -> None:
            target.write_bytes(b"small native fixture")

        def build(output: Path, binary: Path, **kwargs: object) -> Path:
            self.assertEqual(kwargs["core_wheel"], core.wheel)
            self.assertEqual(kwargs["official"], False)
            target = output / f"ngn-v{self.version}-{self.platform}-bundle.tar.gz"
            target.write_bytes(b"bundle fixture")
            return target

        with (
            patch.object(release.subprocess, "check_output", side_effect=["", f"ngn {self.version}"]),
            patch.object(release.subprocess, "run", side_effect=run),
            patch.object(release, "commit", return_value="a" * 40),
            patch.object(release, "binary_archive", side_effect=small_archive),
            patch.object(release, "core_artifacts", return_value=core),
            patch.object(bundle, "build_bundle", side_effect=build),
            patch.object(release, "verify"),
        ):
            release.package(destination, binary, bundled=True)
        manifest = json.loads((destination / self.manifest_file).read_text())
        checksums = (destination / "SHA256SUMS").read_text()
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(len(manifest["artifacts"]), 8)
        for asset in (core.wheel, core.sdist):
            self.assertEqual((destination / asset.name).read_bytes(), asset.read_bytes())
            self.assertEqual(manifest["artifacts"][asset.name], release.digest(asset))
            self.assertIn(f"{release.digest(asset)}  {asset.name}\n", checksums)

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
            "platform": self.platform,
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
        with self.assertRaisesRegex(AssertionError, "does not match this host"):
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
            "".join(line for line in checksum.read_text().splitlines(keepends=True) if self.manifest_file not in line)
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


class MergeTests(unittest.TestCase):
    """Combining natively built platform directories into one published release."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # Canonical: macOS temp dirs sit behind the /var -> /private/var alias.
        self.root = Path(self.temporary.name).resolve()
        self.version = release.version_check()

    def platform_dir(self, platform_name: str) -> Path:
        directory = self.root / platform_name
        directory.mkdir()
        names = release.platform_assets(self.version, platform_name) | release.shared_assets(self.version)
        for name in names:
            # Shared assets deliberately differ per build: the merge must keep Linux's copy.
            (directory / name).write_bytes(f"{platform_name}:{name}".encode())
        manifest = {
            "schema_version": 2,
            "commit": release.commit(),
            "ngn_version": self.version,
            "runtime_protocol": 1,
            "platform": platform_name,
            "bundle": f"ngn-v{self.version}-{platform_name}-bundle.tar.gz",
            "artifacts": {name: release.digest(directory / name) for name in sorted(names)},
        }
        (directory / release.manifest_name(platform_name)).write_text(json.dumps(manifest))
        (directory / "SHA256SUMS").write_text("stale per-platform sums")
        return directory

    def test_merge_keeps_linux_shared_assets_and_adds_only_platform_assets(self) -> None:
        sources = [self.platform_dir(name) for name in ("macos-arm64", "linux-x86_64", "macos-x86_64")]
        output = self.root / "merged"
        release.merge(output, sources)
        names = {path.name for path in output.iterdir()}
        expected = release.shared_assets(self.version) | {"SHA256SUMS", "release-manifest.json"}
        for platform_name in bundle.PLATFORMS:
            expected |= release.platform_assets(self.version, platform_name)
        expected |= {"release-manifest-macos-arm64.json", "release-manifest-macos-x86_64.json"}
        self.assertEqual(names, expected)
        for name in release.shared_assets(self.version):
            self.assertEqual((output / name).read_bytes(), f"linux-x86_64:{name}".encode())
        mac = json.loads((output / "release-manifest-macos-arm64.json").read_text())
        self.assertEqual(set(mac["artifacts"]), release.platform_assets(self.version, "macos-arm64"))
        sums = dict(reversed(line.split("  ", 1)) for line in (output / "SHA256SUMS").read_text().splitlines())
        self.assertEqual(set(sums), names - {"SHA256SUMS"})
        self.assertTrue(all(release.digest(output / name) == checksum for name, checksum in sums.items()))

    def test_merge_rejects_missing_linux_corrupt_or_duplicate_platforms(self) -> None:
        mac = self.platform_dir("macos-arm64")
        with self.assertRaisesRegex(AssertionError, "Linux release supplies"):
            release.merge(self.root / "a", [mac])
        linux = self.platform_dir("linux-x86_64")
        with self.assertRaisesRegex(AssertionError, "Duplicate platform"):
            release.merge(self.root / "b", [linux, mac, mac])
        (mac / "ngn-installer-macos-arm64").write_bytes(b"tampered")
        with self.assertRaisesRegex(AssertionError, "Corrupt macos-arm64 artifact"):
            release.merge(self.root / "c", [linux, mac])
