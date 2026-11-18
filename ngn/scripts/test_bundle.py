"""Offline bundle security/packaging regressions; real relocation is a build gate."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
import warnings
from pathlib import Path
from typing import BinaryIO
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo

import bundle


class BundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "python.tar.gz"
        self.extracted = self.root / "extracted"
        self.extracted.mkdir(mode=0o700)

    def member(self, name: str = "python/file", *, kind: bytes = tarfile.REGTYPE, link: str = "") -> tarfile.TarInfo:
        member = tarfile.TarInfo(name)
        member.mode = 0o644
        member.type = kind
        member.linkname = link
        member.size = 1 if kind == tarfile.REGTYPE else 0
        return member

    def tar(self, *members: tarfile.TarInfo) -> None:
        with tarfile.open(self.archive, "w:gz") as archive:
            for member in members:
                archive.addfile(member, io.BytesIO(b"x" * member.size))

    def wheel(
        self, *, extra: tuple[tuple[str, bytes], ...] = (), tag: str = "py3-none-any", name: str = "example"
    ) -> Path:
        path = self.root / f"{name}-1.0-py3-none-any.whl"
        with ZipFile(path, "w") as wheel:
            for member_name, contents in (
                (f"{name}/__init__.py", b"VALUE = 1\n"),
                (f"{name}-1.0.dist-info/METADATA", f"Metadata-Version: 2.4\nName: {name}\nVersion: 1.0\n".encode()),
                (f"{name}-1.0.dist-info/WHEEL", f"Wheel-Version: 1.0\nTag: {tag}\n".encode()),
                (f"{name}-1.0.dist-info/RECORD", b""),
                (f"{name}-1.0.dist-info/licenses/LICENSE", b"exact upstream license bytes\n\n"),
                (f"{name}-1.0.dist-info/entry_points.txt", b"[console_scripts]\nexample = example:main\n"),
                *extra,
            ):
                wheel.writestr(member_name, contents)
        return path

    def inventory(self) -> Path:
        root = self.root / "ngn"
        (root / "runtime/bin").mkdir(parents=True)
        (root / "runtime/bin/python3.13").write_bytes(b"fixture, never executed")
        (root / "runtime/bin/python3").symlink_to("python3.13")
        (root / "ngn").write_bytes(b"fixture native, never executed")
        (root / "ngn").chmod(0o755)
        files, links = bundle._inventory(root)
        bundle._write_json(root / "bundle.json", {"files": files, "symlinks": links})
        return root

    def test_runtime_lock_is_complete_and_hashes_all_vendored_notices(self) -> None:
        lock = bundle._load_lock()
        self.assertEqual(bundle._object(lock["python"])["version"], "3.13.18")
        self.assertEqual(len(bundle._items(lock["wheels"])), 12)
        notices = bundle._notices(lock)
        self.assertEqual(len(notices), 46)
        metadata = bundle._json(notices["PYTHON.json"])
        self.assertEqual(len(bundle._license_paths(metadata)), 18)
        self.assertNotEqual(notices["LICENSES/LICENSE.cpython.txt"], notices["PYTHON-RUNTIME/LICENSE.txt"])

    def test_notice_version_bump_requires_matching_new_notice_provenance(self) -> None:
        lock = bundle._load_lock()
        python = bundle._object(lock["python"])
        python["version"] = "3.13.999"
        lock["python"] = python
        with self.assertRaisesRegex(ValueError, "version mismatch"):
            bundle._notices(lock)

    def test_missing_or_modified_notices_fail_closed(self) -> None:
        lock = bundle._load_lock()
        target = self.root / "ngn/runtime-licenses"
        shutil.copytree(bundle.ROOT / "ngn/runtime-licenses", target)
        (target / "LICENSES/LICENSE.openssl-3.txt").write_text("missing actual legal text")
        with patch.object(bundle, "ROOT", self.root), self.assertRaisesRegex(ValueError, "corrupt Python license"):
            bundle._notices(lock)

    def test_json_duplicate_keys_and_manifest_size_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Duplicate JSON"):
            bundle._json(b'{"official": true, "official": false}')
        with self.assertRaisesRegex(ValueError, "byte limit"):
            bundle._json(b"{}", bundle.Limits(manifest=1))

    def test_upstream_urls_are_fixed_reviewed_https_only(self) -> None:
        for url in (
            "http://github.com/astral-sh/python-build-standalone/releases/download/tag/file",
            "https://example.org/file",
            "https://github.com/foreign/repo/releases/download/tag/file",
            "https://github.com.evil/astral-sh/python-build-standalone/releases/download/tag/file",
            "https://user:secret@files.pythonhosted.org/packages/file.whl",
            "https://files.pythonhosted.org:443/packages/file.whl",
            "https://files.pythonhosted.org/packages/file.whl?token=secret",
            "https://pypi.org/pypi/nagents/latest/json",
            "https://pypi.org/pypi/nagents/0.28.5/json#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                bundle._url(url)
        bundle._url("https://pypi.org/pypi/nagents/0.28.5/json")
        bundle._url(
            "https://release-assets.githubusercontent.com/github-production-release-asset/file?signature=x",
            redirect=True,
        )
        with self.assertRaises(ValueError):
            bundle._url("https://release-assets.githubusercontent.com/file")

    def test_artifact_filename_hash_size_and_url_are_validated(self) -> None:
        artifact: dict[str, object] = {
            "filename": "file.whl",
            "url": "https://files.pythonhosted.org/packages/file.whl",
            "sha256": "1" * 64,
            "size": 1,
        }
        self.assertEqual(bundle.Artifact.parse(artifact).size, 1)
        for change in (
            {"filename": "../file.whl"},
            {"sha256": "NOT A HASH"},
            {"size": True},
            {"size": 0},
            {"filename": "other.whl"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                bundle.Artifact.parse(artifact | change)

    def test_download_hash_is_verified_before_any_extraction(self) -> None:
        cache = self.root / "cache"
        artifact = bundle.Artifact("file.whl", "https://files.pythonhosted.org/packages/file.whl", "0" * 64, 1)

        def download(url: str, stream: BinaryIO, maximum: int) -> int:
            stream.write(b"x")
            return 1

        with patch.object(bundle, "_download", side_effect=download), self.assertRaisesRegex(ValueError, "SHA-256"):
            bundle._fetch(artifact, cache)
        self.assertEqual(list(cache.iterdir()), [])

    def test_verified_cache_reuse_and_corrupt_cache_refusal(self) -> None:
        cache = self.root / "cache"
        artifact = bundle.Artifact(
            "file.whl", "https://files.pythonhosted.org/packages/file.whl", hashlib.sha256(b"x").hexdigest(), 1
        )

        def download(url: str, stream: BinaryIO, maximum: int) -> int:
            stream.write(b"x")
            return 1

        with patch.object(bundle, "_download", side_effect=download) as request:
            target = bundle._fetch(artifact, cache)
            self.assertEqual(bundle._fetch(artifact, cache), target)
            self.assertEqual(request.call_count, 1)
            target.write_bytes(b"y")
            with self.assertRaisesRegex(ValueError, "Corrupt cached"):
                bundle._fetch(artifact, cache)

    def test_oversized_download_is_rejected_before_network(self) -> None:
        artifact = bundle.Artifact("file.whl", "https://files.pythonhosted.org/packages/file.whl", "0" * 64, 2)
        with patch.object(bundle, "_download") as request, self.assertRaisesRegex(ValueError, "compressed"):
            bundle._fetch(artifact, self.root / "cache", bundle.Limits(compressed=1))
        request.assert_not_called()

    def test_download_stream_and_declared_length_are_bounded(self) -> None:
        class Response(io.BytesIO):
            url = "https://files.pythonhosted.org/packages/file.whl"
            headers: dict[str, str] = {}

        for length in ({}, {"Content-Length": "999"}):
            response = Response(b"too large")
            response.headers = length
            destination = io.BytesIO()
            with patch("bundle.build_opener") as opener:
                opener.return_value.open.return_value.__enter__.return_value = response
                with self.subTest(length=length), self.assertRaisesRegex(ValueError, "compressed byte limit"):
                    bundle._download(response.url, destination, 1)
            self.assertEqual(destination.getvalue(), b"")

    def test_unsafe_existing_cache_ancestry_is_never_repaired(self) -> None:
        unsafe = self.root / "unsafe"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "Unsafe directory ancestry"):
            bundle._private_directory(unsafe / "child")
        self.assertEqual(stat.S_IMODE(unsafe.stat().st_mode), 0o777)
        self.assertFalse((unsafe / "child").exists())

    def test_symlink_cache_parent_and_nonprivate_cache_are_rejected(self) -> None:
        actual = self.root / "actual"
        actual.mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(actual, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            bundle._private_directory(link / "child")
        self.assertFalse((actual / "child").exists())
        actual.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "refusing to repair"):
            bundle._private_directory(actual)
        self.assertEqual(stat.S_IMODE(actual.stat().st_mode), 0o755)

    def test_missing_cache_ancestors_are_created_private(self) -> None:
        bundle._private_directory(self.root / "new/ancestors/cache")
        for name in ("new", "new/ancestors", "new/ancestors/cache"):
            self.assertEqual(stat.S_IMODE((self.root / name).stat().st_mode), 0o700)

    def test_tar_traversal_absolute_foreign_and_ambiguous_paths_are_rejected(self) -> None:
        for name in (
            "../escape",
            "/absolute",
            "python/../../escape",
            "foreign/file",
            "python//file",
            "python/./file",
            "python\\file",
            "C:/file",
        ):
            with self.subTest(name=name):
                self.tar(self.member(name))
                with self.assertRaises(ValueError):
                    bundle._extract_tar(self.archive, self.extracted)
                self.assertEqual(list(self.extracted.iterdir()), [])

    def test_duplicate_tar_members_are_rejected_before_writing(self) -> None:
        self.tar(self.member(), self.member())
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            bundle._extract_tar(self.archive, self.extracted)
        self.assertEqual(list(self.extracted.iterdir()), [])

    def test_hardlinks_devices_fifos_and_privileged_modes_are_rejected(self) -> None:
        for kind in (tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                self.tar(self.member(), self.member("python/special", kind=kind, link="python/file"))
                with self.assertRaisesRegex(ValueError, "Special files"):
                    bundle._extract_tar(self.archive, self.extracted)
                self.assertEqual(list(self.extracted.iterdir()), [])
        member = self.member()
        member.mode = 0o4755
        self.tar(member)
        with self.assertRaisesRegex(ValueError, "privileged"):
            bundle._extract_tar(self.archive, self.extracted)

    def test_absolute_escaping_missing_and_cyclic_symlinks_are_rejected(self) -> None:
        for target in ("/etc/passwd", "../../escape", "../foreign/file", "missing", "link"):
            with self.subTest(target=target):
                self.tar(self.member(), self.member("python/link", kind=tarfile.SYMTYPE, link=target))
                with self.assertRaises(ValueError):
                    bundle._extract_tar(self.archive, self.extracted)
                self.assertEqual(list(self.extracted.iterdir()), [])

    def test_even_internal_symlink_ancestors_cannot_be_used_for_writes(self) -> None:
        self.tar(
            self.member("python/dir", kind=tarfile.DIRTYPE),
            self.member("python/link", kind=tarfile.SYMTYPE, link="dir"),
            self.member("python/link/payload"),
        )
        with self.assertRaisesRegex(ValueError, "non-directory ancestor"):
            bundle._extract_tar(self.archive, self.extracted)
        self.assertEqual(list(self.extracted.iterdir()), [])

    def test_actual_internal_relative_python_symlinks_are_supported(self) -> None:
        self.tar(
            self.member("python/bin/python3.13"),
            self.member("python/bin/python3", kind=tarfile.SYMTYPE, link="python3.13"),
        )
        bundle._extract_tar(self.archive, self.extracted)
        self.assertEqual(os.readlink(self.extracted / "bin/python3"), "python3.13")
        self.assertEqual((self.extracted / "bin/python3").read_bytes(), b"x")

    def test_tar_compressed_expanded_and_member_count_limits(self) -> None:
        self.tar(self.member())
        for limits, message in (
            (bundle.Limits(compressed=1), "compressed"),
            (bundle.Limits(expanded=512), "expanded"),
            (bundle.Limits(members=0), "member count"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                bundle._extract_tar(self.archive, self.extracted, limits=limits)
        self.assertEqual(list(self.extracted.iterdir()), [])

    def test_tar_never_extracts_into_existing_payload_or_symlink_directory(self) -> None:
        self.tar(self.member())
        (self.extracted / "sentinel").write_bytes(b"not ours")
        with self.assertRaisesRegex(ValueError, "new empty private"):
            bundle._extract_tar(self.archive, self.extracted)
        self.assertEqual((self.extracted / "sentinel").read_bytes(), b"not ours")
        link = self.root / "link"
        link.symlink_to(self.extracted, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "new empty private"):
            bundle._extract_tar(self.archive, link)

    def test_wheel_traversal_absolute_and_ambiguous_paths_are_rejected(self) -> None:
        for name in ("../escape", "/escape", "example/../../escape", "example//bad", "example\\bad"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                bundle._wheel_metadata(self.wheel(extra=((name, b"x"),)), "example", "1.0")

    def test_wheel_symlinks_devices_and_privileged_modes_are_rejected(self) -> None:
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFCHR | 0o600, stat.S_IFREG | 0o4755):
            path = self.wheel()
            entry = ZipInfo("example/special")
            entry.external_attr = mode << 16
            with ZipFile(path, "a") as wheel:
                wheel.writestr(entry, "../escape")
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "Special"):
                bundle._wheel_metadata(path, "example", "1.0")

    def test_duplicate_wheel_paths_are_rejected(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            path = self.wheel(extra=(("example/__init__.py", b"duplicate"),))
        with self.assertRaisesRegex(ValueError, "Duplicate wheel"):
            bundle._wheel_metadata(path, "example", "1.0")

    def test_adapter_wheel_must_match_the_exact_checked_in_commit_not_only_its_version(self) -> None:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=bundle.ROOT, text=True).strip()
        prefix = "ngn/integrations/nagents/src/"
        files = bundle._git_files(commit, (prefix + "ngn_nagents",))
        path = self.root / "adapter.whl"
        with ZipFile(path, "w") as wheel:
            for name, contents in files.items():
                wheel.writestr(name.removeprefix(prefix), contents)
        bundle._adapter_source(path, commit)
        with ZipFile(path, "a") as wheel:
            wheel.writestr("ngn_nagents/extra.py", "unreviewed addition")
        with self.assertRaisesRegex(ValueError, "exact source_commit adapter"):
            bundle._adapter_source(path, commit)

    def test_official_core_python_bytes_must_match_source_even_when_public_version_matches(self) -> None:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=bundle.ROOT, text=True).strip()
        files = bundle._git_files(commit, ("src/nagents",), suffixes=(".py", "/py.typed"))
        path = self.root / "core.whl"
        with ZipFile(path, "w") as wheel:
            for name, contents in files.items():
                wheel.writestr(name.removeprefix("src/"), contents)
            wheel.writestr("nagents/web/static/generated.js", "legitimately built frontend")
        binding = bundle._core_source(path, commit)
        self.assertGreater(int(str(binding["source_python_files"])), 100)
        with ZipFile(path, "w") as wheel:
            for name, contents in files.items():
                wheel.writestr(
                    name.removeprefix("src/"),
                    contents + b"\n# unpublished change\n" if name.endswith("/agent.py") else contents,
                )
        with self.assertRaisesRegex(ValueError, "release the matching core first and bump its version"):
            bundle._core_source(path, commit)

    def test_editable_and_interpreter_customizing_wheels_are_rejected(self) -> None:
        for name in (
            "editable.pth",
            "sitecustomize.py",
            "usercustomize.py",
            "example-1.0.dist-info/direct_url.json",
            "pyvenv.cfg",
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Editable"):
                bundle._wheel_metadata(self.wheel(extra=((name, b"x"),)), "example", "1.0")

    def test_foreign_core_adapter_versions_and_wheel_targets_are_rejected(self) -> None:
        path = self.wheel()
        with self.assertRaisesRegex(ValueError, "name/version mismatch"):
            bundle._wheel_metadata(path, "nagents", "0.28.5")
        for tag in (
            "cp313t-cp313t-linux_x86_64",
            "cp314-cp314-manylinux_2_17_x86_64",
            "cp313-cp313-win_amd64",
            "cp313-cp313-manylinux_2_17_aarch64",
        ):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "not compatible"):
                bundle._wheel_metadata(self.wheel(tag=tag), "example", "1.0")

    def test_wheel_install_is_noneditable_retains_dist_info_licenses_and_omits_scripts(self) -> None:
        path = self.wheel()
        site = self.root / "site-packages"
        bundle._install_wheel(path, site, "example", "1.0")
        self.assertEqual((site / "example/__init__.py").read_bytes(), b"VALUE = 1\n")
        self.assertEqual(
            (site / "example-1.0.dist-info/licenses/LICENSE").read_bytes(), b"exact upstream license bytes\n\n"
        )
        self.assertEqual((site / "example-1.0.dist-info/INSTALLER").read_text(), "ngn-bundle\n")
        record = (site / "example-1.0.dist-info/RECORD").read_text()
        self.assertIn("sha256=", record)
        self.assertNotIn(str(self.root), record)
        self.assertFalse((site / "bin").exists())
        self.assertFalse(list(site.rglob("*.pth")))

    def test_wheel_script_or_unknown_data_scheme_cannot_be_silently_installed(self) -> None:
        for scheme in ("scripts", "headers", "data"):
            with self.subTest(scheme=scheme), self.assertRaisesRegex(ValueError, "Unreviewed wheel data scheme"):
                bundle._install_wheel(
                    self.wheel(extra=((f"example-1.0.data/{scheme}/file", b"x"),)), self.root / scheme, "example", "1.0"
                )

    def test_wheel_expanded_member_and_compressed_limits(self) -> None:
        path = self.wheel()
        for limits, message in (
            (bundle.Limits(compressed=1), "bounded binary wheel"),
            (bundle.Limits(expanded=1), "expanded"),
            (bundle.Limits(members=1), "member count"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                bundle._wheel_metadata(path, "example", "1.0", limits)

    def test_official_core_must_match_fixed_public_pypi_filename_and_hash(self) -> None:
        wheel = self.wheel()
        metadata = {
            "info": {"name": "nagents", "version": "0.28.5"},
            "urls": [
                {
                    "filename": wheel.name,
                    "packagetype": "bdist_wheel",
                    "yanked": False,
                    "url": f"https://files.pythonhosted.org/packages/{wheel.name}",
                    "size": wheel.stat().st_size,
                    "digests": {"sha256": "0" * 64},
                }
            ],
        }

        def download(url: str, stream: BinaryIO, maximum: int) -> int:
            self.assertEqual(url, "https://pypi.org/pypi/nagents/0.28.5/json")
            return stream.write(json.dumps(metadata).encode())

        with (
            patch.object(bundle, "_download", side_effect=download),
            self.assertRaisesRegex(ValueError, "exact published PyPI wheel"),
        ):
            bundle._published_core(wheel, "0.28.5")

    def test_runner_and_all_probe_invocations_isolate_and_disable_bytecode_without_host_environment(self) -> None:
        self.assertEqual(bundle.RUNNER, ["runtime/bin/python3", "-I", "-B", "-m", "ngn_nagents"])
        report = json.dumps({"python": "3.13.18", "versions": {"nagents": "0.28.5", "ngn-nagents": "0.100.2"}})
        result = subprocess.CompletedProcess(["fixture"], 0, stdout=report, stderr="")
        with patch.object(subprocess, "run", return_value=result) as run:
            reports = bundle._probe(
                self.root, self.extracted, python_version="3.13.18", core_version="0.28.5", adapter_version="0.100.2"
            )
        self.assertEqual(len(reports), 2)
        self.assertEqual(run.call_count, 3)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][1:3], ["-I", "-B"])
            self.assertNotIn("PYTHONPATH", call.kwargs["env"])
            self.assertNotIn("PYTHONHOME", call.kwargs["env"])
        self.assertEqual(run.call_args_list[0].kwargs["env"]["PATH"], "")
        self.assertNotIn("PATH", run.call_args_list[1].kwargs["env"])

    def test_missing_pip_uses_only_this_runtimes_bundled_ensurepip_wheel_without_compilation_or_scripts(self) -> None:
        directory = self.root / "runtime/lib/python3.13/ensurepip/_bundled"
        directory.mkdir(parents=True)
        wheel = self.wheel(name="pip")
        shutil.copyfile(wheel, directory / wheel.name)
        result = subprocess.CompletedProcess(["fixture"], 1, stdout=b"")
        with patch.object(subprocess, "run", return_value=result) as run:
            bundle._runtime_pip(self.root, self.extracted)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:3], [str(self.root / "runtime/bin/python3"), "-I", "-B"])
        site = self.root / "runtime/lib/python3.13/site-packages"
        self.assertEqual((site / "pip-1.0.dist-info/INSTALLER").read_text(), "ngn-bundle\n")
        self.assertFalse(list(site.rglob("*.pyc")))
        self.assertFalse((site / "bin").exists())

    def test_upstream_local_pip_origin_is_removed_and_its_record_remains_consistent(self) -> None:
        site = self.root / "runtime/lib/python3.13/site-packages"
        bundle._install_wheel(self.wheel(name="pip"), site, "pip", "1.0")
        origin = site / "pip-1.0.dist-info/direct_url.json"
        origin.write_text('{"url": "file:///build/pip.whl"}')
        record = site / "pip-1.0.dist-info/RECORD"
        with record.open("a") as output:
            output.write("pip-1.0.dist-info/direct_url.json,,\n../../../bin/pip,,\n")
        bundle._sanitize_pip(self.root)
        self.assertFalse(origin.exists())
        self.assertNotIn("direct_url.json", record.read_text())
        self.assertNotIn("../../../bin/pip", record.read_text())
        self.assertTrue((site / "pip-1.0.dist-info/licenses/LICENSE").exists())

    def test_inventory_detects_modified_missing_added_and_link_targets(self) -> None:
        root = self.inventory()
        bundle._verify_inventory(root)
        payload = root / "ngn"
        payload.write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "inventory mismatch"):
            bundle._verify_inventory(root)
        payload.unlink()
        with self.assertRaisesRegex(ValueError, "inventory mismatch"):
            bundle._verify_inventory(root)
        payload.write_bytes(b"fixture native, never executed")
        (root / "unexpected").write_bytes(b"extra")
        with self.assertRaisesRegex(ValueError, "inventory mismatch"):
            bundle._verify_inventory(root)
        (root / "unexpected").unlink()
        link = root / "runtime/bin/python3"
        link.unlink()
        link.symlink_to("./python3.13")
        with self.assertRaisesRegex(ValueError, "inventory mismatch"):
            bundle._verify_inventory(root)

    def test_inventory_rejects_external_links_docs_links_and_hardlinks(self) -> None:
        root = self.inventory()
        (root / "docs-link").symlink_to("ngn")
        with self.assertRaisesRegex(ValueError, "internal runtime"):
            bundle._inventory(root)
        (root / "docs-link").unlink()
        os.link(root / "ngn", root / "hardlink")
        with self.assertRaisesRegex(ValueError, "hardlinks"):
            bundle._inventory(root)

    def test_archive_is_reproducible_sorted_and_has_only_controlled_regular_dir_symlink_members(self) -> None:
        root = self.inventory()
        first, second = self.root / "first.tar.gz", self.root / "second.tar.gz"
        bundle._archive(root, first)
        os.utime(root / "ngn", (123456789, 123456789))
        bundle._archive(root, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with tarfile.open(first) as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            self.assertEqual(names, sorted(names))
            self.assertEqual(len(names), len(set(names)))
            self.assertTrue(all(member.name == "ngn" or member.name.startswith("ngn/") for member in members))
            for member in members:
                self.assertEqual((member.mtime, member.uid, member.gid), (0, 0, 0))
                self.assertTrue(member.isfile() or member.isdir() or member.issym())
                self.assertIn(member.mode, (0o644, 0o755, 0o777))

    def test_archive_never_overwrites_an_existing_output(self) -> None:
        root = self.inventory()
        target = self.root / "existing.tar.gz"
        target.write_bytes(b"protected")
        with self.assertRaises(FileExistsError):
            bundle._archive(root, target)
        self.assertEqual(target.read_bytes(), b"protected")

    def test_documents_are_exact_commit_bytes_and_never_include_local_auth_or_configuration(self) -> None:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=bundle.ROOT, text=True).strip()
        bundle._documents(self.extracted, commit)
        self.assertEqual(
            (self.extracted / "config.example.json").read_bytes(),
            subprocess.check_output(["git", "show", f"{commit}:ngn/config.example.json"], cwd=bundle.ROOT),
        )
        self.assertFalse((self.extracted / "config.local.json").exists())
        self.assertFalse((self.extracted / "auth").exists())
        self.assertTrue((self.extracted / "LICENSE").is_file())

    def test_build_rejects_unsupported_platform_and_malformed_source_before_network(self) -> None:
        def build() -> Path:
            return bundle.build_bundle(
                self.root / "out",
                self.root / "binary",
                core_wheel=self.root / "core.whl",
                adapter_wheel=self.root / "adapter.whl",
                official=False,
                source_commit="invalid",
                cache=self.root / "cache",
            )

        with (
            patch.object(platform, "system", return_value="FreeBSD"),
            self.assertRaisesRegex(ValueError, "built natively"),
        ):
            build()
        with self.assertRaisesRegex(ValueError, "source_commit"):
            build()


class CoreDistributionTests(unittest.TestCase):
    """Fast public-asset/source/runtime binding tests; downloaded code is never run."""

    VERSION = "0.100.2"
    COMMIT = "a" * 40

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = b'[project]\nname = "nagents"\nversion = "0.100.2"\n'
        self.sources = {
            "src/nagents/__init__.py": b"VALUE = 1\n",
            "src/nagents/provider/device_auth.py": b"# public source fixture\n",
            "src/nagents/py.typed": b"",
        }
        self.response_metadata: dict[str, object] = {}
        self.core = bundle.CoreArtifacts(
            self.root / f"nagents-{self.VERSION}-py3-none-any.whl", self.root / f"nagents-{self.VERSION}.tar.gz"
        )
        self.source_patch = patch.object(bundle, "_git_files", side_effect=self.git_files)
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)
        self.write_wheel({})
        self.write_sdist({})

    def git_files(self, commit: str, paths: tuple[str, ...], *, suffixes: tuple[str, ...] = ()) -> dict[str, bytes]:
        self.assertEqual(commit, self.COMMIT)
        return {"pyproject.toml": self.project} if paths == ("pyproject.toml",) else self.sources.copy()

    def wheel_files(self) -> dict[str, bytes]:
        distribution = f"nagents-{self.VERSION}.dist-info"
        return {
            **{name.removeprefix("src/"): contents for name, contents in self.sources.items()},
            f"{distribution}/METADATA": f"Metadata-Version: 2.4\nName: nagents\nVersion: {self.VERSION}\n".encode(),
            f"{distribution}/WHEEL": b"Wheel-Version: 1.0\nTag: py3-none-any\n",
            f"{distribution}/RECORD": b"",
            f"{distribution}/licenses/LICENSE": b"retained upstream license\n",
        }

    def write_wheel(self, changes: dict[str, bytes]) -> None:
        with ZipFile(self.core.wheel, "w") as wheel:
            for name, contents in (self.wheel_files() | changes).items():
                wheel.writestr(name, contents)

    def sdist_files(self) -> dict[str, bytes]:
        return {
            "PKG-INFO": f"Name: nagents\nVersion: {self.VERSION}\n".encode(),
            "pyproject.toml": self.project,
            **self.sources,
        }

    def write_sdist(
        self, changes: dict[str, bytes], *, extra: str = "", root: str = "", kind: bytes = tarfile.REGTYPE
    ) -> None:
        prefix = root or f"nagents-{self.VERSION}"
        with tarfile.open(self.core.sdist, "w:gz") as source:
            for name, contents in (self.sdist_files() | changes).items():
                member = tarfile.TarInfo(f"{prefix}/{name}")
                member.size = len(contents)
                source.addfile(member, io.BytesIO(contents))
            if extra:
                member = tarfile.TarInfo(extra)
                member.type, member.linkname = kind, "../../escape"
                source.addfile(member, io.BytesIO())

    def metadata(self) -> dict[str, object]:
        return {
            "info": {"name": "nagents", "version": self.VERSION},
            "urls": [
                {
                    "filename": path.name,
                    "packagetype": kind,
                    "yanked": False,
                    "url": f"https://files.pythonhosted.org/packages/fixture/{path.name}",
                    "size": path.stat().st_size,
                    "digests": {"sha256": bundle.digest(path)},
                }
                for path, kind in ((self.core.wheel, "bdist_wheel"), (self.core.sdist, "sdist"))
            ],
        }

    def download(self, url: str, stream: BinaryIO, maximum: int) -> int:
        if url == f"https://pypi.org/pypi/nagents/{self.VERSION}/json":
            return stream.write(json.dumps(self.response_metadata or self.metadata()).encode())
        for path in (self.core.wheel, self.core.sdist):
            if url == f"https://files.pythonhosted.org/packages/fixture/{path.name}":
                return stream.write(path.read_bytes())
        raise AssertionError(f"Unexpected public URL: {url}")

    def test_both_source_distributions_match_git_without_build_hooks_or_imports(self) -> None:
        with patch.object(subprocess, "run") as execute:
            binding = bundle.verify_core_artifacts(self.core, version=self.VERSION, source_commit=self.COMMIT)
        execute.assert_not_called()
        self.assertEqual(binding["source_python_files"], 3)
        self.assertEqual(binding["wheel_sha256"], bundle.digest(self.core.wheel))
        self.assertEqual(binding["sdist_sha256"], bundle.digest(self.core.sdist))

    def test_official_download_fetches_exact_public_wheel_and_sdist_without_pip(self) -> None:
        with patch.object(bundle, "_download", side_effect=self.download), patch.object(subprocess, "run") as execute:
            result = bundle.download_published_core(
                self.root / "downloaded", version=self.VERSION, source_commit=self.COMMIT
            )
            bundle.verify_core_artifacts(result, version=self.VERSION, source_commit=self.COMMIT, published=True)
        execute.assert_not_called()
        self.assertEqual(result.wheel.read_bytes(), self.core.wheel.read_bytes())
        self.assertEqual(result.sdist.read_bytes(), self.core.sdist.read_bytes())

    def test_official_verification_rejects_public_hash_mismatch_for_either_asset(self) -> None:
        for index in (0, 1):
            metadata = self.metadata()
            entries = bundle._items(metadata["urls"])
            item = bundle._object(entries[index])
            item["digests"] = {"sha256": "0" * 64}
            entries[index] = item
            metadata["urls"] = entries
            self.response_metadata = metadata
            with (
                self.subTest(index=index),
                patch.object(bundle, "_download", side_effect=self.download),
                self.assertRaisesRegex(ValueError, "exact public PyPI bytes"),
            ):
                bundle.verify_core_artifacts(self.core, version=self.VERSION, source_commit=self.COMMIT, published=True)

    def test_public_metadata_requires_both_unyanked_unique_distributions_from_the_fixed_provider(self) -> None:
        for mutation in ("missing-wheel", "missing-sdist", "yanked", "duplicate", "foreign-url", "version"):
            metadata = self.metadata()
            entries = bundle._items(metadata["urls"])
            if mutation.startswith("missing-"):
                entries.pop(0 if mutation == "missing-wheel" else 1)
            elif mutation == "duplicate":
                entries.append(entries[1])
            elif mutation == "version":
                metadata["info"] = {"name": "nagents", "version": "0.100.1"}
            else:
                item = bundle._object(entries[1])
                if mutation == "yanked":
                    item["yanked"] = True
                else:
                    item["url"] = f"https://example.org/{self.core.sdist.name}"
                entries[1] = item
            metadata["urls"] = entries
            self.response_metadata = metadata
            with (
                self.subTest(mutation=mutation),
                patch.object(bundle, "_download", side_effect=self.download),
                self.assertRaises(ValueError),
            ):
                bundle.verify_core_artifacts(self.core, version=self.VERSION, source_commit=self.COMMIT, published=True)

    def test_wrong_wheel_name_version_and_module_bytes_are_rejected(self) -> None:
        metadata = f"nagents-{self.VERSION}.dist-info/METADATA"
        for changes in (
            {metadata: b"Name: other\nVersion: 0.100.2\n"},
            {metadata: b"Name: nagents\nVersion: 0.100.1\n"},
            {"nagents/__init__.py": b"# foreign implementation\n"},
        ):
            self.write_wheel(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                bundle.verify_core_artifacts(self.core, version=self.VERSION, source_commit=self.COMMIT)

    def test_sdist_metadata_source_and_archive_path_negative_cases(self) -> None:
        cases = (
            ({"PKG-INFO": b"Name: foreign\nVersion: 0.100.2\n"}, "", "", tarfile.REGTYPE),
            ({"PKG-INFO": b"Name: nagents\nVersion: 0.100.1\n"}, "", "", tarfile.REGTYPE),
            ({"pyproject.toml": self.project.replace(b"0.100.2", b"0.100.1")}, "", "", tarfile.REGTYPE),
            ({"src/nagents/__init__.py": b"# foreign code"}, "", "", tarfile.REGTYPE),
            ({}, f"nagents-{self.VERSION}/PKG-INFO", "", tarfile.REGTYPE),
            ({}, f"nagents-{self.VERSION}/../escape", "", tarfile.REGTYPE),
            ({}, "/absolute", "", tarfile.REGTYPE),
            ({}, "", "foreign", tarfile.REGTYPE),
            ({}, f"nagents-{self.VERSION}/link", "", tarfile.LNKTYPE),
            ({}, f"nagents-{self.VERSION}/link", "", tarfile.SYMTYPE),
        )
        for changes, extra, prefix, kind in cases:
            self.write_sdist(changes, extra=extra, root=prefix, kind=kind)
            with self.subTest(extra=extra, prefix=prefix, kind=kind, changes=changes), self.assertRaises(ValueError):
                bundle.verify_core_artifacts(self.core, version=self.VERSION, source_commit=self.COMMIT)

    def test_sdist_limits_are_enforced_without_extraction(self) -> None:
        for limits in (
            bundle.Limits(compressed=1),
            bundle.Limits(expanded=1),
            bundle.Limits(members=1),
            bundle.Limits(manifest=1),
        ):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                bundle._core_sdist(self.core.sdist, version=self.VERSION, source_commit=self.COMMIT, limits=limits)

    def embedded(self, *, mutation: str = "") -> Path:
        site = "runtime/lib/python3.13/site-packages/"
        contents = {site + name: data for name, data in self.wheel_files().items()}
        provenance: dict[str, object] = {
            "source_commit": self.COMMIT,
            "core": {
                "filename": self.core.wheel.name,
                "sha256": "0" * 64 if mutation == "wheel" else bundle.digest(self.core.wheel),
                "kind": "public-pypi-wheel",
            },
        }
        contents["notices/provenance.json"] = json.dumps(provenance).encode()
        files = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
        if mutation == "inventory":
            files[site + "nagents/__init__.py"] = "0" * 64
        if mutation == "provenance":
            files["notices/provenance.json"] = "0" * 64
        contents["bundle.json"] = json.dumps(
            {
                "source_commit": self.COMMIT,
                "nagents_version": self.VERSION,
                "official": mutation != "unofficial",
                "files": files,
            }
        ).encode()
        if mutation == "bytes":
            contents[site + "nagents/__init__.py"] = b"foreign embedded implementation"
        if mutation == "extra":
            contents[site + "nagents/injected.py"] = b"extra code"
        if mutation == "missing":
            del contents[site + "nagents/__init__.py"]
        path = self.root / "bundle.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data in contents.items():
                member = tarfile.TarInfo("ngn/" + name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return path

    def test_attached_wheel_is_bound_to_embedded_provenance_inventory_and_all_payload_bytes(self) -> None:
        bundle.verify_bundle_core(
            self.embedded(), self.core.wheel, version=self.VERSION, source_commit=self.COMMIT, official=True
        )
        for mutation in ("wheel", "bytes", "extra", "missing", "inventory", "provenance", "unofficial"):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                bundle.verify_bundle_core(
                    self.embedded(mutation=mutation),
                    self.core.wheel,
                    version=self.VERSION,
                    source_commit=self.COMMIT,
                    official=True,
                )


if __name__ == "__main__":
    unittest.main()
