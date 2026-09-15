# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

"""Build a HACS release from tracked component files, resolving README links."""

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import subprocess
import zipfile
from pathlib import Path

COMPONENT = Path("custom_components/aqara_p3")
VERSION = re.compile(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+|-(?:alpha|beta|rc)\.\d+)?")
REQUIRED = {
    "__init__.py",
    "manifest.json",
    "README.md",
    "LICENSE",
    "native/p3lan.c",
    "native/p3lan-helper",
    "native/LICENSE.musl",
    "protocol/native_info.py",
}


def build_release(root: Path, tag: str, output: Path):
    root = root.resolve()
    component = root / COMPONENT
    manifest = json.loads((component / "manifest.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    if (
        manifest.get("domain") != "aqara_p3"
        or not VERSION.fullmatch(version)
        or tag not in (version, "v" + version)
    ):
        raise ValueError("Tag must match manifest.json version (optional v prefix)")

    tracked = subprocess.check_output(
        ["git", "ls-files", "-z", "--", COMPONENT.as_posix()], cwd=root
    ).decode("utf-8")
    files = {}
    for name in sorted(filter(None, tracked.split("\0"))):
        path = root / name
        relative = path.relative_to(component)
        if "__pycache__" in relative.parts or path.suffix in (".pyc", ".pyo"):
            continue
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError(f"Release file must resolve inside repository: {name}")
        # Reading the file dereferences symlinks; ZIP entries are ordinary files.
        files[relative.as_posix()] = resolved.read_bytes()
    if missing := REQUIRED - files.keys():
        raise ValueError(f"Required release files are not tracked: {sorted(missing)}")
    # Source remains in Git and the release's source archive, not the install ZIP.
    del files["native/p3lan.c"]

    info = ast.parse(files["protocol/native_info.py"].decode("utf-8-sig"))
    expected = next(
        ast.literal_eval(node.value)
        for node in info.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "SHA256" for t in node.targets)
    )
    helper = files["native/p3lan-helper"]
    if (
        not helper.startswith(b"\x7fELF")
        or hashlib.sha256(helper).hexdigest() != expected
    ):
        raise ValueError("Native helper checksum mismatch; rebuild before release")

    output.mkdir(parents=True, exist_ok=True)
    archive = output / "aqara_p3.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for name, data in files.items():
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            mode = 0o755 if name == "native/p3lan-helper" else 0o644
            entry.external_attr = (stat.S_IFREG | mode) << 16
            zip_file.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED)
    checksum = archive.with_suffix(".zip.sha256")
    checksum.write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n",
        encoding="ascii",
    )
    return archive, not re.fullmatch(r"\d+\.\d+\.\d+", version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    archive, prerelease = build_release(
        Path(__file__).resolve().parents[1], args.tag, args.output
    )
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as output:
            output.write(f"prerelease={str(prerelease).lower()}\n")
    print(f"Created {archive} ({archive.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
