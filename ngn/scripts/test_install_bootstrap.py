"""Exercise the POSIX bootstrap without network, Python or package-manager fallback."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


class BootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.version = tomllib.loads((INSTALLER.parent / "Cargo.toml").read_text())["workspace"]["package"]["version"]
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.calls = self.root / "calls"
        self.invocation = self.root / "invocation"
        self.environment = {
            "PATH": str(self.tools),
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.scratch),
            "DOWNLOADS": str(self.downloads),
            "CALLS": str(self.calls),
            "INVOCATION": str(self.invocation),
        }
        for name in ("mktemp", "rm", "rmdir", "sha256sum", "chmod", "cat", "cp"):
            source = shutil.which(name)
            assert source, f"Test utility missing: {name}"
            (self.tools / name).symlink_to(source)
        self.tool("uname", '#!/bin/sh\ncase "$1" in -s) printf "Linux\\n" ;; -m) printf "x86_64\\n" ;; esac\n')
        self.tool(
            "curl",
            """#!/bin/sh
printf '%s\n' "$*" >> "$CALLS"
output=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --output) output=$2; shift 2 ;;
        *) url=$1; shift ;;
    esac
done
cp "$DOWNLOADS/${url##*/}" "$output"
""",
        )
        payload = b'#!/bin/sh\nprintf "%s\\n" "$@" > "$INVOCATION"\n'
        (self.downloads / "ngn-installer-linux-x86_64").write_bytes(payload)
        self.checksum(hashlib.sha256(payload).hexdigest())

    def tool(self, name: str, content: str) -> None:
        path = self.tools / name
        path.write_text(content)
        path.chmod(0o700)

    def checksum(self, checksum: str, *, duplicate: bool = False) -> None:
        line = f"{checksum}  ngn-installer-linux-x86_64\n"
        (self.downloads / "SHA256SUMS").write_text(line * (2 if duplicate else 1))

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", "-s", "--", *arguments],
            env=self.environment,
            cwd=self.root,
            input=INSTALLER.read_text(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_verified_native_installer_receives_exact_quoted_paths_without_python_or_tar(self) -> None:
        for name in ("python", "python3", "pip", "cargo", "node", "tar"):
            self.assertNotIn(name, {entry.name for entry in self.tools.iterdir()})
        result = self.invoke("--prefix", str(self.root / "prefix with spaces"), "--bin-dir", str(self.root / "bin"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.invocation.read_text().splitlines(),
            [
                "install",
                "--version",
                self.version,
                "--prefix",
                str(self.root / "prefix with spaces"),
                "--bin-dir",
                str(self.root / "bin"),
            ],
        )
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertIn("--proto =https --proto-redir =https --tlsv1.2", call)
            self.assertIn(f"https://github.com/abi-jey/nagents/releases/download/v{self.version}/", call)
        self.assertFalse(list(self.scratch.iterdir()))

    def test_corrupt_payload_is_not_executed_and_temporary_files_are_removed(self) -> None:
        self.checksum("0" * 64)
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertFalse(self.invocation.exists())
        self.assertFalse(list(self.scratch.iterdir()))

    def test_verbose_reports_public_assets_and_reaches_native_installer(self) -> None:
        result = self.invoke("--verbose")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"release v{self.version}, platform linux-x86_64", result.stderr)
        for asset in ("SHA256SUMS", "ngn-installer-linux-x86_64"):
            self.assertIn(f"/download/v{self.version}/{asset}", result.stderr)
        self.assertEqual(self.invocation.read_text().splitlines()[-1], "--verbose")
        self.assertNotIn(str(self.scratch), result.stderr)

    def test_duplicate_or_malformed_installer_hash_is_rejected_before_payload_download(self) -> None:
        for checksum, duplicate in (("g" * 64, False), ("a" * 63, False), ("a" * 64, True)):
            with self.subTest(checksum=checksum, duplicate=duplicate):
                self.calls.unlink(missing_ok=True)
                self.checksum(checksum, duplicate=duplicate)
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.invocation.exists())
                self.assertEqual(len(self.calls.read_text().splitlines()), 1)
                self.assertFalse(list(self.scratch.iterdir()))

    def test_unsupported_platform_is_rejected_without_downloading(self) -> None:
        for system, machine in (("FreeBSD", "amd64"), ("Linux", "aarch64"), ("Darwin", "ppc")):
            with self.subTest(system=system, machine=machine):
                self.uname(system, machine)
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("validated for Linux x86_64 and macOS", result.stderr)
                self.assertFalse(self.calls.exists())

    def uname(self, system: str, machine: str) -> None:
        self.tool("uname", f'#!/bin/sh\ncase "$1" in -s) printf "{system}\\n" ;; -m) printf "{machine}\\n" ;; esac\n')

    def test_macos_selects_the_native_installer_for_its_architecture(self) -> None:
        payload = b'#!/bin/sh\nprintf "%s\\n" "$@" > "$INVOCATION"\n'
        checksum = hashlib.sha256(payload).hexdigest()
        for machine, translated, platform in (
            ("arm64", "0", "macos-arm64"),
            ("x86_64", "0", "macos-x86_64"),
            # An x86_64 shell under Rosetta 2 still installs the native Apple silicon build.
            ("x86_64", "1", "macos-arm64"),
        ):
            with self.subTest(machine=machine, translated=translated):
                self.calls.unlink(missing_ok=True)
                self.invocation.unlink(missing_ok=True)
                self.uname("Darwin", machine)
                self.tool("sysctl", f'#!/bin/sh\nprintf "{translated}\\n"\n')
                (self.downloads / f"ngn-installer-{platform}").write_bytes(payload)
                (self.downloads / "SHA256SUMS").write_text(
                    f"{'0' * 64}  ngn-installer-linux-x86_64\n{checksum}  ngn-installer-{platform}\n"
                )
                result = self.invoke()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(self.calls.read_text().splitlines()[-1].endswith(f"/ngn-installer-{platform}"))
                self.assertEqual(self.invocation.read_text().splitlines()[:3], ["install", "--version", self.version])

    def test_no_hash_or_download_tool_is_not_a_reason_to_skip_verification(self) -> None:
        (self.tools / "sha256sum").unlink()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sha256sum or shasum", result.stderr)
        self.assertFalse(self.calls.exists())
        (self.tools / "curl").unlink()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("native ngn-installer", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_help_has_no_network_or_home_requirement(self) -> None:
        self.environment.pop("HOME")
        self.environment["PATH"] = os.devnull
        result = self.invoke("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No Python", result.stdout)
        self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
