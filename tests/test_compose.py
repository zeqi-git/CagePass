"""Tests for zq_vpc.config and zq_vpc.compose."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from zq_vpc.compose import generate_compose_dict
from zq_vpc.config import ConfigError, ZqVpcConfig, default_config, load_config, save_config


def test_default_config_uses_cagepass_remote_base_dir() -> None:
    cfg = default_config()
    assert cfg.remote.ssh_host == "my-remote-host"
    assert cfg.remote.base_dir == "~/cagepass_projects"
    assert cfg.cage.container_name == "local-cage"


def test_config_roundtrip_and_invalid_project_name_rejected(tmp_path: Path) -> None:
    cfg_path = tmp_path / "zq-vpc.yaml"
    cfg = default_config()
    cfg.add_project("proj-a")
    save_config(cfg, cfg_path)

    loaded = load_config(cfg_path)
    assert "proj-a" in loaded.projects
    assert loaded.projects["proj-a"].remote_dir == "~/cagepass_projects/proj-a/local"

    with pytest.raises(ConfigError):
        loaded.add_project("../escape")
    with pytest.raises(ConfigError):
        loaded.add_project("bad name with spaces")


def test_generate_compose_enforces_offline_nonroot_and_git_ro_overlays(tmp_path: Path) -> None:
    fake_home = tmp_path / "home" / "alice"
    repo_root = fake_home / "zq_vpc"
    (repo_root / "vault" / "proj-a" / "data").mkdir(parents=True)
    work_dir = repo_root / "vault" / "proj-a" / "work"
    (work_dir / ".git" / "hooks").mkdir(parents=True)
    (repo_root / ".state" / "keys").mkdir(parents=True)
    pub_key = repo_root / ".state" / "keys" / "cage_id_ed25519.pub"
    pub_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITEST dev@cage\n")

    cfg = default_config()
    cfg.add_project("proj-a")

    compose = generate_compose_dict(
        cfg,
        project_root=str(repo_root),
        home_dir=str(fake_home),
    )

    # 1. Network MUST be internal: true (zero internet egress)
    assert compose["networks"]["cage_net"]["internal"] is True

    svc = compose["services"]["local-cage"]
    # 2. Hardening assertions
    assert svc.get("privileged") is not True
    assert "no-new-privileges:true" in svc["security_opt"]
    assert "ALL" in svc["cap_drop"]
    assert svc["networks"] == ["cage_net"]
    assert "ports" not in svc

    volumes = svc["volumes"]
    # Never mount docker.sock
    assert not any("docker.sock" in v for v in volumes)

    # 3. Verify data is :ro, work is :rw, and .git is overlaid as :ro
    assert any(v.endswith(":/workspace/proj-a/data:ro") for v in volumes)
    assert any(v.endswith(":/workspace/proj-a/work:rw") for v in volumes)
    assert any(v.endswith(":/workspace/proj-a/work/.git:ro") for v in volumes)

    # Serializes cleanly to YAML
    dumped = yaml.safe_dump(compose)
    assert "local-cage" in dumped
