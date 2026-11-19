"""Configuration schema, validation, and YAML persistence for cagepass."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


class ConfigError(ValueError):
    """Raised when cagepass.yaml or a project configuration is invalid."""


def validate_project_name(name: str) -> str:
    if not name or not PROJECT_NAME_RE.match(name):
        raise ConfigError(
            f"Invalid project name '{name}'. Must match {PROJECT_NAME_RE.pattern} "
            "(letters, digits, '_' or '-', starting with letter/digit)."
        )
    return name


@dataclass
class RemoteConfig:
    ssh_host: str = "my-remote-host"
    base_dir: str = "~/cagepass_projects"
    socket_path: str = "~/cagepass_run/cage.sock"
    socks5_proxy: str = ""
    jump_host: str = ""
    target_addr: str = ""


@dataclass
class CageConfig:
    container_name: str = "local-cage"
    image: str = "cagepass-cage:latest"
    ssh_port: int = 2222
    local_bridge_port: int = 22220


@dataclass
class ProjectConfig:
    name: str
    remote_dir: str
    extra_ro: List[str] = field(default_factory=list)
    extra_rw: List[str] = field(default_factory=list)
    ignore: List[str] = field(default_factory=list)
    enabled: bool = True
    isolated: bool = False


@dataclass
class CagePassConfig:
    remote: RemoteConfig = field(default_factory=RemoteConfig)
    cage: CageConfig = field(default_factory=CageConfig)
    projects: Dict[str, ProjectConfig] = field(default_factory=dict)

    def add_project(
        self,
        name: str,
        remote_dir: Optional[str] = None,
        extra_ro: Optional[List[str]] = None,
        extra_rw: Optional[List[str]] = None,
        ignore: Optional[List[str]] = None,
        isolated: bool = False,
    ) -> ProjectConfig:
        validate_project_name(name)
        base = self.remote.base_dir.rstrip("/")
        target_remote = remote_dir or f"{base}/{name}/local"
        proj = ProjectConfig(
            name=name,
            remote_dir=target_remote,
            extra_ro=list(extra_ro or []),
            extra_rw=list(extra_rw or []),
            ignore=list(ignore or []),
            enabled=True,
            isolated=isolated,
        )
        self.projects[name] = proj
        return proj

    def to_dict(self) -> Dict[str, Any]:
        return {
            "remote": {
                "ssh_host": self.remote.ssh_host,
                "base_dir": self.remote.base_dir,
                "socket_path": self.remote.socket_path,
                "socks5_proxy": self.remote.socks5_proxy,
                "jump_host": self.remote.jump_host,
                "target_addr": self.remote.target_addr,
            },
            "cage": {
                "container_name": self.cage.container_name,
                "image": self.cage.image,
                "ssh_port": self.cage.ssh_port,
                "local_bridge_port": self.cage.local_bridge_port,
            },
            "projects": {
                name: {
                    "remote_dir": p.remote_dir,
                    "extra_ro": p.extra_ro,
                    "extra_rw": p.extra_rw,
                    "ignore": p.ignore,
                    "enabled": p.enabled,
                    "isolated": p.isolated,
                }
                for name, p in self.projects.items()
            },
        }


def default_config() -> CagePassConfig:
    return CagePassConfig()


def load_config(path: Path) -> CagePassConfig:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}. Run 'cagepass init' first.")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Config file must be a YAML mapping: {path}")

    remote_raw = raw.get("remote") or {}
    cage_raw = raw.get("cage") or {}
    projects_raw = raw.get("projects") or {}

    remote = RemoteConfig(
        ssh_host=str(remote_raw.get("ssh_host", "my-remote-host")),
        base_dir=str(remote_raw.get("base_dir", "~/cagepass_projects")),
        socket_path=str(remote_raw.get("socket_path", "~/cagepass_run/cage.sock")),
        socks5_proxy=str(remote_raw.get("socks5_proxy", "")),
        jump_host=str(remote_raw.get("jump_host", "")),
        target_addr=str(remote_raw.get("target_addr", "")),
    )
    cage = CageConfig(
        container_name=str(cage_raw.get("container_name", "local-cage")),
        image=str(cage_raw.get("image", "cagepass-cage:latest")),
        ssh_port=int(cage_raw.get("ssh_port", 2222)),
        local_bridge_port=int(cage_raw.get("local_bridge_port", 22220)),
    )

    cfg = CagePassConfig(remote=remote, cage=cage)
    for name, pdata in projects_raw.items():
        validate_project_name(str(name))
        pdata = pdata or {}
        base = remote.base_dir.rstrip("/")
        cfg.projects[name] = ProjectConfig(
            name=name,
            remote_dir=str(pdata.get("remote_dir") or f"{base}/{name}/local"),
            extra_ro=[str(x) for x in (pdata.get("extra_ro") or [])],
            extra_rw=[str(x) for x in (pdata.get("extra_rw") or [])],
            ignore=[str(x) for x in (pdata.get("ignore") or [])],
            enabled=bool(pdata.get("enabled", True)),
            isolated=bool(pdata.get("isolated", False)),
        )
    return cfg


def save_config(cfg: CagePassConfig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(cfg.to_dict(), sort_keys=False, allow_unicode=True)
    path.write_text(rendered, encoding="utf-8")
