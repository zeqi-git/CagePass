"""Tests for zq_vpc.safety (path validation, VCS protection, put auditing)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from zq_vpc.safety import (
    SafetyError,
    find_protected_vcs_dirs,
    inspect_put_source,
    validate_mount_path,
)


def test_blocks_root_and_home_and_sensitive_dirs(tmp_path: Path) -> None:
    fake_home = tmp_path / "home" / "alice"
    fake_home.mkdir(parents=True)
    ssh_dir = fake_home / ".ssh"
    ssh_dir.mkdir()
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    repo_root = fake_home / "zq_vpc"
    state_dir = repo_root / ".state"
    state_dir.mkdir(parents=True)

    # Root, parent of home, home itself, ~/.ssh, ~/.claude, .state must all be blocked
    for bad in [
        Path("/"),
        tmp_path,
        tmp_path / "home",
        fake_home,
        ssh_dir,
        ssh_dir / "keys",
        claude_dir,
        state_dir,
        state_dir / "keys",
        repo_root,
    ]:
        with pytest.raises(SafetyError):
            validate_mount_path(str(bad), project_root=str(repo_root), home_dir=str(fake_home))


def test_allows_safe_project_subdir_inside_home(tmp_path: Path) -> None:
    fake_home = tmp_path / "home" / "alice"
    repo_root = fake_home / "zq_vpc"
    safe_vault = repo_root / "vault" / "proj-a" / "data"
    safe_dataset = fake_home / "datasets" / "sales"
    safe_vault.mkdir(parents=True)
    safe_dataset.mkdir(parents=True)

    # Should NOT raise
    validate_mount_path(str(safe_vault), project_root=str(repo_root), home_dir=str(fake_home))
    validate_mount_path(str(safe_dataset), project_root=str(repo_root), home_dir=str(fake_home))


def test_blocks_symlink_escaping_to_ssh_or_home(tmp_path: Path) -> None:
    fake_home = tmp_path / "home" / "alice"
    ssh_dir = fake_home / ".ssh"
    ssh_dir.mkdir(parents=True)
    repo_root = fake_home / "zq_vpc"
    repo_root.mkdir(parents=True)

    sneaky_link = fake_home / "datasets" / "harmless_name"
    sneaky_link.parent.mkdir(parents=True)
    os.symlink(ssh_dir, sneaky_link)

    with pytest.raises(SafetyError):
        validate_mount_path(str(sneaky_link), project_root=str(repo_root), home_dir=str(fake_home))


def test_case_insensitive_blocking_on_macos_style_paths(tmp_path: Path) -> None:
    fake_home = tmp_path / "home" / "alice"
    (fake_home / ".ssh").mkdir(parents=True)
    repo_root = fake_home / "zq_vpc"
    (repo_root / ".state").mkdir(parents=True)

    with pytest.raises(SafetyError):
        validate_mount_path(
            str(fake_home / ".SSH"),
            project_root=str(repo_root),
            home_dir=str(fake_home),
        )
    with pytest.raises(SafetyError):
        validate_mount_path(
            str(repo_root / ".STATE"),
            project_root=str(repo_root),
            home_dir=str(fake_home),
        )


def test_find_protected_vcs_dirs_detects_git_hg_and_mixed_case(tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / ".git" / "hooks").mkdir(parents=True)
    (work / "subrepo" / ".Git").mkdir(parents=True)
    (work / "mercurial" / ".hg").mkdir(parents=True)
    (work / "normal" / "src").mkdir(parents=True)

    found = find_protected_vcs_dirs(str(work))
    rel_found = sorted(os.path.relpath(p, work) for p in found)
    assert rel_found == sorted([".git", os.path.join("subrepo", ".Git"), os.path.join("mercurial", ".hg")])


def test_inspect_put_source_flags_sensitive_files_and_external_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret")

    src = tmp_path / "bundle"
    src.mkdir()
    (src / "data.csv").write_text("a,b\n1,2\n")
    (src / ".env").write_text("API_KEY=123\n")
    (src / "id_ed25519").write_text("PRIVATE KEY")
    os.symlink(outside, src / "escape_link")

    audit = inspect_put_source(str(src))
    assert len(audit.escaping_symlinks) == 1
    assert any(p.endswith(".env") for p in audit.sensitive_files)
    assert any(p.endswith("id_ed25519") for p in audit.sensitive_files)
