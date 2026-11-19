<div align="center">

# CagePass

**不开门，只递信 —— 让远程 AI 编程助手安全操作你本机的隔离数据**

*Keep private datasets locked inside a local offline Docker vault while your remote coding agent writes code, triggers local execution via `./local-exec`, and receives synced results.*

![CagePass](assets/cagepass-poster.jpg)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

</div>

---

## 解决什么问题？

当你把 AI 编程助手（如 Claude Code）运行在远程服务器 / 海外主机（尤其是**无 `sudo`、禁装系统软件、运行在隔离容器内**的环境）时，经常会遇到两个痛点：
1. **想让远程 AI 使用本机数据文件**（可能是 GB 级数据或敏感原始文件），但不想把原始数据上传到远程硬盘，更不能把本机宿主机暴露给远程。
2. **同时维护多个项目**时，既希望代码和报告双向实时同步、手感如本地，又希望重计算能在本地断网容器里就地跑完。

**CagePass** 用一套极简、零远程侵入的架构解决这个问题：
- **原始数据 100% 留在本机**（`vault/<project>/data/`，以只读 `:ro` 挂进彻底断外网的 `local-cage` Docker 容器）；
- **代码与产出实时双向同步**（`vault/<project>/work/` ↔ 远程 `<project>/local/`，由本机 Mutagen 驱动，远程无需 `sudo`、无需 FUSE、无需装任何系统软件）；
- **穿透沙箱的 `./local-exec` 文件 RPC**：远程 AI 只需在项目目录运行 `./local-exec "python3 analyze.py"`，本机守护进程自动在断网容器（预装 `pandas`、`duckdb`、`pyarrow`）内就地执行，并把结果写回同步区——**零远程私钥、零常驻反向端口**。

---

## 四层安全护栏

1. **目录白名单 + 分级读写**：仅挂载受控金库 `vault/<project>/{data:ro, work:rw}` 与显式声明的白名单目录，宿主机其余文件对容器物理不可见。
2. **高危路径拦截**（借鉴 [Claude-Cage](https://github.com/Lee-xeo/Claude-Cage)）：启动前解析 `realpath` + 大小写折叠（适配 macOS APFS），强制拒绝挂载 `/`、`$HOME`、`~/.ssh`、`~/.claude`、系统目录及 `.state/`。
3. **`.git` 等 VCS 目录自动只读覆盖**：扫描所有 `:rw` 挂载目录内的 `.git` / `.hg` / `.sl` / `.jj` 并以 `:ro` 覆盖挂载，防止远程篡改 `.git/hooks` 或 `.git/config` 触发宿主机命令执行。
4. **本地容器彻底断外网 + 屏蔽 Locale 透传**：`local-cage` 仅连接 `internal: true` 内部网络（`cap_drop: ALL`、`no-new-privileges:true`、非 root `dev` 运行、绝不挂 `docker.sock`）；所有出站 SSH 命令自动清除 `SendEnv`，避免透传本机语言环境变量。

---

## 快速开始

### 前置依赖（仅本机安装，远程零安装）
- Python 3.9+（含 `PyYAML`）
- Docker（macOS 推荐 OrbStack / Docker Desktop）
- [Mutagen](https://mutagen.io/)：`brew install mutagen-io/mutagen/mutagen && mutagen daemon start`
- 本机 `~/.ssh/config` 中配好可免密登录的远程主机别名（如 `my-remote-host`）

### 常用命令

```bash
# 1. 初始化配置（生成本地私有 zq-vpc.yaml，已被 .gitignore 忽略）
./bin/cagepass init
# 编辑 zq-vpc.yaml，将 remote.ssh_host 改为你在 ~/.ssh/config 里的主机别名

# 2. 新建项目槽位（支持多项目并行）
./bin/cagepass project add proj-a
./bin/cagepass project list

# 3. 安全投递本机文件（自动拦截逃逸软链接与 .env/私钥等敏感文件）
./bin/cagepass put /path/to/raw_dataset.csv -p proj-a --ro   # 放只读数据区（绝不同步到远程）
./bin/cagepass put /path/to/scripts_dir     -p proj-a --rw   # 放读写工作区（双向实时同步）

# 4. 启动本地断网容器、文件 RPC 桥接与 Mutagen 同步
./bin/cagepass up

# 5. 状态查看、体检与停机
./bin/cagepass status
./bin/cagepass doctor
./bin/cagepass shell proj-a     # 进入本地断网容器调试
./bin/cagepass project stop proj-a
./bin/cagepass down             # 停止同步与本地容器，回收共享
```

在远程项目目录（如 `~/cagepass_projects/proj-a/`）下，远程 AI 可以直接：
- 读写 `./local/` 里的代码和报告；
- 运行 `./local-exec "python3 your_script.py"` 在你的本机断网容器中读取 `/workspace/proj-a/data/` 并输出结果到 `/workspace/proj-a/work/`。

---

## 运行测试

```bash
python3 -m pytest -v
```

## 许可

[MIT License](LICENSE)
