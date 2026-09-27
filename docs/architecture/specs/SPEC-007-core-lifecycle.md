# SPEC-007：普通用户核心生命周期

2026-09-27。这是 PRD-002 的一部分，不替代整体蓝图或签名安装验收。

后续签名约束：Developer ID 构建仅经 macOS 13+、默认目录、用户明确启用/批准的 LaunchAgent 使用 XPC；不能按下文开发路径启动临时 socket 核心。源码和 ad-hoc 临时启动合同不变。身份策略、等待批准界面及精确构建哈希的升级限制见 [SPEC-009](SPEC-009-signed-core-transport.md)。

## 入口与责任

`relay_app.py --agent-desktop` 启动连接工作台；只在未明确停机且目录空闲时启动普通用户核心，已有核心则重连。`--core-service` 复用无界面核心。冻结包通过同样参数分流，使用 `multiprocessing.freeze_support()`，不会让作业监督子进程误入菜单桌面。后续已将默认双击及 `network-doctor-menu.py` 默认入口切换为该工作台，原账号/历史迁移与 `--legacy-desktop` 回退见 SPEC-008；新旧运行时不能同时占用同一数据目录。

启动只代表核心可用，不代表网络健康。全新配置可能进行既有只读环境识别；未启用守护时不自动进行完整检测。模型上传、密钥、执行授权及调查上下文不因启动而恢复。

```sh
apps/macos/.venv/bin/python code/relay_app.py --agent-desktop --data-dir "$HOME/Library/Application Support/Relay-core-preview"
apps/macos/.venv/bin/python code/relay_app.py --lifecycle status --data-dir "$HOME/Library/Application Support/Relay-core-preview"
apps/macos/.venv/bin/python code/relay_app.py --lifecycle stop --data-dir "$HOME/Library/Application Support/Relay-core-preview"
apps/macos/.venv/bin/python code/relay_app.py --lifecycle start --data-dir "$HOME/Library/Application Support/Relay-core-preview"
```

`relay_core.py serve` 仍默认空闲；新增 `--restore-guard` 只恢复新核心明确保存的守护选择，不导入旧菜单的自动启动配置。前台 `watch` 不保存守护启用意图，保持退出后结束的原合同。`relay_core.py desktop` 仍仅连接，不代用户启动核心。核心 `settings.persistent_startup` 为未知值 null；实际系统注册状态由桌面生命周期管理器独立查询。

## 启动与停机

- 私有 `lifecycle/operation.lock` 串行化合作客户端的启动、停机及服务变更。`agent/owner.lock` 与 `ipc/owner.lock` 确认是否有既有运行时。占用但无法认证连接时拒绝替换，不根据 PID 搜索并杀进程。
- 启动使用固定入口/参数、独立会话和最小环境；不传递模型密钥、Python 路径注入或 DYLD 注入变量。新子进程带随机 launch ID，只有连接到匹配实例才报告启动成功。等待最多 15 秒；超时不杀进程、不自动重启，同一个管理器不会再启动仍存活的前一子进程。核心所有权锁另行防止多个核心拥有同一目录。
- 私有原子文件 `lifecycle/control.json` 保存停机抑制位。普通打开桌面尊重明确停机；用户点击“启动核心”才解除。服务入口也检查抑制位。它不是授权文件，不授予写网络权限。
- IPC `shutdown` 只接受 `purpose=stop|service_change|uninstall`。请求先保存既有去重日志，随即进入 draining，拒绝新任务，撤销未开始动作并暂停守护。返回 accepted 不表示已停机。
- 已启动执行必须结束验证或回退。关闭监听、等待操作线程和守护、保存操作终态、关闭数据库并释放所有权锁之后，才原子写入 `lifecycle/stopped.json`，绑定核心实例和原始请求 ID。生命周期管理器同时核对回执和锁。丢失响应只等原始回执，不重发 shutdown。
- 管理器默认等待最多 90 秒；超时仍不强杀、不注销服务。记录失败不能冒充完整停机回执；内存取消仍生效。网络是否恢复看任务收据，stopped 不等于 verified。未知副作用、回退失败和恢复文件原样保留。
- 完整停机及卸载准备清除已保存的守护启用意图。服务变更暂停当前实例，但保留用户明确的守护偏好；新实例只恢复该偏好，修复信任与上传同意仍暂停。

退出桌面仅断开连接；设置内“停止核心”是独立确认操作。生命周期操作在后台线程完成，执行期间禁止另一个生命周期操作和正常菜单退出打断流程。离线设置仍显示启动控件，其余核心设置不作为实时状态展示。

## 登录服务与移除

采用 macOS 13+ 的 `SMAppService` 用户会话 LaunchAgent，不复制特权二进制、不安装 root daemon、不自行收集管理员密码。App 内携带 `Contents/Library/LaunchAgents/com.wangxinlei.relay.agent.plist`，`BundleProgram` 为 `Contents/MacOS/Relay`。这些能力与目录约定依据 [Apple SMAppService](https://developer.apple.com/documentation/servicemanagement/smappservice) 和 [Apple 辅助程序迁移说明](https://developer.apple.com/documentation/servicemanagement/updating-helper-executables-from-earlier-versions-of-macos)。

只有冻结 App、默认用户数据目录、macOS 13+ 且存在服务文件时显示可操作注册。源码和自定义数据目录不假装支持安装服务。选择登录启动需要独立确认；先收尾当前核心，再 register。状态区分未注册、已启用、等待系统批准、文件缺失和未知；待批准时不悄悄使用临时进程兜底，可打开系统登录项。已注册但干净停止的服务使用无 `-k` 的 `launchctl kickstart` 启动，不结束已有进程。

LaunchAgent 的 KeepAlive 仅针对非成功退出；明确正常停机不会立即被重拉起。取消登录启动先停机，再 unregister；只有系统状态确认为 not_registered 才报告完成。卸载准备遵循同样顺序，保留应用、数据库、档案、历史和恢复资料，返回“可移除应用”，不自动删除任何内容。无法查询注册状态时不能声称已经完成卸载准备。用户主动删除 App、系统强退或强制注销会话不受合作退出协议保证。

## 打包与证据边界

构建补齐工作台图标、ServiceManagement 依赖和 LaunchAgent；自检覆盖 Agent 入口/原生模块/资源，输入哈希覆盖新入口与服务 plist。冻结进程再启动独立核心时使用 `PYINSTALLER_RESET_ENVIRONMENT=1`，避免继承父冻结运行时。依据 [PyInstaller 常见问题](https://pyinstaller.org/en/latest/common-issues-and-pitfalls.html)。

`tests/test_lifecycle.py` 用实际私有 socket、临时子进程和 FakeMac 验证；系统服务注册仅用模拟对象。`tests/test_lifecycle_native.py` 验证实际 Cocoa 确认、异步收尾、离线可见性及最小尺寸深浅色。`scripts/verify_lifecycle_bundle.py` 从新生成的 App 启动临时空闲核心，父启动器退出后再连接并停机；预置中性配置，不检测网络、不启用守护、不注册服务。构建/实测记录见 QA-002。

当前仍是本地 ad-hoc 开发包，不是 Developer ID 公证发行。真实 SMAppService 注册/批准/拒绝/升级/注销、系统登录/注销和安装移动尚未执行。UID/私有 IPC 不是可信签名应用身份；独立特权服务、Windows 生命周期、已安装版升级、跨核心推理/执行信任、安全升级和整体产品验收均未完成。
