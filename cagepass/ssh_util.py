"""SSH command helpers for non-interactive ph access and dedicated cage keys."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple


def build_hk_proxy_command(
    socks5_proxy: str,
    jump_host: str = "",
    target_addr: str = "",
) -> str:
    """Build an SSH ProxyCommand that routes through a local SOCKS5 proxy to a jump host."""
    return (
        f"/usr/bin/ssh -o ConnectTimeout=8 "
        f"-o 'ProxyCommand=/usr/bin/nc -X 5 -x {socks5_proxy} %h %p' "
        f"-W {target_addr} {jump_host}"
    )


def build_ph_ssh_cmd(
    ssh_host: str,
    remote_cmd: Optional[str] = None,
    extra_opts: Optional[List[str]] = None,
    connect_timeout: int = 10,
    socks5_proxy: str = "",
    jump_host: str = "",
    target_addr: str = "",
) -> List[str]:
    """Build a hardened, non-interactive SSH command to the remote host.

    - Disables RemoteCommand and TTY allocation.
    - Clears SendEnv so local LANG/LC_* never leaks to the remote host.
    - When `socks5_proxy`, `jump_host`, and `target_addr` are set, overrides ProxyCommand
      to route through the local SOCKS5 proxy -> jump host.
    """
    cmd: List[str] = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-o",
        "RemoteCommand=none",
        "-o",
        "RequestTTY=no",
        "-o",
        "SendEnv=-*",
    ]
    if socks5_proxy and jump_host and target_addr:
        cmd.extend(
            [
                "-o",
                f"ProxyCommand={build_hk_proxy_command(socks5_proxy, jump_host, target_addr)}",
            ]
        )
    if extra_opts:
        cmd.extend(extra_opts)
    cmd.append(ssh_host)
    if remote_cmd is not None:
        cmd.append(remote_cmd)
    return cmd


def ensure_cage_keypair(state_dir: Path) -> Tuple[Path, Path]:
    """Ensure a dedicated ED25519 keypair exists under `.state/keys/`.

    Never reuses the user's host `~/.ssh/id_*` keys.
    """
    keys_dir = state_dir / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(keys_dir, 0o700)

    priv_path = keys_dir / "cage_id_ed25519"
    pub_path = keys_dir / "cage_id_ed25519.pub"

    if not priv_path.exists() or not pub_path.exists():
        if priv_path.exists():
            priv_path.unlink()
        if pub_path.exists():
            pub_path.unlink()
        subprocess.run(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-f",
                str(priv_path),
                "-N",
                "",
                "-C",
                "cagepass-cage",
                "-q",
            ],
            check=True,
        )

    os.chmod(priv_path, 0o600)
    os.chmod(pub_path, 0o644)
    return priv_path, pub_path
