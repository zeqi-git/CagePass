"""Command-line interface for cagepass."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from cagepass import remote, sync, tunnel
from cagepass.compose import write_compose_file
from cagepass.config import (
    ConfigError,
    default_config,
    load_config,
    save_config,
    validate_project_name,
)
from cagepass.safety import SafetyError, inspect_put_source
from cagepass.ssh_util import ensure_cage_keypair


def _default_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _cmd_init(root: Path) -> int:
    cfg_path = root / "cagepass.yaml"
    (root / "vault").mkdir(parents=True, exist_ok=True)
    state_dir = root / ".state"
    state_dir.mkdir(parents=True, exist_ok=True)

    ensure_cage_keypair(state_dir)

    if cfg_path.exists():
        print(f"[init] Config already exists: {cfg_path}")
    else:
        cfg = default_config()
        save_config(cfg, cfg_path)
        print(f"[init] Created config: {cfg_path} (remote base_dir={cfg.remote.base_dir})")
    print(f"[init] Dedicated cage SSH keypair ready in {state_dir / 'keys'}")
    return 0


def _cmd_project_add(root: Path, name: str, remote_dir: Optional[str]) -> int:
    cfg_path = root / "cagepass.yaml"
    cfg = load_config(cfg_path)
    proj = cfg.add_project(name, remote_dir=remote_dir)
    save_config(cfg, cfg_path)

    (root / "vault" / name / "data").mkdir(parents=True, exist_ok=True)
    (root / "vault" / name / "work").mkdir(parents=True, exist_ok=True)
    print(f"[project] Added '{name}':")
    print(f"  local ro data : {root / 'vault' / name / 'data'}")
    print(f"  local rw work : {root / 'vault' / name / 'work'}")
    print(f"  remote synced : {cfg.remote.ssh_host}:{proj.remote_dir}")
    return 0


def _cmd_project_stop(root: Path, name: str) -> int:
    cfg_path = root / "cagepass.yaml"
    cfg = load_config(cfg_path)
    if name not in cfg.projects:
        print(f"[project] Unknown project: {name}", file=sys.stderr)
        return 1
    cfg.projects[name].enabled = False
    save_config(cfg, cfg_path)
    sync.terminate_session(name)
    print(f"[project] Stopped '{name}' (sync terminated, vault files preserved).")
    return 0


def _cmd_project_start(root: Path, name: str) -> int:
    cfg_path = root / "cagepass.yaml"
    cfg = load_config(cfg_path)
    if name not in cfg.projects:
        print(f"[project] Unknown project: {name}", file=sys.stderr)
        return 1
    cfg.projects[name].enabled = True
    save_config(cfg, cfg_path)
    print(f"[project] Enabled '{name}'. Run 'cagepass up' to apply mounts and sync.")
    return 0


def _cmd_project_list(root: Path) -> int:
    cfg = load_config(root / "cagepass.yaml")
    if not cfg.projects:
        print("No projects configured. Use 'cagepass project add <name>'.")
        return 0
    for name, proj in cfg.projects.items():
        state = "enabled" if proj.enabled else "stopped"
        print(f"- {name} [{state}] -> {cfg.remote.ssh_host}:{proj.remote_dir}")
    return 0


def _cmd_put(
    root: Path,
    src: str,
    project: str,
    mode: str,
    force: bool,
) -> int:
    validate_project_name(project)
    cfg = load_config(root / "cagepass.yaml")
    if project not in cfg.projects:
        print(f"[put] Project '{project}' not found. Run 'cagepass project add {project}' first.", file=sys.stderr)
        return 1

    audit = inspect_put_source(src)
    if audit.has_blockers:
        print("[put] BLOCKED: escaping symlinks detected:", file=sys.stderr)
        for s in audit.escaping_symlinks:
            print(f"  - {s}", file=sys.stderr)
        return 2

    if audit.has_warnings and not force:
        print("[put] BLOCKED: potentially sensitive files detected (re-run with --force if intentional):", file=sys.stderr)
        for s in audit.sensitive_files:
            print(f"  - {s}", file=sys.stderr)
        return 3

    target_subdir = "data" if mode == "ro" else "work"
    dest_dir = root / "vault" / project / target_subdir
    dest_dir.mkdir(parents=True, exist_ok=True)

    src_path = Path(os.path.expanduser(src))
    dest_path = dest_dir / src_path.name

    if src_path.is_dir():
        if dest_path.exists():
            shutil.rmtree(dest_path)
        shutil.copytree(src_path, dest_path, symlinks=True)
    else:
        shutil.copy2(src_path, dest_path)

    print(f"[put] Staged '{src_path.name}' -> {dest_path} ({mode})")
    return 0


def _cmd_up(root: Path, home_dir: Optional[str], local_only: bool) -> int:
    cfg = load_config(root / "cagepass.yaml")
    state_dir = root / ".state"
    priv_key, _ = ensure_cage_keypair(state_dir)

    compose_path = write_compose_file(cfg, project_root=str(root), home_dir=home_dir)
    print(f"[up] Generated hardened compose: {compose_path}")

    res = subprocess.run(
        ["docker", "compose", "-f", str(compose_path), "up", "-d", "--build"],
        check=False,
    )
    if res.returncode != 0:
        print("[up] Failed to start local-cage container. Is OrbStack/Docker running?", file=sys.stderr)
        return res.returncode

    bridge_pid = tunnel.start_bridge(
        state_dir=state_dir,
        container_name=cfg.cage.container_name,
        container_port=cfg.cage.ssh_port,
        bind_port=cfg.cage.local_bridge_port,
        default_timeout=cfg.cage.exec_timeout,
    )
    print(f"[up] Local cage bridge running (pid={bridge_pid}, 127.0.0.1:{cfg.cage.local_bridge_port}, timeout={cfg.cage.exec_timeout}s)")

    if local_only:
        print("[up] --local-only mode: skipped overseas tunnel/sync.")
        return 0

    enabled_projects = {
        name: p.remote_dir for name, p in cfg.projects.items() if p.enabled
    }

    ok_remote = remote.provision_remote_user_space(
        ssh_host=cfg.remote.ssh_host,
        remote_socket=cfg.remote.socket_path,
        local_priv_key=priv_key,
        projects=enabled_projects,
        socks5_proxy=cfg.remote.socks5_proxy,
        jump_host=cfg.remote.jump_host,
        target_addr=cfg.remote.target_addr,
        default_timeout=cfg.cage.exec_timeout,
    )
    if not ok_remote:
        print(f"[up] WARNING: could not reach '{cfg.remote.ssh_host}'. Local cage is up; retry 'cagepass up' when overseas VPN is online.", file=sys.stderr)
        return 1

    print(f"[up] Provisioned ./local-exec (file-RPC) & CLAUDE.md on '{cfg.remote.ssh_host}' (zero overseas keys/tunnels)")

    if not sync.mutagen_installed():
        print("[up] WARNING: 'mutagen' not installed. Run: brew install mutagen-io/mutagen/mutagen", file=sys.stderr)
    else:
        for name, proj in cfg.projects.items():
            if not proj.enabled:
                continue
            work_dir = str(root / "vault" / name / "work")
            ok = sync.ensure_session(
                project_name=name,
                local_work_dir=work_dir,
                ssh_host=cfg.remote.ssh_host,
                remote_dir=proj.remote_dir,
                extra_ignores=proj.ignore,
            )
            status_str = "synced" if ok else "failed"
            print(f"[up] Mutagen session '{name}': {status_str}")

    return 0


def _cmd_down(root: Path) -> int:
    cfg = load_config(root / "cagepass.yaml")
    state_dir = root / ".state"

    for name in cfg.projects:
        sync.terminate_session(name)
    tunnel.stop_tunnel_and_bridge(state_dir)

    compose_path = state_dir / "docker-compose.generated.yml"
    if compose_path.exists():
        subprocess.run(
            ["docker", "compose", "-f", str(compose_path), "down"],
            check=False,
        )
    print("[down] Stopped local-cage, bridge, reverse tunnel, and Mutagen sync sessions.")
    return 0


def _cmd_status(root: Path) -> int:
    cfg = load_config(root / "cagepass.yaml")
    tstat = tunnel.tunnel_status(root / ".state")
    print(f"Remote host : {cfg.remote.ssh_host} (base_dir={cfg.remote.base_dir})")
    print(f"Bridge PID  : {tstat['bridge_pid'] or 'stopped'}")
    print(f"Tunnel PID  : {tstat['tunnel_pid'] or 'stopped'}")
    print(f"Mutagen     : {'installed' if sync.mutagen_installed() else 'NOT installed'}")
    docker_res = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", cfg.cage.container_name],
        capture_output=True,
        text=True,
    )
    cstatus = docker_res.stdout.strip() if docker_res.returncode == 0 else "not running"
    print(f"Container   : {cfg.cage.container_name} ({cstatus})")
    return 0


def _cmd_shell(root: Path, project: Optional[str]) -> int:
    cfg = load_config(root / "cagepass.yaml")
    workdir = f"/workspace/{project}" if project else "/workspace"
    res = subprocess.run(
        ["docker", "exec", "-it", "-w", workdir, cfg.cage.container_name, "/bin/bash"],
        check=False,
    )
    return res.returncode


def _cmd_doctor(root: Path) -> int:
    cfg = load_config(root / "cagepass.yaml")
    print("=== cagepass doctor ===")

    # 1. Docker daemon
    d_info = subprocess.run(["docker", "info"], capture_output=True, text=True)
    print(f"[{'OK' if d_info.returncode == 0 else 'FAIL'}] Docker daemon reachable")

    # 2. Mutagen
    print(f"[{'OK' if sync.mutagen_installed() else 'WARN'}] Mutagen installed locally")

    # 3. Offline egress check inside local-cage
    if d_info.returncode == 0:
        egress = subprocess.run(
            [
                "docker",
                "exec",
                cfg.cage.container_name,
                "python3",
                "-c",
                "import urllib.request; urllib.request.urlopen('https://1.1.1.1', timeout=2)",
            ],
            capture_output=True,
            text=True,
        )
        if egress.returncode != 0:
            print("[OK] local-cage internet egress blocked (offline isolation verified)")
        else:
            print("[FAIL] local-cage reached public internet! Check cage_net internal:true")

    # 4. Overseas hygiene check
    hyg = remote.check_overseas_hygiene(
        cfg.remote.ssh_host,
        socks5_proxy=cfg.remote.socks5_proxy,
        jump_host=cfg.remote.jump_host,
        target_addr=cfg.remote.target_addr,
    )
    if hyg.get("reachable") == "yes":
        print(f"[OK] Overseas '{cfg.remote.ssh_host}' reachable")
        print(hyg.get("raw", ""))
    else:
        print(f"[WARN] Overseas '{cfg.remote.ssh_host}' unreachable: {hyg.get('error', '')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cagepass", description="Local isolated Docker cage & overseas bridge")
    parser.add_argument("--root", default=str(_default_root()), help="Path to cagepass repository root")
    parser.add_argument("--home", default=None, help="Override home directory (for testing)")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Initialize cagepass.yaml, vault/, and dedicated SSH keypair")

    p_proj = sub.add_parser("project", help="Manage multi-project slots")
    p_proj_sub = p_proj.add_subparsers(dest="proj_cmd", required=True)
    p_add = p_proj_sub.add_parser("add", help="Add a new project slot")
    p_add.add_argument("name", help="Project name")
    p_add.add_argument("--remote-dir", default=None, help="Override remote local/ directory")
    p_stop = p_proj_sub.add_parser("stop", help="Stop sync & disable project")
    p_stop.add_argument("name", help="Project name")
    p_start = p_proj_sub.add_parser("start", help="Re-enable a stopped project")
    p_start.add_argument("name", help="Project name")
    p_proj_sub.add_parser("list", help="List configured projects")

    p_put = sub.add_parser("put", help="Safely stage a file or directory into a project vault")
    p_put.add_argument("src", help="Local file or directory to stage")
    p_put.add_argument("-p", "--project", required=True, help="Target project name")
    mode_grp = p_put.add_mutually_exclusive_group(required=True)
    mode_grp.add_argument("--ro", action="store_true", help="Stage into read-only data/")
    mode_grp.add_argument("--rw", action="store_true", help="Stage into read-write work/")
    p_put.add_argument("--force", action="store_true", help="Allow staging sensitive files (e.g. .env)")

    p_up = sub.add_parser("up", help="Start local-cage, bridge, tunnel, and Mutagen sync")
    p_up.add_argument("--local-only", action="store_true", help="Start only local Docker cage & bridge")

    sub.add_parser("down", help="Stop sync, tunnel, bridge, and local-cage")
    sub.add_parser("status", help="Show local-cage, tunnel, and sync status")

    p_sh = sub.add_parser("shell", help="Open bash inside local-cage")
    p_sh.add_argument("project", nargs="?", default=None, help="Optional project directory")

    sub.add_parser("doctor", help="Run security, offline isolation, and overseas hygiene checks")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path(os.path.expanduser(args.root)).resolve()

    try:
        if args.command == "init":
            return _cmd_init(root)
        if args.command == "project":
            if args.proj_cmd == "add":
                return _cmd_project_add(root, args.name, args.remote_dir)
            if args.proj_cmd == "stop":
                return _cmd_project_stop(root, args.name)
            if args.proj_cmd == "start":
                return _cmd_project_start(root, args.name)
            if args.proj_cmd == "list":
                return _cmd_project_list(root)
        if args.command == "put":
            return _cmd_put(root, args.src, args.project, "ro" if args.ro else "rw", args.force)
        if args.command == "up":
            return _cmd_up(root, args.home, args.local_only)
        if args.command == "down":
            return _cmd_down(root)
        if args.command == "status":
            return _cmd_status(root)
        if args.command == "shell":
            return _cmd_shell(root, args.project)
        if args.command == "doctor":
            return _cmd_doctor(root)
    except (ConfigError, SafetyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
