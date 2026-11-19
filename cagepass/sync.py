"""Host-side Mutagen multi-project bidirectional sync management."""

from __future__ import annotations

import shutil
import subprocess
from typing import List, Optional, Sequence

DEFAULT_IGNORES = (
    ".git/hooks",
    ".git/config",
    ".hg/hgrc",
    ".DS_Store",
)


def session_name(project_name: str) -> str:
    return f"cagepass-{project_name}"


def mutagen_installed() -> bool:
    return shutil.which("mutagen") is not None


def build_mutagen_create_cmd(
    project_name: str,
    local_work_dir: str,
    ssh_host: str,
    remote_dir: str,
    extra_ignores: Optional[Sequence[str]] = None,
) -> List[str]:
    """Build the `mutagen sync create` command for a single project's work/ dir."""
    cmd = [
        "mutagen",
        "sync",
        "create",
        f"--name={session_name(project_name)}",
        "--sync-mode=two-way-resolved",
    ]
    seen = set()
    for pat in list(DEFAULT_IGNORES) + list(extra_ignores or []):
        if pat not in seen:
            cmd.append(f"--ignore={pat}")
            seen.add(pat)

    cmd.extend([local_work_dir, f"{ssh_host}:{remote_dir}"])
    return cmd


def ensure_session(
    project_name: str,
    local_work_dir: str,
    ssh_host: str,
    remote_dir: str,
    extra_ignores: Optional[Sequence[str]] = None,
) -> bool:
    """Create or resume a Mutagen sync session for the project."""
    if not mutagen_installed():
        return False

    name = session_name(project_name)
    check = subprocess.run(
        ["mutagen", "sync", "list", name],
        capture_output=True,
        text=True,
    )
    if check.returncode == 0:
        subprocess.run(["mutagen", "sync", "resume", name], check=False)
        return True

    cmd = build_mutagen_create_cmd(
        project_name=project_name,
        local_work_dir=local_work_dir,
        ssh_host=ssh_host,
        remote_dir=remote_dir,
        extra_ignores=extra_ignores,
    )
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode == 0


def terminate_session(project_name: str) -> bool:
    """Terminate a project's Mutagen sync session if it exists."""
    if not mutagen_installed():
        return True
    name = session_name(project_name)
    res = subprocess.run(
        ["mutagen", "sync", "terminate", name],
        capture_output=True,
        text=True,
    )
    return res.returncode == 0
