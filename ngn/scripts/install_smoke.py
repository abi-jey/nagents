"""Accept an actual bundled installation with no host language/build tools on PATH.

Uses only private fixture state and loopback providers, never a paid model or an
existing credential store. Run the script itself from an explicit development venv.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tarfile
from pathlib import Path
from typing import cast

from release import digest

SCRIPTS = Path(__file__).resolve().parent
IMPORT_PROBE = """import importlib.metadata as m, json, pathlib, ssl, sys
import certifi, nagents, ngn_nagents
from ngn_nagents.auth import available
print(json.dumps({
    'prefix': sys.prefix,
    'paths': [nagents.__file__, ngn_nagents.__file__],
    'core': m.version('nagents'), 'adapter': m.version('ngn-nagents'),
    'auth_available': available(), 'dont_write_bytecode': sys.dont_write_bytecode,
    'ca': certifi.where(), 'ca_count': ssl.create_default_context().cert_store_stats()['x509_ca'],
    'editable': [json.loads(d.read_text('direct_url.json') or '{}').get('dir_info', {}).get('editable', False)
                 for d in (m.distribution('nagents'), m.distribution('ngn-nagents'))]
}))
"""
SMOKE = """import runpy, sys
# Only checked-in fixture helpers, never a source core/adapter import override.
sys.path.insert(0, sys.argv[1])
sys.argv = [sys.argv[1] + '/smoke.py', '--binary', sys.argv[2], '--bundled-runtime']
runpy.run_path(sys.argv[0], run_name='__main__')
"""


def bundle_metadata(path: Path) -> dict[str, object]:
    with tarfile.open(path, "r:gz") as source:
        entry = source.getmember("ngn/bundle.json")
        if not entry.isfile() or not 0 < entry.size <= 8 * 1024 * 1024:
            raise ValueError("Invalid bundle manifest")
        stream = source.extractfile(entry)
        assert stream is not None
        value: object = json.loads(stream.read())
    if not isinstance(value, dict):
        raise ValueError("Bundle manifest must be an object")
    return cast("dict[str, object]", value)


def inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): f"link:{path.readlink()}" if path.is_symlink() else digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_symlink() or path.is_file()
    }


def execute(
    arguments: list[str], *, environment: dict[str, str], directory: Path, success: bool = True, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments, cwd=directory, env=environment, capture_output=True, text=True, timeout=timeout, check=False
    )
    if (result.returncode == 0) != success:
        raise AssertionError(
            f"Unexpected command exit {result.returncode}: {arguments}\n{result.stdout}\n{result.stderr}"
        )
    return result


def accept(installer: Path, bundle: Path, output: Path, *, upgrade_bundle: str = "") -> None:
    output.mkdir(mode=0o700)
    metadata = bundle_metadata(bundle)
    version = str(metadata["ngn_version"])
    prefix, bin_dir = output / "installation", output / "bin"
    home, temporary, workspace = output / "home", output / "tmp", output / "workspace"
    for directory in (home, temporary, workspace):
        directory.mkdir(mode=0o700)
    state = output / "protected-state"
    state.mkdir(mode=0o700)
    # Deliberately invalid, synthetic content: install/update must not parse auth
    # or initialize Host even when an explicit application config is unreadable.
    sentinels = {workspace / "valuable.txt": b"original workspace contents\n"}
    for name in ("ngn", "ngn-rust"):
        directory = state / name
        directory.mkdir(mode=0o700)
        (directory / "auth").mkdir(mode=0o700)
        sentinels[directory / "auth/openai.json"] = b"synthetic protected-state sentinel, not credentials\n"
        sentinels[directory / "conversation.db"] = b"synthetic conversation sentinel\n"
    for path, content in sentinels.items():
        path.write_bytes(content)
        path.chmod(0o600)
    state_before = inventory(state)
    environment = {"HOME": str(home), "TMPDIR": str(temporary), "PATH": "", "XDG_DATA_HOME": str(state)}
    checksum = digest(bundle)
    arguments = ["--bundle", str(bundle), "--sha256", checksum, "--version", version]
    installation = execute(
        [
            str(installer),
            "--config",
            str(output / "must-not-read.json"),
            "install",
            *arguments,
            "--prefix",
            str(prefix),
            "--bin-dir",
            str(bin_dir),
        ],
        environment=environment,
        directory=output,
    )
    (output / "install.log").write_text(installation.stdout + installation.stderr)
    command = bin_dir / "ngn"
    generation = prefix / "releases" / version
    runtime = generation / "runtime"
    python = runtime / "bin/python3"
    before = inventory(generation)
    probe_environment = {
        **environment,
        "SSL_CERT_FILE": str(generation / str(metadata["ca_file"])),
        "SSL_CERT_DIR": str(output / "no-system-certs"),
    }
    probe = execute(
        [str(python), "-I", "-B", "-c", IMPORT_PROBE],
        environment=probe_environment,
        directory=workspace,
    )
    runtime_info = json.loads(probe.stdout)
    assert Path(runtime_info["prefix"]).resolve() == runtime.resolve()
    assert all(Path(path).resolve().is_relative_to(runtime.resolve()) for path in runtime_info["paths"])
    assert runtime_info["core"] == metadata["nagents_version"]
    assert runtime_info["adapter"] == metadata["adapter_version"]
    assert runtime_info["auth_available"] and runtime_info["dont_write_bytecode"]
    assert not any(runtime_info["editable"])
    assert Path(runtime_info["ca"]).resolve().is_relative_to(runtime.resolve()) and runtime_info["ca_count"] > 0
    doctor = execute([str(command), "doctor"], environment=environment, directory=workspace)
    assert str(generation) in doctor.stdout and "runtime/bin/python3" in doctor.stdout
    (output / "doctor.log").write_text(doctor.stdout + doctor.stderr)
    checks = execute(
        [str(python), "-I", "-B", "-c", SMOKE, str(SCRIPTS), str(command)],
        environment=environment,
        directory=workspace,
        timeout=180,
    )
    (output / "smoke.log").write_text(checks.stdout + checks.stderr)
    assert len([line for line in checks.stdout.splitlines() if line.startswith("PASS:")]) == 8
    assert inventory(generation) == before, "Runtime execution changed the immutable release inventory"
    selected = (prefix / "current").readlink()
    for check_flag in (["--check"], []):
        result = execute(
            [str(command), "--config", str(output / "must-not-read.json"), "update", *arguments, *check_flag],
            environment=environment,
            directory=workspace,
        )
        with (output / "update.log").open("a") as log:
            log.write(result.stdout + result.stderr)
        assert (prefix / "current").readlink() == selected
    corrupt = output / "corrupt.tar.gz"
    with bundle.open("rb") as source:
        corrupt.write_bytes(source.read(128))
    execute(
        [str(command), "update", "--bundle", str(corrupt), "--sha256", checksum, "--version", version],
        environment=environment,
        directory=workspace,
        success=False,
    )
    assert (prefix / "current").readlink() == selected
    foreign_bin = output / "foreign-bin"
    foreign_bin.mkdir(mode=0o700)
    foreign = foreign_bin / "ngn"
    foreign.write_bytes(b"existing Python ngn must not be overwritten\n")
    execute(
        [
            str(installer),
            "install",
            *arguments,
            "--prefix",
            str(output / "other-prefix"),
            "--bin-dir",
            str(foreign_bin),
        ],
        environment=environment,
        directory=workspace,
        success=False,
    )
    assert foreign.read_bytes() == b"existing Python ngn must not be overwritten\n"
    unsafe = output / "unsafe"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o775)
    execute(
        [
            str(installer),
            "install",
            *arguments,
            "--prefix",
            str(unsafe / "prefix"),
            "--bin-dir",
            str(output / "other-bin"),
        ],
        environment=environment,
        directory=workspace,
        success=False,
    )
    assert unsafe.stat().st_mode & 0o777 == 0o775
    assert inventory(generation) == before
    upgraded = "not requested; cross-version update is covered by native unit tests"
    if upgrade_bundle:
        newer = Path(upgrade_bundle).absolute()
        newer_metadata = bundle_metadata(newer)
        newer_version = str(newer_metadata["ngn_version"])
        assert newer_version != version, "A real cross-version test needs a genuinely different built binary/bundle"
        execute(
            [str(command), "update", "--bundle", str(newer), "--sha256", digest(newer), "--version", newer_version],
            environment=environment,
            directory=workspace,
        )
        result = execute([str(command), "--version"], environment=environment, directory=workspace)
        assert result.stdout.strip() == f"ngn {newer_version}"
        assert inventory(generation) == before, "Updating modified a running generation's retained runtime"
        upgraded = newer_version
    for path, content in sentinels.items():
        assert path.read_bytes() == content, f"Installation/update changed protected state: {path}"
    assert inventory(state) == state_before, "Installation/update created or changed protected application state"
    evidence = {
        "version": version,
        "source_commit": metadata["source_commit"],
        "bundle_sha256": checksum,
        "binary_sha256": digest(generation / "ngn"),
        "prefix": str(prefix),
        "command": str(command),
        "runtime_python": str(python),
        "runtime": runtime_info,
        "suites_passed": 8,
        "path": "",
        "state_sentinels_unchanged": True,
        "release_inventory_unchanged": True,
        "cross_version_update": upgraded,
    }
    (output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print("PASS: real bundled first install, isolated imports/CA, eight CLI/HTTP/WS/PTY suites with empty PATH")
    print(
        "PASS: managed check/no-op, corrupt update, foreign binary and unsafe ancestry leave protected state unchanged"
    )
    print(f"Evidence: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--upgrade-bundle", default="")
    arguments = parser.parse_args()
    accept(
        arguments.installer.absolute(),
        arguments.bundle.absolute(),
        arguments.output.absolute(),
        upgrade_bundle=arguments.upgrade_bundle,
    )


if __name__ == "__main__":
    main()
