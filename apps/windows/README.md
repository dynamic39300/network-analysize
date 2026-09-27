# Windows 只读核心预览

2026-09-27。这是 PRD-002 完整目标中的一个开发里程碑，不是 Windows 正式安装版。已有平台适配和模拟测试，尚未在 Windows 真机上验证；不能以 macOS 测试通过代替支持承诺。

## 能力边界

- 结构化读取多接口标识、IPv4/IPv6 地址与路由、接口 DNS、有效 NRPT、系统 VPN 档案及当前用户代理。每个来源保留作用域、未知状态和能力限制。
- 在当前交互用户会话中，通过系统自带 `curl.exe` 探测健康档案。支持明确直连及已读取的手工用户代理；不把 WinHTTP 静态默认代理当作用户的实际代理。
- 复用本地 Agent、档案、私有记录、最近验证与守护预算。原生 IP Helper 订阅失败时保留定时检查，不冒充原生事件可用。
- 显式 `investigate` 或 `watch` 可选择模型调查；默认不启用，需另给模型名和本次上传同意。没有 Windows 网络修改工具、提权、动态脚本或 GUI；不存在 `execute`/`authorize` 命令。导入档案不授予网络写权限。
- Windows VPN 的接口归属、逐目标路由和实际连接路径尚未实现；相关目标保持未知，不复用 macOS 接口参数或退回直连。PAC、自动发现、代理例外和应用自己的代理规则也不推断为已验证。
- 旧配置中如声明了 DNS/IPv6 策略，当前 Windows 模块不能验证它们，会明确报告策略未验证；不会只因 HTTP 成功就保存整体健康记录。

## 运行

候选验证环境为交互式 Windows 10/11、Python 3.12，尚无已验收最低 Windows 版本。需要系统自带 Windows PowerShell、NetTCPIP/DnsClient 命令及 `curl.exe`。VPN/NRPT 等可选来源不可读时明确记为不完整。固定采集脚本由仓库提供，不读取用户 profile，不请求执行策略绕过；被系统策略阻止时应保持未知。

在仓库根目录的普通用户 PowerShell 中执行：

```powershell
uv sync --project apps/windows --locked --default-index https://pypi.org/simple
apps/windows/.venv/Scripts/python.exe code/relay_core.py --help
apps/windows/.venv/Scripts/python.exe code/relay_core.py profiles list
apps/windows/.venv/Scripts/python.exe code/relay_core.py profiles import .\health-profile.json
apps/windows/.venv/Scripts/python.exe code/relay_core.py check
apps/windows/.venv/Scripts/python.exe code/relay_core.py watch
```

`profiles import` 是显式保存并启用输入定义；不会探测或改系统配置。`export ID` 导出含原始地址的定义，`activate ID` 切换，`remove ID` 仅能移除非活动档案。格式为 `relay-health-profile-v1`，与 macOS 档案导出兼容；来源/授权/旧验证结果不能从文件导入。

依赖锁使用 PyPI；上面的命令仅为本项目指定解析源，不修改系统或其他项目的镜像配置。当前锁定 `pywin32 311`，其真实 Windows 安装和原生行为尚待验证。

`check` 才访问档案中明确配置的 HTTP(S) 目标；新目录从默认配置的百度、Google、OpenAI 三个公网目标建立初始档案，可先导入用户自己的目标替换。`watch` 显式启动前台只读守护，`Ctrl+C` 收尾退出；不安装开机启动、不修改持久守护开关。没有在后台替用户开启监测。运行期间另一个 CLI 不能同时打开该数据目录；先退出 `watch` 再编辑档案。原生桌面与该 CLI 之间的 IPC 尚未实现。

默认输出逐行 JSON，排除 SID、接口名字、内部域名、原始 URL 和异常详情；`check --raw` 明确输出私有原始证据，仅用于本机排查。输出采用 ASCII 转义，仍是有效 UTF-8 JSON，不受终端旧代码页影响。`check` 退出码：`0` 表示当前目标健康、存储可用且无待核对恢复；`2` 表示仍有退化/未知/需认证或记录等事项；`1` 表示命令未完成。

`investigate` 也会检测档案目标，默认不上传模型；显式附加 `--model NAME --allow-model-upload` 才启用工具调查。API 密钥只从本机 `RELAY_MODEL_API_KEY` 环境读取，模型数据/预算/回环服务示例见 [SPEC-003](../../docs/architecture/specs/SPEC-003-reasoning-loop.md)。上传同意不授予网络写权限；模型协议和进程传输已在 macOS 上用合成 Windows 证据与回环服务验证，尚未做 Windows 原生或云端模型验收。`investigate --raw` 包含原始证据和模型假设，不应用于公开分享。

## 数据与生命周期

默认目录为 `%LOCALAPPDATA%/Relay/core-<用户 SID 哈希>-session-<会话 ID>`。用户和会话分开，暂不跨会话共享活动档案；可用全局 `--data-dir PATH` 显式指定目录。数据目录需要本地文件系统，拒绝 UNC、符号链接及其他 reparse point。创建目录时设置当前用户与 SYSTEM 的私有 DACL，读取时验证所有者和访问列表；`pywin32` 缺失或权限不符时拒绝启动，不照搬 POSIX `chmod` 假定 Windows 安全。

所有权锁使用 Windows 文件区间锁。进程退出释放锁，后续启动使旧授权失效；没有执行中的实例可以并行接管。Windows ACL 和跨进程锁仍需下列原生检查证实。不要先提权来规避目录错误或企业限制。

## 验证

模拟测试不修改网络设置，也不访问真实公司目标：

```powershell
apps/windows/.venv/Scripts/python.exe -m unittest discover -s tests -p test_windows.py -v
apps/windows/.venv/Scripts/python.exe -m unittest discover -s tests -p test_core.py -v
```

真实 Windows 的只读验收需明确选择运行：

```powershell
$env:RELAY_WINDOWS_NATIVE_TESTS = '1'
apps/windows/.venv/Scripts/python.exe -m unittest discover -s tests -p test_windows_native.py -v
Remove-Item Env:RELAY_WINDOWS_NATIVE_TESTS
```

原生检查仅验证临时目录 DACL、跨进程记录锁、当前会话结构化采集/代理来源、通知注册和注销；不切换网络、不执行网络写入、不发 HTTP 请求。仍需后续验收双网卡、实际网络切换、VPN 厂商、NRPT 管理策略、睡眠唤醒、长期预算和安装升级。完整范围见 [实施台账](../../docs/engineering/tickets/TKT-002-autonomous-agent-delivery.md)。
