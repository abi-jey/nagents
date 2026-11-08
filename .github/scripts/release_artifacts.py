"""Bind distribution metadata and bytes to an immutable release candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path


def artifact_version(path: Path) -> str:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ValueError("Wheel must contain exactly one METADATA")
            data = archive.read(names[0])
    else:
        with tarfile.open(path, "r:gz") as archive:
            members = [
                member
                for member in archive.getmembers()
                if len(Path(member.name).parts) == 2 and member.name.endswith("/PKG-INFO")
            ]
            if len(members) != 1:
                raise ValueError("Source distribution must contain exactly one root PKG-INFO")
            stream = archive.extractfile(members[0])
            if stream is None:
                raise ValueError("Missing source metadata")
            data = stream.read()
    metadata = BytesParser().parsebytes(data)
    if metadata.get("Name") != "nagents" or not metadata.get("Version"):
        raise ValueError("Unexpected distribution metadata")
    return str(metadata["Version"])


def manifest(directory: Path, sha: str, version: str) -> dict[str, object]:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Release SHA must be a full commit hash")
    paths = sorted(directory.iterdir())
    if (
        len(paths) != 2
        or sum(path.suffix == ".whl" for path in paths) != 1
        or sum(path.name.endswith(".tar.gz") for path in paths) != 1
    ):
        raise ValueError("Expected exactly one wheel and one source distribution")
    hashes = {}
    for path in paths:
        if path.is_symlink() or artifact_version(path) != version:
            raise ValueError("Artifact version does not match release version")
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"sha": sha, "version": version, "artifacts": hashes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("create", "verify"))
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    expected = manifest(args.dist, args.sha, args.version)
    if args.mode == "create":
        args.manifest.write_text(json.dumps(expected, sort_keys=True) + "\n", encoding="utf-8")
    elif json.loads(args.manifest.read_text(encoding="utf-8")) != expected:
        raise ValueError("Release provenance does not match candidate or artifact bytes")


if __name__ == "__main__":
    main()
