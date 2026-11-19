"""Zero-sudo overseas user-space helpers (local-exec, CLAUDE.md, hygiene check)."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from typing import Dict

from zq_vpc.ssh_util import build_ph_ssh_cmd

MANAGED_START = "<!-- zq-vpc:managed:start -->"
MANAGED_END = "<!-- zq-vpc:managed:end -->"


def build_claude_md_section(project_name: str, remote_local_dir: str) -> str:
    """Build the CLAUDE.md instruction block for a remote project (container-compatible)."""
    return f"""{MANAGED_START}
## Local Docker Bridge (`{project_name}`)

- **Small files / code (bidirectional sync)**: Read and write directly under `./local/` (synced in real time with the user's local `vault/{project_name}/work`).
- **Large / private datasets (local-only, never copied to remote)**: Raw data stays ONLY on the user's local machine inside the isolated offline container at `/workspace/{project_name}/data` (read-only).
- **Run scripts locally inside the offline container** (works directly from inside sandboxed remote containers):
  ```bash
  ./local-exec "python3 your_script.py"
  ```
  Scripts execute in `/workspace/{project_name}/work` (which mirrors `./local/`) with `pandas`, `duckdb`, and `pyarrow` pre-installed, and can read `/workspace/{project_name}/data` at local disk speed. Any output written to `/workspace/{project_name}/work/` automatically syncs back to `./local/`.
{MANAGED_END}
"""


def build_project_local_exec_script() -> str:
    """Build the self-contained project-root `./local-exec` file-RPC runner.

    Works inside sandboxed remote containers with zero SSH or host-network dependencies.
    """
    return """#!/usr/bin/env python3
import json
import os
import sys
import time
import uuid
from pathlib import Path

if len(sys.argv) < 2:
    print("Usage: ./local-exec <command...>", file=sys.stderr)
    sys.exit(1)

cmd = " ".join(sys.argv[1:])
base = Path(__file__).resolve().parent / "local" / ".zq_rpc"
base.mkdir(parents=True, exist_ok=True)

req_id = uuid.uuid4().hex[:12]
tmp_req = base / f".tmp_req_{req_id}.json"
req_file = base / f"req_{req_id}.json"
res_file = base / f"res_{req_id}.json"

tmp_req.write_text(json.dumps({"id": req_id, "cmd": cmd}, ensure_ascii=False), encoding="utf-8")
tmp_req.replace(req_file)

deadline = time.time() + 600
while time.time() < deadline:
    if res_file.exists():
        try:
            data = json.loads(res_file.read_text(encoding="utf-8"))
        except Exception:
            time.sleep(0.1)
            continue
        res_file.unlink(missing_ok=True)
        if data.get("stdout"):
            sys.stdout.write(data["stdout"])
        if data.get("stderr"):
            sys.stderr.write(data["stderr"])
        sys.exit(int(data.get("returncode", 1)))
    time.sleep(0.25)

req_file.unlink(missing_ok=True)
print("[local-exec] Timed out waiting for local-cage. Is zq-vpc up on your Mac?", file=sys.stderr)
sys.exit(124)
"""


def build_local_exec_script(remote_socket: str, remote_key: str) -> str:
    """Build the overseas user-space ~/bin/local-exec wrapper script."""
    return f"""#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "Usage: local-exec <project_name> <command...>" >&2
  exit 1
fi

PROJECT="$1"
shift
CMD="$*"

SOCK="{remote_socket}"
KEY="{remote_key}"
SOCK="${{SOCK/#\\~/$HOME}}"
KEY="${{KEY/#\\~/$HOME}}"

if [ ! -S "$SOCK" ]; then
  echo "[local-exec] Local cage socket not found at $SOCK. Is zq-vpc up on your Mac?" >&2
  exit 2
fi

exec ssh \\
  -i "$KEY" \\
  -o BatchMode=yes \\
  -o StrictHostKeyChecking=no \\
  -o UserKnownHostsFile=/dev/null \\
  -o LogLevel=ERROR \\
  -o ProxyCommand="nc -U '$SOCK'" \\
  dev@local-cage \\
  "cd /workspace/$PROJECT/work && $CMD"
"""


def provision_remote_user_space(
    ssh_host: str,
    remote_socket: str,
    local_priv_key: Path,
    projects: Dict[str, str],
    socks5_proxy: str = "",
    jump_host: str = "",
    target_addr: str = "",
) -> bool:
    """Idempotently provision project ./local-exec (file-RPC) and CLAUDE.md on `ph`.

    Strictly zero sudo, zero OS software installation, zero private keys uploaded overseas.
    """
    commands = []

    proj_exec_b64 = base64.b64encode(
        build_project_local_exec_script().encode("utf-8")
    ).decode("ascii")

    for proj_name, remote_local_dir in projects.items():
        proj_parent = remote_local_dir.rstrip("/")
        if proj_parent.endswith("/local"):
            proj_parent = proj_parent[: -len("/local")]
        md_b64 = base64.b64encode(
            build_claude_md_section(proj_name, remote_local_dir).encode("utf-8")
        ).decode("ascii")
        commands.append(f"mkdir -p {remote_local_dir} {proj_parent}/vpc")
        commands.append(
            f"printf '%s' '{proj_exec_b64}' | base64 -d > {proj_parent}/local-exec && chmod 755 {proj_parent}/local-exec"
        )
        commands.append(
            f"python3 -c \""
            f"import base64, pathlib, re; "
            f"p = pathlib.Path('{proj_parent}/CLAUDE.md').expanduser(); "
            f"block = base64.b64decode('{md_b64}').decode('utf-8'); "
            f"txt = p.read_text('utf-8') if p.exists() else ''; "
            f"pat = re.compile(r'{MANAGED_START}.*?{MANAGED_END}\\n?', re.S); "
            f"new = pat.sub(block, txt) if pat.search(txt) else (txt + ('\\n' if txt and not txt.endswith('\\n') else '') + block); "
            f"p.write_text(new, 'utf-8')"
            f"\""
        )

    if not commands:
        return True

    full_script = " && ".join(commands)
    res = subprocess.run(
        build_ph_ssh_cmd(
            ssh_host,
            remote_cmd=full_script,
            socks5_proxy=socks5_proxy,
            jump_host=jump_host,
            target_addr=target_addr,
        ),
        capture_output=True,
        text=True,
    )
    return res.returncode == 0


def check_overseas_hygiene(
    ssh_host: str,
    socks5_proxy: str = "",
    jump_host: str = "",
    target_addr: str = "",
) -> Dict[str, str]:
    """Read-only anti-ban hygiene check on overseas `ph` (TZ, LANG, exit IP)."""
    probe = (
        'printf "TZ=%s\\nLANG=%s\\nDATE=%s\\n" '
        '"${TZ:-unset}" "${LANG:-unset}" "$(date +%Z)"; '
        'curl -fsS --max-time 5 https://ipinfo.io/json 2>/dev/null || echo "IPINFO=unavailable"'
    )
    res = subprocess.run(
        build_ph_ssh_cmd(
            ssh_host,
            remote_cmd=probe,
            socks5_proxy=socks5_proxy,
            jump_host=jump_host,
            target_addr=target_addr,
        ),
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return {"reachable": "no", "error": (res.stderr or res.stdout).strip()}
    return {"reachable": "yes", "raw": res.stdout.strip()}
