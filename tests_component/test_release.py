# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

import hashlib
import json
import stat
import zipfile
from pathlib import Path

import pytest

from scripts.package_release import build_release


@pytest.fixture
def release_repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    component = root / "custom_components/aqara_p3"
    component.mkdir(parents=True)
    (root / "README.md").write_text("# Test integration\n")
    helper = b"\x7fELFtest helper"
    files = {
        "__init__.py": b"",
        "manifest.json": json.dumps(
            {"domain": "aqara_p3", "version": "0.4.5"}
        ).encode(),
        "LICENSE": b"Project license",
        "native/p3lan.c": b"/* source */",
        "native/p3lan-helper": helper,
        "native/local_mode.sh": b"#!/bin/sh\n",
        "native/LICENSE.musl": b"Third-party license",
        "protocol/native_info.py": f'SHA256 = "{hashlib.sha256(helper).hexdigest()}"'.encode(),
    }
    for name, data in files.items():
        path = component / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (component / "README.md").symlink_to("../../README.md")
    tracked = [f"custom_components/aqara_p3/{name}" for name in [*files, "README.md"]]

    def git_files(args, cwd):
        assert args == ["git", "ls-files", "-z", "--", "custom_components/aqara_p3"]
        assert cwd == root
        return ("\0".join(tracked) + "\0").encode()

    monkeypatch.setattr("scripts.package_release.subprocess.check_output", git_files)
    # Local files and firmware must not enter an integration release.
    (component / "private.local.json").write_text('{"password":"private"}')
    (root / "fw").mkdir()
    (root / "fw/device.bin").write_bytes(b"firmware")
    return root


def test_release_contents_symlinks_checksum_and_reproducibility(release_repo, tmp_path):
    archive, prerelease = build_release(release_repo, "v0.4.5", tmp_path / "out")
    assert prerelease is False
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        assert set(z.namelist()) == {
            "__init__.py",
            "manifest.json",
            "README.md",
            "LICENSE",
            "native/p3lan-helper",
            "native/local_mode.sh",
            "native/LICENSE.musl",
            "protocol/native_info.py",
        }
        assert z.read("README.md") == (release_repo / "README.md").read_bytes()
        assert all(stat.S_ISREG(f.external_attr >> 16) for f in z.infolist())
        assert z.getinfo("native/p3lan-helper").external_attr >> 16 & 0o777 == 0o755
    checksum = archive.with_suffix(".zip.sha256").read_text()
    assert (
        checksum
        == f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  aqara_p3.zip\n"
    )
    again, _ = build_release(release_repo, "0.4.5", tmp_path / "again")
    assert archive.read_bytes() == again.read_bytes()


def test_release_rejects_wrong_tag_before_creating_archive(release_repo, tmp_path):
    with pytest.raises(ValueError, match="Tag must match"):
        build_release(release_repo, "v0.4.6", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_release_rejects_corrupt_helper(release_repo, tmp_path):
    (release_repo / "custom_components/aqara_p3/native/p3lan-helper").write_bytes(
        b"bad"
    )
    with pytest.raises(ValueError, match="checksum mismatch"):
        build_release(release_repo, "v0.4.5", tmp_path / "out")


def test_release_rejects_external_symlink(release_repo, tmp_path):
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    readme = release_repo / "custom_components/aqara_p3/README.md"
    readme.unlink()
    readme.symlink_to(outside)
    with pytest.raises(ValueError, match="inside repository"):
        build_release(release_repo, "v0.4.5", tmp_path / "out")


@pytest.mark.parametrize("version", ["0.4.6b1", "0.4.6-rc.1"])
def test_prerelease(release_repo, tmp_path, version):
    manifest = release_repo / "custom_components/aqara_p3/manifest.json"
    manifest.write_text(json.dumps({"domain": "aqara_p3", "version": version}))
    _, prerelease = build_release(release_repo, "v" + version, tmp_path / "out")
    assert prerelease is True


def test_hacs_asset_matches_workflow():
    import yaml

    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "hacs.json").read_text())
    assert config["zip_release"] and config["filename"] == "aqara_p3.zip"
    # BaseLoader keeps the YAML key 'on' a string (GitHub uses YAML 1.2).
    workflow = yaml.load(
        (root / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert workflow["on"] == {"push": {"tags": ["**"]}}
    steps = workflow["jobs"]["release"]["steps"]
    assert "scripts/package_release.py" in steps[1]["run"]
    assert "aqara_p3.zip" in steps[2]["run"]
