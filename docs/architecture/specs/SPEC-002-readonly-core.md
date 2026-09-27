# SPEC-002：只读平台证据与无界面核心

2026-09-27。记录当前源码契约及限制，不替代完整 [BLUEPRINT-001](../BLUEPRINT-001-autonomous-network-agent.md)。实现为 `code/relay/environment.py`、`platforms/windows*`、`core.py` 和 `relay_core.py`。

## 平台证据

`EnvironmentSnapshot` / `relay-environment-v1` 包含平台、采集时间、执行作用域、接口、路由、解析器、代理、VPN、能力声明、不完整来源及限制。每类证据独立，不拼成一套全局 DNS/代理“真相”。原兼容 `status` 仍供已有 Agent 和诊断视图使用，并非完整环境模型。

Windows 使用仓库内固定 PowerShell 脚本，UTF-8 JSON 通过严格解析进入领域数据。单次调用超时 30 秒，解析文档上限 2 MiB，每类最多 8192 条，重复键、错误类型、错误地址族、重复接口身份/索引以及过期或无时区时间拒绝接受。这个 2 MiB 是解析输入上限，不是子进程捕获缓冲区的硬内存上限；受限作业执行器仍待实现。

接口 ID 采用 Windows `.NET NetworkInterface.Id`，当前适配只接受 GUID 形式；不以接口名字或可重分配的 index 为稳定身份。数据按地址族和索引关联到 ID，关联不上的地址/路由/DNS 标记不完整。索引变动或重命名不改变同一 ID；网卡重装导致 ID 改变时不会擅自合并。`.NET` 文档仅说明其为适配器标识，此处的格式约束是本实现的保守边界，非跨平台保证。[NetworkInterface.Id](https://learn.microsoft.com/en-us/dotnet/api/system.net.networkinformation.networkinterface.id)

路由仅覆盖当前进程默认 network compartment；分开保存路由 metric 和接口 metric，不把表中一项当作已证明的实际应用路径。多个来源顺次读取，不具有事务一致性。[Get-NetRoute](https://learn.microsoft.com/en-us/powershell/module/nettcpip/get-netroute?view=windowsserver2025-ps)

DNS 服务器按接口/地址族保留，NRPT 使用有效策略并标为 namespace 作用域；来源不能证明逐域名实际解析结果，也不能证明 Relay 有权修改管理策略。系统 VPN 档案的存在/连接状态不证明第三方 VPN 接口归属。[Get-DnsClientNrptPolicy](https://learn.microsoft.com/en-us/powershell/module/dnsclient/get-dnsclientnrptpolicy?view=windowsserver2025-ps)、[Get-VpnConnection](https://learn.microsoft.com/en-us/powershell/module/vpnclient/get-vpnconnection?view=windowsserver2025-ps)

代理原生读取保留 `process_user_wininet` 和 `winhttp_static_default` 两个来源，API 分配的字符串由 `GlobalFree` 释放。后者不是当前 WinHTTP 会话、所有应用或登录用户的最终配置；禁止将其作为前者缺失时的替代路径。[当前用户代理](https://learn.microsoft.com/en-us/windows/win32/api/winhttp/nf-winhttp-winhttpgetieproxyconfigforcurrentuser)、[WinHTTP 静态默认配置](https://learn.microsoft.com/en-us/windows/win32/api/winhttp/nf-winhttp-winhttpgetdefaultproxyconfiguration)

采集进程 SID/SessionId 与核心的当前令牌用户/进程会话匹配。服务身份、非交互会话及 Session 0 不发出冒充用户路径的 HTTP 检查。无法确认的用户代理、自动代理、代理例外或 VPN 路径保持未知。显式选择直连是“不经应用代理”，不是绕过 VPN 或企业路由。系统自带 curl 使用固定绝对路径，忽略 curl 配置、关闭 URL 展开、不跟随重定向；Windows VPN 不复用仅在部分平台支持的接口名参数。[curl --interface](https://curl.se/docs/manpage.html#--interface)

macOS 目前只把原有检查的实际覆盖映射到相同结构：BSD 接口名、已有目标路由、摘要型 DNS/代理和 VPN 归属证据。稳定服务 UUID、多作用域解析器与完整应用路径仍待接入，能力标为 `partial`。

Windows 若加载了已有配置中的 DNS/IPv6 预期策略，会增加明确的 `policy_validation` 未验证状态。HTTP 成功不能代替这些预期的验证，也不能据此更新完整正常记录；没有静默忽略从 macOS 带来的策略要求。

## 核心生命周期

`CoreRuntime` 组合已有 Agent、档案、私有 journal 和守护调度；无 Cocoa 或账号服务依赖，默认不调用模型。CLI 可作为单独进程运行，macOS 新增 `serve` / `desktop` 源码连接入口，合同见 [SPEC-005](SPEC-005-local-core-ipc.md)。默认安装桌面尚未迁移，Windows 本地传输、安装守护服务和特权服务也未交付。因此 ARCH1 只能记“部分”，不能视为三个独立生命周期已经完成。

- `check()`：显式只读观察、保存记录与符合条件的最近验证，返回内部 run 和脱敏摘要。
- `investigate()`：追加本地方案或显式同意的模型工具循环；数据边界和预算见 [SPEC-003](SPEC-003-reasoning-loop.md)。没有模型时保持本地能力，模型不赋予网络写权限。
- `start_guard()`：显式启动事件订阅与预算内只读守护。CLI 生命周期限于前台进程，不继承配置中的自动启动许可。
- `close()`：阻止新探测、撤销未执行授权、注销事件、等待已开始的只读命令收尾，再释放 journal 所有权。
- `on_report`：仅健康/能力/覆盖/持久状态等有变化时回调；在诊断锁释放后调用，允许客户端关闭核心。回调失败不停止调度。
- macOS 守护可形成未执行方案；下一轮或退出撤销旧方案。Windows 当前无成熟写工具。`check`/`watch` 保持只读；后续 macOS 动态命令交互试用是单独入口，合同见 [SPEC-004](SPEC-004-dynamic-jobs.md)，不从守护或模型上传同意自动取得执行许可。

Windows 使用三类 IP Helper 通知，回调只请求调度，不读取或修改网络。注销从核心线程发起，先屏蔽新回调，不在通知回调里取消自身；部分注册失败清理已成功注册的句柄，注销失败继续持有原生 callback 防止悬空。[NotifyIpInterfaceChange](https://learn.microsoft.com/en-us/windows/win32/api/netioapi/nf-netioapi-notifyipinterfacechange)、[NotifyRouteChange2](https://learn.microsoft.com/en-us/windows/win32/api/netioapi/nf-netioapi-notifyroutechange2)、[CancelMibChangeNotify2](https://learn.microsoft.com/en-us/windows/win32/api/netioapi/nf-netioapi-cancelmibchangenotify2)

Windows 数据目录按用户和会话隔离；创建时明确设置私有 DACL，校验只允许当前 SID 与 SYSTEM，不接受 UNC/reparse point。库通过 Windows 文件区间锁保持单实例所有权；POSIX 继续使用权限位和 flock。初始化失败、正常退出都释放锁，不等垃圾回收来释放。文件权限保护不等于防御同用户恶意进程或系统管理员。[pywin32 CreateDirectory](https://mhammond.github.io/pywin32/win32file__CreateDirectory_meth.html)、[GetTokenInformation](https://mhammond.github.io/pywin32/win32security__GetTokenInformation_meth.html)

## 报告与验证

普通输出为 `relay-core-report-v1`，只包含运行/档案版本标识、目标序号和状态、受限接口类别、来源能力、计数、预算及存储状态。不含 URL、内部域名、接口名、SID、原始异常或授权凭证。`--raw` 和档案导出是显式的本机原始资料操作。报告不是执行授权或恢复材料。

模拟测试覆盖上述契约；Windows 原生测试由 `RELAY_WINDOWS_NATIVE_TESTS=1` 显式启用，当前尚未在 Windows 执行。指南见 [Windows 只读预览](../../../apps/windows/README.md)，实际测试结果见 [QA-002](../../quality/reports/QA-002-diagnostic-panel.md)。后续 macOS 动态作业、授权后调查和源码桌面连接见 SPEC-003/004/005；Windows 实机/执行/本地传输、特权服务、跨轮目标轮换、默认桌面迁移与完整 UI 均保留在实施范围内。
