from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path

import pytest

from client import skills_loader


SKILL_MD = b"""---
name: demo-skill
description: test skill
---
# Demo
"""


def _zip(entries: list[tuple[str, bytes]], *, symlink: str = "") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            if name == symlink:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, data)
            else:
                archive.writestr(name, data)
    return output.getvalue()


@pytest.fixture
def skill_dirs(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    users = tmp_path / "users"
    builtin.mkdir()
    monkeypatch.setattr(skills_loader, "SKILLS_DIR", builtin)
    monkeypatch.setattr(skills_loader, "USER_SKILLS_DIR", users)
    return users


def test_valid_nested_skill_zip_is_extracted(skill_dirs):
    doc = skills_loader.add_user_skill_zip(_zip([
        ("demo/SKILL.md", SKILL_MD),
        ("demo/docs/guide.md", b"guide"),
    ]))
    assert doc.slug == "demo-skill"
    assert (skill_dirs / "demo-skill" / "docs" / "guide.md").read_bytes() == b"guide"


@pytest.mark.parametrize("unsafe", [
    "demo/../../outside.txt",
    "/absolute.txt",
    "C:/windows.txt",
    "demo/../sibling.txt",
])
def test_zip_path_traversal_is_rejected(skill_dirs, unsafe):
    payload = _zip([
        ("demo/SKILL.md", SKILL_MD),
        (unsafe, b"escape"),
    ])
    with pytest.raises(ValueError, match="不安全路径"):
        skills_loader.add_user_skill_zip(payload)
    assert not (skill_dirs.parent / "outside.txt").exists()


def test_zip_symlink_is_rejected(skill_dirs):
    payload = _zip([
        ("demo/SKILL.md", SKILL_MD),
        ("demo/link", b"../../outside"),
    ], symlink="demo/link")
    with pytest.raises(ValueError, match="符号链接"):
        skills_loader.add_user_skill_zip(payload)


def test_zip_file_count_limit_is_enforced(skill_dirs, monkeypatch):
    monkeypatch.setattr(skills_loader, "MAX_SKILL_FILES", 1)
    payload = _zip([
        ("demo/SKILL.md", SKILL_MD),
        ("demo/guide.md", b"guide"),
    ])
    with pytest.raises(ValueError, match="文件数超过"):
        skills_loader.add_user_skill_zip(payload)


def test_zip_uncompressed_size_limit_is_enforced(skill_dirs, monkeypatch):
    monkeypatch.setattr(skills_loader, "MAX_SKILL_TOTAL_BYTES", len(SKILL_MD) + 3)
    payload = _zip([
        ("demo/SKILL.md", SKILL_MD),
        ("demo/guide.md", b"large"),
    ])
    with pytest.raises(ValueError, match="解压后超过"):
        skills_loader.add_user_skill_zip(payload)


def test_reupload_replaces_stale_files(skill_dirs):
    skills_loader.add_user_skill_zip(_zip([
        ("demo/SKILL.md", SKILL_MD),
        ("demo/stale.md", b"old"),
    ]))
    skills_loader.add_user_skill_zip(_zip([
        ("demo/SKILL.md", SKILL_MD),
        ("demo/current.md", b"new"),
    ]))
    root = skill_dirs / "demo-skill"
    assert not (root / "stale.md").exists()
    assert (root / "current.md").exists()
