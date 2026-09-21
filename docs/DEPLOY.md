# 部署指南（Windows / Linux / 树莓派）

三种环境都是**同一条命令**，启动器会自己识别平台并调整默认参数。

```bash
python bootstrap.py          # 或 ./start.sh（Linux/Pi/macOS）、start.cmd（Windows 双击）
```

---

## 一、一键启动做了什么

`bootstrap.py` 是唯一的实现，三个入口脚本只是「找到 Python 并调用它」的薄壳：

| 步骤 | 行为 | 为什么要这么做 |
| --- | --- | --- |
| 1. 找 Python | 优先复用已有的 `.venv`，否则用 `py -3.x` / `python3.11` 等逐个探测 3.10+ | 第二次启动直接进 venv，不再重复探测 |
| 2. 建 venv | `.venv` 不存在才创建 | 避免污染系统 Python（Debian 有 PEP 668 保护） |
| 3. 进 venv | 用 `os.execv` 重新执行自己，参数原样透传 | 保证后续所有逻辑都跑在正确的解释器里 |
| 4. 装依赖 | 已装齐则跳过；32 位 ARM 自动附加 piwheels 源 | 树莓派上避免本地编译 |
| 5. 自检 | 平台 / Python / 依赖 / 配置 / 库可写 / 凭证 / 时区 / 磁盘（可选网络连通性） | 把「跑不起来」的原因在启动前讲清楚 |
| 6. 启动 | 采集常驻（主线程）+ 看板（子线程） | 单进程即可，无需 supervisor |

自检**不会**因为缺凭证而拒绝启动 —— 缺 CSQAQ Token 只是能力受限（拿不到在售价），
免凭据的源仍然工作。只有「Python 版本过低 / 依赖缺失 / 库不可写」才算阻断。

### 常用参数

```bash
python bootstrap.py                      # 完整启动（采集 + 看板）
python bootstrap.py --setup              # 只准备环境，不启动
python bootstrap.py --check              # 只自检（--no-network 跳过网络探测）
python bootstrap.py --check --doctor-json # 自检结果输出 JSON（给脚本消费）
python bootstrap.py --run                # 跑一轮就退出（适合 cron）
python bootstrap.py --collect            # 只常驻采集，不起看板（树莓派省资源）
python bootstrap.py --serve              # 只起看板
python bootstrap.py --daemon             # 后台常驻（写 PID + 日志）
python bootstrap.py --status / --stop    # 查看 / 停止后台实例
python bootstrap.py --port 9000 --host 0.0.0.0
python bootstrap.py --skip-install       # 服务里用：跳过依赖检查加速启动
```

后台运行时会写：

```
run/facet.pid      进程号
logs/facet.log     标准输出与错误（追加写）
```

---

## 二、Windows

### 首次

