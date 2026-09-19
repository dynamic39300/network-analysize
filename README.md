# 网络诊断与修复工具（网络助手）

> 项目代号：network-doctor ｜ 项目目录：`20260914-network诊断`
> 归档日期：2026-09-14 ｜ 当前基线：菜单栏版 v2.0 / 终端版 v1.0

---

## 项目一句话

一个面向 macOS 的**网络环境诊断与修复工具集**，围绕"公司 VPN + ClashX + 公共 DNS"三种上网场景，自动识别当前网络状态、定位问题（DNS 混配 / 代理残留 / IPv6 泄漏 / 连通性异常），并提供一键修复与开机自启守护。

## 为什么需要它

公司网络环境存在三种互相切换的联网方式（直连国内 / 公司 VPN 智能翻墙 / ClashX 代理），配置项多且互相影响（DNS、IPv6、系统代理、VPN 接口），日常频繁出现：

- 关闭 ClashX 后系统代理残留 → 全网打不开
- VPN 连接后 DNS 未切换/混配 → 外网不稳定
- IPv6 绕过代理 → AI 工具触发地域限制
- DNS 被 fake-ip 模式篡改 → 无法解析

每次都要手工敲命令排查修复，繁琐易错，于是做了这个工具集。

## 组件清单（当前基线）

| 组件 | 文件 | 版本 | 形态 | 说明 |
|---|---|---|---|---|
| 菜单栏版 | `code/network-doctor-menu.py` + `code/relay/` | v2.0 / Relay | rumps 菜单栏应用 | 常驻菜单栏，每 30 秒后台检测，Logo 常驻显示，一键检测/修复/报告 |
| 终端版 | `code/network-doctor.sh` | v1.0 MVP | 交互式 CLI | 完整检测 + 可达性测试 + 问题诊断 + 一键修复 + 自检 |
| DNS 守护 | `code/vpn-dns-sync.sh` | v1.0 | 后台守护进程 | 每 10 秒检测 VPN 状态，自动在"公司 DNS / 公共 DNS"间切换 |
| 启动/停止 | `code/start-menu-app.sh`、`stop-menu-app.sh` | v1.x | 辅助脚本 | 管理菜单栏应用进程（launchd 防重复、SIGKILL 兜底） |
| 一键检查 | `code/network-check.sh` | v1.0 | 终端脚本 | 8 项快速状态检查 |
| 打包配置 | `code/setup.py` | - | py2app | 打包为 `.app` |

## 目录结构

```
20260914-network诊断/
├── README.md                    # 本文档：项目总览
├── docs/
│   ├── 01-项目背景.md            # 背景、场景模型、目标与约束
│   ├── 02-开发过程.md            # 时间线、版本演进、关键决策与踩坑
│   ├── 03-软件方案.md            # 架构、检测/修复逻辑、部署方案
│   ├── 04-当前运行状态.md        # 运行快照、已知问题清单
│   └── 05-2.0升级规划.md         # 系统性升级方案（本次任务核心）
├── code/                        # 当前实现基线（与源项目指纹一致）
│   ├── network-doctor-menu.py       # Relay 菜单栏入口
│   ├── relay/                       # 插件化检测/修复引擎
│   ├── relay_config.py              # 配置、预设、环境探测
│   ├── menubar_icon.png / app_icon.png
│   ├── network-doctor.sh
│   ├── vpn-dns-sync.sh
│   ├── start-menu-app.sh / stop-menu-app.sh
│   ├── network-check.sh / test-muemod-vpn-route.sh
│   ├── setup.py
│   └── assets/                  # 应用图标（.icns / .png）
├── deploy/
│   └── com.wangxinlei.networkdoctor.plist   # launchd 自启配置
└── archive/                     # 历史版本与日志（.bak/.bak2/旧日志）
```

## 安装与运行（当前方式）

```bash
# 1. 创建虚拟环境并安装依赖（在安装目录执行）
/usr/bin/python3 -m venv "$HOME/Library/Application Support/网络助手/.venv"
"$HOME/Library/Application Support/网络助手/.venv/bin/pip" install rumps setproctitle

# 2. 安装脚本副本（必须放在 ~/Library/Application Support 等非保护路径，
#    因为 launchd 进程读不了 ~/Documents，见 docs/02）
cp code/network-doctor-menu.py "$HOME/Library/Application Support/网络助手/"

# 3. 加载 launchd 自启任务（gui 域 = 用户登录会话，图标可正常渲染）
launchctl bootstrap "gui/$(id -u)" deploy/com.wangxinlei.networkdoctor.plist
```

> 详细部署步骤与注意事项见 `docs/03-软件方案.md`。

## 本次任务目标

在归档当前基线与全部开发资料的基础上，**系统性升级到 2.0 正式版**，重点解决：

1. **状态图标可见性与可靠性**（历史多次出现"进程在跑但菜单栏无图标"）
2. **修复动作的安全性**（自动切 DNS 曾导致依赖网络的进程集体失联）
3. **可用性体验**（通知、历史记录、配置化、快速检测、场景自适应）

完整方案见 `docs/05-2.0升级规划.md`。

## 当前源码真相（2026-09-19）

当前实际运行版本已经升级为 **Relay 插件化架构**，源码已同步回 `code/`。后续开发以本项目目录为准；`~/Library/Application Support/网络助手/` 只作为 launchd 运行副本。

菜单栏显示已调整为只显示 Relay Logo，网络状态保留在下拉菜单中，不再在菜单栏标题区域显示绿色状态点或“正常/警告/异常”文字。
