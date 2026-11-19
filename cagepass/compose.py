"""Docker Compose generator for the isolated offline local-cage container."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from cagepass.config import CagePassConfig
from cagepass.safety import find_protected_vcs_dirs, validate_mount_path


def _add_vcs_ro_overlays(
    host_rw_dir: str,
    container_rw_dir: str,
    volumes: List[str],
) -> None:
    """Overlay any .git/.hg/.sl/.jj inside a :rw mount as :ro."""
    real_host = os.path.realpath(host_rw_dir)
    for vcs_path in find_protected_vcs_dirs(real_host):
        rel = os.path.relpath(vcs_path, real_host)
        container_target = os.path.join(container_rw_dir, rel).replace(os.sep, "/")
        volumes.append(f"{vcs_path}:{container_target}:ro")


def generate_compose_dict(
    cfg: CagePassConfig,
    project_root: str,
    home_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate all mounts and build a hardened docker-compose dictionary."""
    root = Path(os.path.realpath(project_root))
    pub_key = root / ".state" / "keys" / "cage_id_ed25519.pub"

    volumes: List[str] = []
    if pub_key.exists():
        volumes.append(f"{pub_key}:/home/dev/.ssh/authorized_keys:ro")

    for name, proj in cfg.projects.items():
        if not proj.enabled:
            continue

        data_dir = root / "vault" / name / "data"
        work_dir = root / "vault" / name / "work"
        data_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)

        real_data = validate_mount_path(str(data_dir), project_root=str(root), home_dir=home_dir)
        real_work = validate_mount_path(str(work_dir), project_root=str(root), home_dir=home_dir)

        volumes.append(f"{real_data}:/workspace/{name}/data:ro")
        volumes.append(f"{real_work}:/workspace/{name}/work:rw")
        _add_vcs_ro_overlays(real_work, f"/workspace/{name}/work", volumes)

        for idx, extra_ro in enumerate(proj.extra_ro):
            real_extra = validate_mount_path(extra_ro, project_root=str(root), home_dir=home_dir)
            base_label = Path(real_extra).name or f"extra_ro_{idx}"
            volumes.append(f"{real_extra}:/workspace/{name}/extra_ro/{base_label}:ro")

        for idx, extra_rw in enumerate(proj.extra_rw):
            real_extra = validate_mount_path(extra_rw, project_root=str(root), home_dir=home_dir)
            base_label = Path(real_extra).name or f"extra_rw_{idx}"
            container_extra = f"/workspace/{name}/extra_rw/{base_label}"
            volumes.append(f"{real_extra}:{container_extra}:rw")
            _add_vcs_ro_overlays(real_extra, container_extra, volumes)

    dockerfile_dir = str(root / "docker")

    return {
        "networks": {
            "cage_net": {
                "driver": "bridge",
                "internal": True,
            }
        },
        "services": {
            "local-cage": {
                "build": {
                    "context": dockerfile_dir,
                    "dockerfile": "Dockerfile.cage",
                },
                "image": cfg.cage.image,
                "container_name": cfg.cage.container_name,
                "hostname": "local-cage",
                "user": "dev",
                "networks": ["cage_net"],
                "security_opt": ["no-new-privileges:true"],
                "cap_drop": ["ALL"],
                "volumes": volumes,
                "restart": "unless-stopped",
            }
        },
    }


def write_compose_file(
    cfg: CagePassConfig,
    project_root: str,
    home_dir: Optional[str] = None,
) -> Path:
    """Generate and write .state/docker-compose.generated.yml."""
    compose = generate_compose_dict(cfg, project_root=project_root, home_dir=home_dir)
    out_path = Path(project_root) / ".state" / "docker-compose.generated.yml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        yaml.safe_dump(compose, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return out_path