1. 装 Python 3.12（[python.org](https://www.python.org/downloads/)，安装时勾选 **Add python.exe to PATH**），或从微软商店装。
2. 双击 `start.cmd`。

`start.cmd` 会按 `py -3.13` → `py -3.12` → … → `python` 的顺序找解释器；
出错时窗口不会立刻关闭，会停下来让你看报错。

### 开机自启

```powershell
.\deploy\install-windows.ps1                 # 默认 1800 秒采集间隔
.\deploy\install-windows.ps1 -Interval 600
.\deploy\install-windows.ps1 -Uninstall
```

用「计划任务」而不是 Windows 服务：服务运行在 Session 0，本项目自带 Web 看板，
计划任务在用户会话里跑更符合个人使用场景，也不需要额外装 nssm 之类的包装工具。

任务配置为**登录时启动 + 异常退出 2 分钟后重启 + 不因电池而停止**。

---

## 三、Linux（含 x86 服务器）

```bash
chmod +x start.sh
./start.sh --setup      # 首次：建 venv 装依赖
./start.sh              # 启动
```

### 安装为 systemd 服务（推荐）

```bash
sudo ./deploy/install-linux.sh              # 安装并启动
sudo ./deploy/install-linux.sh --user pi    # 指定运行用户
sudo ./deploy/install-linux.sh --uninstall  # 卸载
```

装好后：

```bash
systemctl status facet
journalctl -u facet -f          # 跟随日志
sudo systemctl restart facet
```

unit 里做了几件容易被忽略的事：

- `KillSignal=SIGINT` + `TimeoutStopSec=30` —— 采集循环能收到 Ctrl+C 语义并收尾，
  不会被 SIGTERM 直接砍断导致 SQLite 写入中断
- `MemoryMax=512M` / `CPUQuota=80%` —— 低配机器上防止异常膨胀拖垮整机
- `ProtectSystem=full` + `ReadWritePaths=<项目目录>` —— 只允许写项目目录
- `Restart=always` + `RestartSec=15` —— 崩溃自愈

---

## 四、树莓派

### 前置：优先用 64 位系统

```bash
uname -m        # aarch64 = 64 位（推荐）  armv7l = 32 位
```

32 位系统上 `pydantic-core` 等依赖缺少预编译 wheel，需要 Rust 工具链才能编译，
装起来又慢又容易失败。**建议重装 Raspberry Pi OS 64-bit 或 Ubuntu arm64。**

启动器会检测到 32 位并明确提示；如果你确实要用 32 位，`platform.py` 会自动
附加 piwheels 索引（`--extra-index-url https://www.piwheels.org/simple`）以提高成功率。

### 安装依赖

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

### 部署

```bash
git clone <你的仓库> ~/facet && cd ~/facet
chmod +x start.sh
./start.sh --setup                      # 首次配置（树莓派上约 3-5 分钟）
./start.sh --check                      # 自检，确认没问题
sudo ./deploy/install-linux.sh --user pi   # 装成开机自启服务
```

### 树莓派专属建议

**1. 数据库移出 SD 卡。** 这是最重要的一条 —— 长期高频写入会磨损 SD 卡：

```bash
sudo mkdir -p /mnt/facet-data
# 挂载你的 USB/SSD 到 /mnt/facet-data（写进 /etc/fstab 保证开机自动挂载）
echo "FACET_DB=/mnt/facet-data/facet.db" >> ~/facet/.env
sudo systemctl restart facet
```

**2. 拉长轮询间隔省资源。** 启动器在低内存/树莓派上已自动把默认轮询调到 3600 秒、
并发降到 2；想进一步调：

```bash
echo "FACET_INTERVAL=7200" >> ~/facet/.env   # 2 小时一次
sudo systemctl restart facet
```

**3. 只跑采集不起看板。** 需要看图时再从笔记本上开：

```bash
./start.sh --collect        # 树莓派上只采集
# 本地机器：把库里数据拷出来看，或临时 ssh -L 转发
```

**4. 从其它机器访问看板。** 看板默认只绑 `127.0.0.1`（监控清单暴露你的持仓意向，
是敏感信息）。推荐 SSH 端口转发而不是改 bind：

```bash
ssh -L 8787:127.0.0.1:8787 pi@<树莓派IP>
# 然后在本地浏览器打开 http://127.0.0.1:8787
```

**5. 内存与温度。** Zero/3B 上建议只监控几十个饰品；`--collect` 模式常驻内存约 60-90MB。
若机器发烫，检查是否有其它进程，本工具的采集是 IO 等待型、CPU 占用很低。

### 性能参考（树莓派 4B / 2GB / 64 位系统）

| 项目 | 实测/预期 |
| --- | --- |
| 冷启动（建 venv + 装依赖） | 3-5 分钟 |
| 热启动（复用 venv） | 2-4 秒 |
| 常驻内存 | 60-90 MB |
| 一轮采集耗时（50 个饰品 / 1 个源） | 1-3 秒 |
| SQLite 体积（50 饰品 / 30 分钟一次 / 一年） | 约 200-400 MB（启用归档后约 5-20 MB） |

归档建议（把超过 90 天的明细压成日线）：

```bash
# 每周跑一次，或加进 crontab
0 4 * * 0 cd ~/facet && .venv/bin/python -m facet archive run --keep-days 90
0 4 * * 0 cd ~/facet && .venv/bin/python -m facet archive prune-extreme --keep-days 7
```

---

## 五、排错

先跑自检，它会把大部分问题直接说出来：

```bash
python bootstrap.py --check
# 或在已有环境里
python -m facet doctor
```

| 症状 | 原因与处理 |
| --- | --- |
| 双击 `start.cmd` 刷屏 `'xxx' 不是内部或外部命令`、`'hon.exe"'`、`'ootstrap.py'`，并夹杂乱码中文 | **文件编码与系统代码页冲突**。cmd.exe 按系统 OEM 代码页（中文 Windows 是 936/GBK）读 `.cmd`；若文件是 UTF-8，中文注释会被错误解码，字节错位时**会吃掉换行符把两行合并**，命令碎片就被当成命令执行。<br>→ 已修：`start.cmd` 改为**纯 ASCII**，中文提示由 Python 输出。若你改过该文件，请保持纯 ASCII。 |
| PowerShell 里 `.ps1` 中文乱码 / 报语法错误 | Windows PowerShell 5.1 读**无 BOM** 的 `.ps1` 时按系统 ANSI（GBK）解码。<br>→ 已修：`start.ps1` 与 `deploy/install-windows.ps1` 保存为 **UTF-8 with BOM**。你改动后请确保编辑器保留 BOM（VS Code 右下角选 `UTF-8 with BOM`）。 |
| 输出里 `✓` `─` `★` 变成 `?` 或直接报 `UnicodeEncodeError` | 控制台代码页不是 UTF-8。<br>→ 已修：`facet/console.py` 在启动时把控制台切到 UTF-8，切不动就自动降级为 ASCII 符号（`OK` / `-` / `*`）。可用 `python -m facet doctor` 查看当前控制台编码状态。 |
| 运行完工具后，当前终端窗口里其它命令的中文变乱码 | 工具把控制台切到了 UTF-8，但进程被**强制终止**（任务管理器结束进程、`job_kill`）时来不及还原。<br>→ 正常 Ctrl+C 退出会自动还原。已乱码时执行 `chcp 936` 手动恢复。 |
| `找不到 Python 3.10+` | Debian/Pi 上 `sudo apt install -y python3 python3-venv python3-pip` |
| `创建虚拟环境失败` / `ensurepip is not available` | 缺 `python3-venv`，同上安装 |
| 依赖安装卡住或失败 | 换镜像：`echo "FACET_PIP_EXTRA_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple" >> .env`；或先跑 `./start.sh --setup` 看完整报错 |
| 32 位 ARM 编译 `pydantic-core` 失败 | 换 64 位系统（首选），或装 `build-essential python3-dev` 与 Rust 工具链 |
| 看板打不开 | 确认绑定地址：默认 `127.0.0.1:8787`，只能本机访问；远程用 SSH 转发 |
| `⚠ 缺少 CSQAQ_TOKEN` | 到 [csqaq.com](https://csqaq.com) 注册取 Token，**并在官网绑定本机白名单 IP**，写进 `.env` |
| 采到 0 条报价 | 看 `python -m facet sources` 确认有可用源；再 `python -m facet probe` 逐个测 |
| 告警不推送 | `config.yaml` 里 `notify.enabled` 默认 `false`；先用 `dry_run: true` 验证规则 |
| Windows 提示禁止运行脚本 | 用 `powershell -ExecutionPolicy Bypass -File .\start.ps1`，或直接双击 `start.cmd` |
| systemd 服务起不来 | `journalctl -u facet -n 50 --no-pager`；多数是路径或权限问题，重跑 `install-linux.sh` 会重建 unit |

### 关于 Windows 下的文件编码（重要，别踩）

本项目对入口文件有**硬性编码约定**，`tests/test_console.py` 会检查它们：

| 文件 | 编码要求 | 原因 |
| --- | --- | --- |
| `start.cmd` | **纯 ASCII，无 BOM** | cmd.exe 按 OEM 代码页读；UTF-8 中文会导致行合并、命令碎片被执行；BOM 会被当成命令名 |
| `start.ps1`、`deploy/install-windows.ps1` | **UTF-8 with BOM** | PS 5.1 读无 BOM 的 .ps1 按 GBK 解码，中文乱码甚至破坏语法 |
| `start.sh`、`deploy/install-linux.sh` | **UTF-8 无 BOM** | 有 BOM 会让 shebang 失效（`#!/usr/bin/env bash` 前面多出字节） |

改这些文件时请留意编辑器的编码设置。VS Code 右下角可切换；
不确定就跑 `python -m pytest tests/test_console.py -q` 验证。

---

## 六、升级

```bash
cd <项目目录>
git pull
python bootstrap.py --reinstall --check    # 重装依赖（requirements 变了才需要）
# 重启服务
sudo systemctl restart facet                 # Linux/Pi
Stop-ScheduledTask facet; Start-ScheduledTask facet   # Windows
```

数据库 schema 是**向前兼容**的（全部用 `CREATE TABLE IF NOT EXISTS`，
新增表不会影响旧数据）。全文索引在启动时若发现为空会自动回填，无需手工迁移。
