"""Tests for ssh_util, remote helpers, sync command building, and CLI workflows."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from zq_vpc.cli import main as cli_main
from zq_vpc.config import load_config
from zq_vpc.remote import build_claude_md_section, build_local_exec_script
from zq_vpc.ssh_util import build_ph_ssh_cmd, ensure_cage_keypair
from zq_vpc.sync import build_mutagen_create_cmd


def test_build_ph_ssh_cmd_disables_remote_command_tty_and_locale_leak() -> None:
    cmd = build_ph_ssh_cmd("my-remote-host", remote_cmd="echo ok")
    assert cmd[0] == "ssh"
    assert "RemoteCommand=none" in cmd
    assert "RequestTTY=no" in cmd
    assert "BatchMode=yes" in cmd
    # Must prevent local LANG/LC_* leaking
    assert "SendEnv=-*" in cmd
    assert cmd[-2] == "my-remote-host"
    assert cmd[-1] == "echo ok"


def test_build_ph_ssh_cmd_supports_local_socks5_hk_jump() -> None:
    cmd = build_ph_ssh_cmd(
        "my-remote-host",
        remote_cmd="echo ok",
        socks5_proxy="127.0.0.1:7890",
        jump_host="my-jump-host",
        target_addr="127.0.0.1:2222",
    )
    assert any(
        arg.startswith("ProxyCommand=") and "127.0.0.1:7890" in arg and "my-jump-host" in arg
        for arg in cmd
    )


def test_ensure_cage_keypair_creates_0600_ed25519_key(tmp_path: Path) -> None:
    priv, pub = ensure_cage_keypair(tmp_path)
    assert priv.exists()
    assert pub.exists()
    assert (priv.stat().st_mode & 0o777) == 0o600
    assert pub.read_text().startswith("ssh-ed25519 ")


def test_build_mutagen_create_cmd_ignores_git_hooks_and_targets_remote_dir() -> None:
    cmd = build_mutagen_create_cmd(
        project_name="proj-a",
        local_work_dir="/tmp/example-workspace/vault/proj-a/work",
        ssh_host="my-remote-host",
        remote_dir="~/cagepass_projects/proj-a/local",
        extra_ignores=["*.parquet"],
    )
    assert cmd[:3] == ["mutagen", "sync", "create"]
    assert "--name=zq-vpc-proj-a" in cmd
    assert "--sync-mode=two-way-resolved" in cmd
    assert "--ignore=.git/hooks" in cmd
    assert "--ignore=.git/config" in cmd
    assert "--ignore=*.parquet" in cmd
    assert "my-remote-host:~/cagepass_projects/proj-a/local" in cmd


def test_remote_helpers_generate_claude_md_and_local_exec() -> None:
    md = build_claude_md_section("proj-a", "~/cagepass_projects/proj-a/local")
    assert "proj-a" in md
    assert "./local-exec" in md
    assert "/workspace/proj-a/data" in md

    script = build_local_exec_script("~/cagepass_run/cage.sock", "~/cagepass_run/cage_id_ed25519")
    assert script.startswith("#!/usr/bin/env bash")
    assert "cage.sock" in script
    assert "StrictHostKeyChecking=no" in script


def test_cli_init_project_add_and_put_flow(tmp_path: Path) -> None:
    fake_home = tmp_path / "home" / "alice"
    repo_root = fake_home / "cagepass"
    repo_root.mkdir(parents=True)

    # 1. init
    rc = cli_main(["--root", str(repo_root), "--home", str(fake_home), "init"])
    assert rc == 0
    cfg_path = repo_root / "zq-vpc.yaml"
    assert cfg_path.exists()
    assert (repo_root / ".state" / "keys" / "cage_id_ed25519").exists()

    # 2. project add proj-a
    rc = cli_main(["--root", str(repo_root), "--home", str(fake_home), "project", "add", "proj-a"])
    assert rc == 0
    cfg = load_config(cfg_path)
    assert "proj-a" in cfg.projects
    assert cfg.projects["proj-a"].remote_dir == "~/cagepass_projects/proj-a/local"
    assert (repo_root / "vault" / "proj-a" / "data").is_dir()
    assert (repo_root / "vault" / "proj-a" / "work").is_dir()

    # 3. zq-vpc put normal file to --ro
    sample = fake_home / "raw.csv"
    sample.write_text("col1,col2\n1,2\n")
    rc = cli_main(
        ["--root", str(repo_root), "--home", str(fake_home), "put", str(sample), "-p", "proj-a", "--ro"]
    )
    assert rc == 0
    assert (repo_root / "vault" / "proj-a" / "data" / "raw.csv").read_text() == "col1,col2\n1,2\n"

    # 4. zq-vpc put blocks sensitive .env unless --force
    secret = fake_home / ".env"
    secret.write_text("SECRET=1\n")
    rc = cli_main(
        ["--root", str(repo_root), "--home", str(fake_home), "put", str(secret), "-p", "proj-a", "--rw"]
    )
    assert rc != 0
    assert not (repo_root / "vault" / "proj-a" / "work" / ".env").exists()

    # 5. zq-vpc project stop proj-a disables it without deleting vault files
    with mock.patch("zq_vpc.sync.terminate_session", return_value=True):
        rc = cli_main(["--root", str(repo_root), "--home", str(fake_home), "project", "stop", "proj-a"])
    assert rc == 0
    cfg = load_config(cfg_path)
    assert cfg.projects["proj-a"].enabled is False
    assert (repo_root / "vault" / "proj-a" / "data" / "raw.csv").exists()
