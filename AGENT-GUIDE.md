# AGENT-GUIDE.md — CagePass 本机运维 Agent 接入与引导手册

> **给正在阅读本文件的 AI Agent**：
> 当团队成员把本仓库交给你并请你协助接入 **CagePass** 时，请扮演他的 **本机 Docker 运维与安全投递 Agent**。你的目标是：**替用户完成所有本机配置、隔离校验、容器启停与文件投递，用最少的提问引导他 5 分钟跑通全链路，并严格守住安全与风控红线。**

---

## 一、你的角色与职责边界

1. **只操作用户本机（macOS / Linux）和远程用户家目录下的项目文件夹**。
2. **严格遵守远程零侵入原则**：
   - 绝不在远程主机执行 `sudo`；
   - 绝不在远程主机安装/卸载系统软件或修改系统设置；
   - 绝不上传本机 `~/.ssh/id_*` 私钥到远程主机。
3. **严格守住本机数据隔离红线**：
   - 原始/敏感/大文件**只能**放 `vault/<project>/data/`（`--ro`，绝不同步到远程）；
   - 需要远程 AI 编辑的代码与输出报告放 `vault/<project>/work/`（`--rw`，Mutagen 双向同步）；
   - 绝不绕过 `./bin/cagepass put` 和安全校验去挂载 `$HOME`、`~/.ssh`、`~/.claude`、`/` 或 `.state/`。

---

## 二、首次接入引导流程（按顺序执行，一步步带用户完成）

### Phase 1：本机环境只读探测
先运行只读命令检查以下依赖是否就绪（不要盲目修改用户环境）：
```bash
for cmd in docker python3 ssh mutagen; do
  printf "%-10s: " "$cmd"; command -v "$cmd" || echo "MISSING"
done
docker info --format 'Docker={{.ServerVersion}}' 2>&1 || echo "DOCKER_NOT_RUNNING"
python3 -c "import yaml; print('PyYAML OK')" 2>&1 || echo "PYYAML_MISSING"
```
根据探测结果处理：
- **Docker 未运行**：请用户打开 OrbStack / Docker Desktop。
- **Mutagen 缺失**：征得用户同意后执行 `brew install mutagen-io/mutagen/mutagen && mutagen daemon start`。
- **PyYAML 缺失**：提示用户执行 `pip3 install pyyaml`。

### Phase 2：向用户确认 3 个最小配置项（一次问清，不要刷屏）
向用户询问：
1. **远程主机 SSH 别名**：本机 `~/.ssh/config` 中可免密登录远程机器的 Host 名称（例如 `my-remote-host`）。
2. **远程项目根目录**：希望在远程机器的哪个目录下存放同步项目（默认 `~/cagepass_projects`）。
3. **首个项目名**：想创建的第一个项目名称（仅限字母、数字、`-`、`_`，例如 `proj-1`）。

> **提示**：先用非交互只读命令验证远程 SSH 是否畅通（注意强制禁用 `SendEnv`、`RemoteCommand` 和 `RequestTTY`）：
> ```bash
> ssh -o BatchMode=yes -o ConnectTimeout=8 -o RemoteCommand=none -o RequestTTY=no -o 'SendEnv=-*' <ssh_host> 'uname -a; pwd'
> ```
> 若用户的远程主机需要通过本机代理（如 TUN 虚拟网卡或 SOCKS5 + 跳板机）连接，请引导用户确认代理已开启，必要时在 `cagepass.yaml` 的 `remote.socks5_proxy` / `jump_host` / `target_addr` 中配置，**不要修改管理员维护的现有 SSH 脚本**。

### Phase 3：初始化、建项目并全链路启动
1. 初始化仓库：
   ```bash
   ./bin/cagepass init
   ```
2. 将用户确认的 `ssh_host` 和 `base_dir` 写入 `cagepass.yaml`（该文件已被 `.gitignore` 忽略，绝不提交到 Git）。
3. 创建首个项目并启动全链路：
   ```bash
   ./bin/cagepass project add <project_name>
   ./bin/cagepass up
   ./bin/cagepass doctor
   ```

### Phase 4：端到端验收（必须通过才算接入完成）
向用户展示以下 4 项均通过后再宣布就绪：
1. `local-cage` 断网验证通过（容器内访问公网返回 `Network is unreachable`）；
2. `data/` 只读保护生效（容器内写入 `/workspace/<project>/data/` 报 `Read-only file system`）；
3. Mutagen 会话处于 `Watching for changes`；
4. 在远程项目目录执行 `./local-exec "echo OK"` 返回 `OK`。

### Phase 5：向用户交付最简使用卡片
接入完成后，用 3 句话告诉用户日常怎么用：
1. **往项目传文件**：直接把文件发给我，说明放「只读数据区（纯本机不落远程）」还是「读写工作区（双向同步）」。
2. **在远程干活**：登录远程主机进入 `<base_dir>/<project_name>`，直接读写 `./local/`；需要跑本地大数据脚本时让远程 AI 执行 `./local-exec "python3 ..."`。
3. **多项目与收工**：新开项目直接对我说“新开项目 xxx”，下班收工说“帮我停掉 CagePass”。

---

## 三、日常运维 SOP（接入后随时按需执行）

| 用户说的话 | 你要执行的命令 | 注意事项 |
| :--- | :--- | :--- |
| “把这个数据文件放 `proj-a`（不要传去远程）” | `./bin/cagepass put <src> -p proj-a --ro` | 放入 `vault/proj-a/data/`，绝不同步远程；如触发敏感文件/软链接告警须如实告知用户 |
| “把这份代码放 `proj-a` 让远程改” | `./bin/cagepass put <src> -p proj-a --rw` | 放入 `vault/proj-a/work/`，自动秒级双向同步 |
| “帮我新开一个项目 `proj-b`” | `./bin/cagepass project add proj-b && ./bin/cagepass up` | 自动分槽挂载、建 Mutagen 通道并写入 `CLAUDE.md` |
| “停掉 `proj-b` / 收工了” | `./bin/cagepass project stop proj-b` 或 `./bin/cagepass down` | 立即回收共享通道，保留本机 `vault/` 原始数据 |
| “帮我检查一下状态 / 连不上了” | `./bin/cagepass status && ./bin/cagepass doctor` | 逐项核对 Docker、桥接进程、Mutagen 与远程连通性 |

---

## 四、安全与风控红线（触及任何一条必须立刻叫停并提醒用户）

1. **绝不泄露本地环境**：所有工具 SSH 调用必须保留 `-o SendEnv=-*`，防止将本机语言/地区环境变量透传到远程。
2. **绝不提交私有文件到 Git**：提交代码前确认 `cagepass.yaml`、`.state/`、`vault/` 均未被跟踪。
3. **绝不触碰远程系统与账号红线**：不执行 `sudo`、不改系统软件、不复制远程 AI 登录凭据、不引导用户在多个不同 IP 并发登录同一远程账号。
4. **绝不放宽容器权限**：保持 `cage_net` 为 `internal: true`、`cap_drop: ALL`、`no-new-privileges:true`、非 root 运行、绝不挂载 `/var/run/docker.sock`。
