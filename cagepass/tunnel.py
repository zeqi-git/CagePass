"""Host-side local bridge and reverse SSH tunnel to the offline local-cage."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from cagepass.ssh_util import build_ph_ssh_cmd


def _pid_alive(pid_file: Path) -> Optional[int]:
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        return None


def _stop_pid(pid_file: Path) -> None:
    pid = _pid_alive(pid_file)
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if pid_file.exists():
        pid_file.unlink()


def process_rpc_request(req_file: Path, project_name: str, container_name: str) -> None:
    """Execute a single RPC request inside the offline local-cage container."""
    try:
        payload = json.loads(req_file.read_text(encoding="utf-8"))
        req_id = str(payload.get("id", ""))
        cmd = str(payload.get("cmd", ""))
    except Exception:
        req_file.unlink(missing_ok=True)
        return

    res_file = req_file.parent / f"res_{req_id}.json"
    tmp_file = req_file.parent / f".tmp_res_{req_id}.json"

    try:
        proc = subprocess.run(
            [
                "docker",
                "exec",
                "-w",
                f"/workspace/{project_name}/work",
                container_name,
                "/bin/bash",
                "-lc",
                cmd,
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        result = {
            "id": req_id,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired:
        result = {
            "id": req_id,
            "returncode": 124,
            "stdout": "",
            "stderr": "[local-exec] Command timed out after 600s.\n",
        }
    except Exception as exc:
        result = {
            "id": req_id,
            "returncode": 1,
            "stdout": "",
            "stderr": f"[local-exec] Bridge error: {exc}\n",
        }

    tmp_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    tmp_file.replace(res_file)
    req_file.unlink(missing_ok=True)


def _rpc_watcher_loop(vault_dir: Path, container_name: str) -> None:
    """Poll vault/*/work/.cagepass_rpc/req_*.json and execute inside local-cage."""
    in_flight = set()
    while True:
        try:
            if vault_dir.is_dir():
                for proj_dir in vault_dir.iterdir():
                    rpc_dir = proj_dir / "work" / ".cagepass_rpc"
                    if not rpc_dir.is_dir():
                        continue
                    for req_file in sorted(rpc_dir.glob("req_*.json")):
                        key = str(req_file)
                        if key in in_flight:
                            continue
                        in_flight.add(key)

                        def _run(rf: Path = req_file, pn: str = proj_dir.name, k: str = key) -> None:
                            try:
                                process_rpc_request(rf, pn, container_name)
                            finally:
                                in_flight.discard(k)

                        threading.Thread(target=_run, daemon=True).start()
        except Exception:
            pass
        time.sleep(0.3)


def run_bridge_server(
    container_name: str,
    container_port: int,
    bind_port: int,
    vault_dir: Optional[str] = None,
) -> None:
    """Serve local TCP bridge + vault file-RPC watcher for the offline local-cage."""
    if vault_dir:
        threading.Thread(
            target=_rpc_watcher_loop,
            args=(Path(vault_dir), container_name),
            daemon=True,
        ).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", bind_port))
    srv.listen(16)

    def handle(conn: socket.socket) -> None:
        proc = subprocess.Popen(
            [
                "docker",
                "exec",
                "-i",
                container_name,
                "socat",
                "STDIO",
                f"TCP:127.0.0.1:{container_port}",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        def sock_to_proc() -> None:
            try:
                assert proc.stdin is not None
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    proc.stdin.write(data)
                    proc.stdin.flush()
            except OSError:
                pass
            finally:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                except OSError:
                    pass

        def proc_to_sock() -> None:
            try:
                assert proc.stdout is not None
                fd = proc.stdout.fileno()
                while True:
                    data = os.read(fd, 65536)
                    if not data:
                        break
                    conn.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    conn.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t1 = threading.Thread(target=sock_to_proc, daemon=True)
        t2 = threading.Thread(target=proc_to_sock, daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        proc.terminate()
        conn.close()

    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
    finally:
        srv.close()


def start_bridge(
    state_dir: Path,
    container_name: str,
    container_port: int,
    bind_port: int,
) -> int:
    """Start the background loopback bridge + file-RPC watcher to the offline container."""
    pid_file = state_dir / "bridge.pid"
    existing = _pid_alive(pid_file)
    if existing is not None:
        return existing

    vault_dir = state_dir.parent / "vault"
    log_file = (state_dir / "bridge.log").open("ab")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cagepass.tunnel",
            container_name,
            str(container_port),
            str(bind_port),
            str(vault_dir),
        ],
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )
    pid_file.write_text(str(proc.pid))
    time.sleep(0.3)
    return proc.pid


def start_reverse_tunnel(
    state_dir: Path,
    ssh_host: str,
    remote_socket: str,
    local_bridge_port: int,
    socks5_proxy: str = "",
    jump_host: str = "",
    target_addr: str = "",
) -> int:
    """Start a host-side reverse SSH tunnel (reusing ph-proxy or local SOCKS5 -> HK jump)."""
    pid_file = state_dir / "tunnel.pid"
    existing = _pid_alive(pid_file)
    if existing is not None:
        return existing

    # Resolve remote ~ to absolute path (OpenSSH -R does not expand ~) and clean stale socket
    prep = subprocess.run(
        build_ph_ssh_cmd(
            ssh_host,
            remote_cmd=(
                f'S="{remote_socket}"; S="${{S/#\\~/$HOME}}"; '
                'mkdir -p "$(dirname "$S")" && rm -f "$S" && printf "%s" "$S"'
            ),
            socks5_proxy=socks5_proxy,
            jump_host=jump_host,
            target_addr=target_addr,
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    resolved_socket = prep.stdout.strip() if prep.returncode == 0 and prep.stdout.strip() else remote_socket

    cmd = build_ph_ssh_cmd(
        ssh_host,
        remote_cmd=None,
        extra_opts=[
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-R",
            f"{resolved_socket}:127.0.0.1:{local_bridge_port}",
        ],
        socks5_proxy=socks5_proxy,
        jump_host=jump_host,
        target_addr=target_addr,
    )
    log_file = (state_dir / "tunnel.log").open("ab")
    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )
    pid_file.write_text(str(proc.pid))
    return proc.pid


def stop_tunnel_and_bridge(state_dir: Path) -> None:
    _stop_pid(state_dir / "tunnel.pid")
    _stop_pid(state_dir / "bridge.pid")


def tunnel_status(state_dir: Path) -> dict:
    return {
        "bridge_pid": _pid_alive(state_dir / "bridge.pid"),
        "tunnel_pid": _pid_alive(state_dir / "tunnel.pid"),
    }


if __name__ == "__main__":
    if len(sys.argv) >= 4:
        vdir = sys.argv[4] if len(sys.argv) >= 5 else None
        run_bridge_server(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), vault_dir=vdir)
