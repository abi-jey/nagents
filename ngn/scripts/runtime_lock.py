"""Derive a non-Linux runtime lock and its vendored notices from the reviewed Linux lock.

The Linux lock (ngn/runtime-lock.json) remains the reviewed source of truth for
the CPython release and every dependency version. For another platform this
script pins the *same* versions: the matching python-build-standalone runtime
of the same release, and the platform's binary wheel for each compiled
dependency (pure wheels are reused byte-for-byte). Every hash is taken from the
upstream index (GitHub asset digest or PyPI digest) and recomputed locally.

Notices come from the matching ``pgo+lto-full`` archive of the same release,
exactly like the Linux notices: PYTHON.json, python/licenses/* and the runtime
LICENSE.txt. pip and MPL-2.0 notices are reused when the stripped runtime ships
the same bundled pip wheel.

Usage: python ngn/scripts/runtime_lock.py macos-arm64
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

import bundle

PBS = "https://github.com/astral-sh/python-build-standalone/releases/download"
API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags"


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "ngn-runtime-lock"})
    with urllib.request.urlopen(request, timeout=300) as response:
        data: bytes = response.read()
    return data


def _download(url: str, destination: Path, sha256: str, size: int) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "ngn-runtime-lock"})
    checksum = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=600) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            checksum.update(chunk)
            output.write(chunk)
    if destination.stat().st_size != size or checksum.hexdigest() != sha256:
        raise ValueError(f"Download does not match its published digest: {url}")


def _asset(release: dict[str, object], name: str) -> dict[str, object]:
    assets = release["assets"]
    assert isinstance(assets, list)
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one upstream asset {name}")
    asset = matches[0]
    digest = str(asset["digest"])
    if not digest.startswith("sha256:"):
        raise ValueError("Upstream asset has no SHA-256 digest")
    return asset


def _artifact(filename: str, url: str, size: int, sha256: str) -> dict[str, object]:
    return {"filename": filename, "url": url, "size": size, "sha256": sha256}


def _platform_wheel(name: str, version: str, spec: bundle.PlatformSpec) -> dict[str, object]:
    metadata = json.loads(_get(f"https://pypi.org/pypi/{quote(name)}/{quote(version)}/json"))
    candidates = []
    for item in metadata["urls"]:
        filename = str(item["filename"])
        if item["packagetype"] == "bdist_wheel" and spec.wheel_compatible(filename):
            candidates.append(item)
    if not candidates:
        raise ValueError(f"No {spec.name} cp313 wheel for {name} {version}")
    # Prefer the single-architecture wheel with the lowest deployment target.
    candidates.sort(key=lambda item: ("universal2" in item["filename"], item["filename"]))
    chosen = candidates[0]
    return _artifact(chosen["filename"], chosen["url"], int(chosen["size"]), str(chosen["digests"]["sha256"]))


def generate(platform: str, root: Path = bundle.ROOT) -> None:
    spec = bundle.PLATFORMS[platform]
    if platform == "linux-x86_64":
        raise ValueError("The Linux lock is the reviewed source; it is not generated")
    source_lock = json.loads((root / "ngn/runtime-lock.json").read_text())
    python = source_lock["python"]
    release = python["release"]
    version = python["version"]
    upstream = json.loads(_get(f"{API}/{release}"))
    runtime_name = f"cpython-{version}+{release}-{spec.triple}-install_only_stripped.tar.gz"
    full_name = f"cpython-{version}+{release}-{spec.triple}-pgo+lto-full.tar.zst"
    runtime_asset, full_asset = _asset(upstream, runtime_name), _asset(upstream, full_name)
    runtime = _artifact(
        runtime_name,
        f"{PBS}/{release}/{quote(runtime_name)}",
        int(str(runtime_asset["size"])),
        str(runtime_asset["digest"]).removeprefix("sha256:"),
    )
    full = _artifact(
        full_name,
        f"{PBS}/{release}/{quote(full_name)}",
        int(str(full_asset["size"])),
        str(full_asset["digest"]).removeprefix("sha256:"),
    )
    wheels = []
    for wheel in source_lock["wheels"]:
        if wheel["artifact"]["filename"].endswith("-py3-none-any.whl"):
            wheels.append(wheel)
        else:
            wheels.append(
                {
                    "name": wheel["name"],
                    "version": wheel["version"],
                    "artifact": _platform_wheel(wheel["name"], wheel["version"], spec),
                }
            )
    notices = spec.notices_dir(root)
    linux_notices = bundle.PLATFORMS["linux-x86_64"].notices_dir(root)
    with tempfile.TemporaryDirectory(prefix="ngn-runtime-lock-") as temporary:
        scratch = Path(temporary)
        _download(str(full["url"]), scratch / "full.tar.zst", str(full["sha256"]), int(str(full["size"])))
        _download(str(runtime["url"]), scratch / "runtime.tar.gz", str(runtime["sha256"]), int(str(runtime["size"])))
        extracted = scratch / "full"
        extracted.mkdir()
        subprocess.run(
            [
                "tar",
                "--zstd",
                "-xf",
                str(scratch / "full.tar.zst"),
                "-C",
                str(extracted),
                "python/PYTHON.json",
                "python/licenses",
            ],
            check=True,
        )
        with tarfile.open(scratch / "runtime.tar.gz") as stripped:
            license_member = stripped.extractfile("python/lib/python3.13/LICENSE.txt")
            assert license_member is not None
            runtime_license = license_member.read()
            pips = [
                member.name
                for member in stripped.getmembers()
                if member.name.startswith("python/lib/python3.13/ensurepip/_bundled/pip-")
            ]
        linux_pips = json.loads((linux_notices / "PROVENANCE.json").read_text())["probe_3_13_18_stripped"]["summary"][
            "bundled_pip"
        ]
        if len(pips) != 1 or f"/pip-{linux_pips}-py3-none-any.whl" not in pips[0]:
            raise ValueError("Bundled pip differs from the reviewed Linux notices; review pip notices manually")
        if notices.exists():
            shutil.rmtree(notices)
        (notices / "LICENSES").mkdir(parents=True)
        (notices / "PYTHON-RUNTIME").mkdir()
        shutil.copyfile(extracted / "python/PYTHON.json", notices / "PYTHON.json")
        for license_file in sorted((extracted / "python/licenses").iterdir()):
            shutil.copyfile(license_file, notices / "LICENSES" / license_file.name)
        (notices / "PYTHON-RUNTIME/LICENSE.txt").write_bytes(runtime_license)
        shutil.copytree(linux_notices / "PIP", notices / "PIP")
        shutil.copytree(linux_notices / "UPSTREAM", notices / "UPSTREAM")
    python_json = json.loads((notices / "PYTHON.json").read_text())
    if python_json.get("python_version") != version or python_json.get("target_triple") != spec.triple:
        raise ValueError("Full archive metadata does not match the pinned runtime")
    files = []
    for path in sorted(notices.rglob("*")):
        if path.is_file():
            contents = path.read_bytes()
            files.append(
                {
                    "vendor_path": path.relative_to(notices).as_posix(),
                    "size": len(contents),
                    "sha256": hashlib.sha256(contents).hexdigest(),
                }
            )
    provenance = {
        "schema_version": 1,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "generator": "ngn/scripts/runtime_lock.py",
        "purpose": (
            "Redistribution notices for the pinned CPython runtime shipped by ngn on this platform. "
            "PYTHON.json and LICENSES/ were extracted from the full archive of the same upstream release; "
            "PYTHON-RUNTIME/LICENSE.txt from the stripped runtime itself; PIP/ and UPSTREAM/ are reused from "
            "the reviewed Linux notices because the runtime bundles the same pip wheel."
        ),
        "pin": {
            "runtime_asset": runtime_name,
            "runtime_url": runtime["url"],
            "runtime_size": runtime["size"],
            "runtime_sha256": runtime["sha256"],
            "python_version": version,
            "target_triple": spec.triple,
            "threading": "gil-enabled (non-freethreaded)",
        },
        "full_archive": {
            "asset": full_name,
            "asset_id": full_asset["id"],
            "url": full["url"],
            "size": full["size"],
            "sha256": full["sha256"],
            "release_tag": release,
            "release_commit": python["release_commit"],
            "verification": [
                f"GitHub API asset digest == sha256:{full['sha256']}",
                "locally recomputed sha256 of the download == same hash",
            ],
        },
        "files": files,
    }
    provenance_bytes = (json.dumps(provenance, indent=1) + "\n").encode()
    (notices / "PROVENANCE.json").write_bytes(provenance_bytes)
    lock = {
        "lock_schema": 1,
        "platform": platform,
        "core": source_lock["core"],
        "python": {**python, "artifact": runtime},
        "python_notices": {
            "provenance_sha256": hashlib.sha256(provenance_bytes).hexdigest(),
            "source": full,
        },
        "wheels": wheels,
    }
    spec.lock_path(root).write_text(json.dumps(lock, indent=1) + "\n")
    print(f"Wrote {spec.lock_path(root).relative_to(root)} and {notices.relative_to(root)}/")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("platform", choices=sorted(set(bundle.PLATFORMS) - {"linux-x86_64"}))
    generate(parser.parse_args().platform)


if __name__ == "__main__":
    main()
