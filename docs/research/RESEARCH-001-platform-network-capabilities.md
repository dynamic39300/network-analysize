# RESEARCH-001：Relay 平台网络能力与权限边界

- 日期：2026-09-26。
- 状态：研究提案，未实机验证；不代表已实现功能、兼容性承诺或已批准的架构决策。
- 范围：Windows/macOS 网络配置、代理、特权执行与受管配置边界。只使用下列 10 个 Microsoft/Apple 官方一手来源，不涉及行业产品、通用标准或本项目代码分析。
- 方法：核查官方正文；Apple 动态页面使用同一文档的官方 Markdown 表述辅助读取。每源事实摘要控制在约 150 字内；后续架构推断单列。

## 1. 官方文档事实

### S01. Windows 多网卡与 IP Helper 路由查询

来源：[GetBestRoute2 function][S01]。定位：Parameters、Remarks、Requirements。适用：文档列出 Windows Vista 起的客户端支持。

事实：`GetBestRoute2` 根据目标 IP、源地址及接口参数返回最佳路由与源地址，属于 IP Helper。接口索引可能随网卡禁用、启用等操作改变，不能视为持久标识。

### S02. Windows NRPT 与 VPN split DNS

来源：[VPN name resolution][S02]。定位：Name Resolution Policy table、DNS suffix、Persistent name resolution rules。适用：Windows 10/11 的文档所述 VPN 配置路径。

事实：DNS 缓存之后先匹配 NRPT 命名空间规则；VPN 配置可为域名规则指定 DNS 服务器，并设置后缀与持久规则。未命中时涉及优先接口、后缀及超时后的其他接口查询。

### S03. Windows NRPT 的 GPO 优先关系

来源：[Configure DNSSEC rules using the Name Resolution Policy Table][S03]。定位：NRPT rule processing、Prerequisites、View the current NRPT policy。适用：页面列出 Windows Server 2016–2025；本文仅引用其 NRPT/GPO 规则。

事实：域 GPO 配置了 NRPT 时，本地 GPO 的 NRPT 设置被忽略。配置域 GPO 的前提包括管理员组成员身份，以及创建或编辑该 GPO 的权限。文档提供有效策略查询入口。

### S04. Windows WinHTTP、用户代理与应用代理

来源：[Setting WinINet Proxy Configurations in WinHTTP][S04]。定位：Setting the Proxy Configuration on a Session / on a Single Request。适用：WinHTTP API 文档；其中 IE、proxycfg 示例有历史背景。

事实：WinHTTP 代理可按会话设置，并被单次请求覆盖。应用可读取当前用户的 WinINet/IE 设置后用于 WinHTTP；服务读取用户设置有用户配置加载前提。自动代理按目标 URL 解析。

### S05. Windows UAC 与执行身份

来源：[How User Account Control works][S05]。定位：Sign in process、The UAC user experience、UAC elevation prompts。适用：页面列出的 Windows 10/11 与 Server 版本。

事实：默认 UAC 行为下，管理员登录也使用标准令牌启动普通应用；需要管理员令牌时发生提权。标准用户通常提供管理员凭据，管理员批准模式通常请求同意；提示行为可由策略改变。

### S06. Windows 服务 IPC 的命名管道权限

来源：[Named Pipe Security and Access Rights][S06]。定位：安全描述符、访问检查、通用写权限、logon SID 段落。适用：Win32 命名管道。

事实：命名管道按令牌与 DACL 检查访问。默认 ACL 包含 Everyone 与匿名账户的读取权限；通用写权限还含创建管道实例的权限。文档建议用登录 SID 限制远程用户或其他终端会话的访问。

### S07. macOS SystemConfiguration 的配置与运行态

来源：[Components of the System Configuration Framework][S07]。定位：The Persistent Store、The Dynamic Store。适用：Apple 归档指南，更新于 2006-02-07；仅作为概念与接口使用边界依据，不作现代 macOS 实现保证。

事实：持久库保存各 location、service、interface 的配置；动态库保存当前网络状态及当前配置副本，并提供变化通知。文档要求应用不得依赖存储文件的路径、类型和名称，也不得直接访问该文件。

### S08. macOS NetworkExtension、VPN 归属与受管凭据

来源：[NETunnelProviderManager][S08]。定位：Overview、Configuration Model、Profile Configuration / Credential Storage。适用：该类在 macOS 的配置管理范围；不将页面中的 iOS 部署示例当成 macOS 通则。

事实：该类要求 NetworkExtension entitlement，配置视图限于本应用创建或描述文件关联的配置。描述文件导入的 VPN 凭据另需 `com.apple.managed.vpn.shared` 访问组 entitlement；文档要求应用及扩展不得写入该组。

### S09. macOS SMAppService 与 helper 批准

来源：[SMAppService.register()][S09]。定位：Availability、Discussion。适用：macOS 13 起；更早系统的 helper 安装方案不在本次研究范围。

事实：服务注册受用户批准约束。LaunchDaemon 要经管理员在系统设置批准才会启动；多用户 LaunchAgent 要在每个用户运行应用时分别注册。未获批准可返回相应错误。

### S10. macOS MDM 配置的管理权

来源：[Intro to device management][S10]。定位：Enrollment profiles、Configuration and legacy configuration profile removal。适用：Apple 设备管理；本文仅采用其中适用于 Mac 的配置与移除边界。

事实：自动设备注册可设定只有管理服务能移除注册描述文件。移除注册描述文件会连带移除关联配置；移除方式取决于安装来源。文档另说明，Mac 用户持有本地管理员凭据时可移除手工安装的描述文件。

## 2. 对 Relay 的架构推断

以下均为根据上述事实提出的设计约束，不是官方对 Relay 的承诺，也未验证具体 API 调用或产品行为。

| 提案 | 设计约束与理由 | 依据 |
| --- | --- | --- |
| A01. 以目标和接口组织路由诊断 | 为每个目标保存 IPv4/IPv6、候选接口、路由结果、源地址和采集时间。执行前重新核对接口身份；不要仅凭默认网卡或单次查询认定实际应用流量路径。接口枚举、路由写入权限和 metric 行为另行验证。 | [S01][S01] |
| A02. 按命名空间诊断 split DNS | 对内部域、公共域及短名称分别采集有效 NRPT、接口 DNS、后缀和解析结果。常规诊断使用目标用户的系统解析路径；指定 DNS 服务器的查询仅作独立探针。修复前识别策略来源，公共 DNS 替换不进入默认修复集。 | [S02][S02]、[S03][S03] |
| A03. 代理模型保留作用域 | 分别记录 WinHTTP 配置、交互用户配置、应用已知覆盖及目标 URL 的自动代理结果；未知项显式保留。服务探测与用户应用探测分开解释。修改 WinHTTP 后仅验证受该设置影响的请求，不宣称浏览器或所有应用已恢复。 | [S04][S04] |
| A04. UI、探测与特权执行分离 | 普通权限承担 UI 和可执行的只读采集；写操作交给范围受限的执行器。Windows 的账户成员身份、实际令牌与 UAC 结果分别处理；macOS 单列 helper 未注册、待批准、可用状态。拒绝或取消后保留诊断能力。 | [S05][S05]、[S09][S09] |
| A05. 服务连接不等于操作授权 | Windows 管道显式设置最小 DACL、核验调用者与会话，限定本机使用。两平台执行器仅接受固定操作及结构化参数，逐请求校验目标、授权和前置状态；不接受任意 shell、路径或脚本。同用户进程能连接也不能得到任意执行能力。 | [S06][S06]、[S05][S05]、[S09][S09] |
| A06. 配置意图与生效状态分开 | macOS 读取与后续写入应走公开 SystemConfiguration API，保存 service/interface 关联。候选修复要求变更前快照、应用后回读及目标探测；配置已保存不能直接报告为网络已恢复。现代系统的授权、提交与生效机制仍需核查。 | [S07][S07] |
| A07. VPN 适配按提供方声明 | 把可观察的系统接口、路由、解析现象与 VPN 配置管理能力分开。只有明确受支持、具有相应授权且已验证的提供方适配才能执行连接或配置操作；NE 枚举为空不能推断系统没有 VPN。不得承诺通用跨 VPN 修复。 | [S01][S01]、[S02][S02]、[S08][S08] |
| A08. 修复计划绑定配置管理方 | 每个动作记录配置来源、作用域、所需授权、预期差异和回滚条件。执行前发现配置已变化就重新诊断；回滚也不得覆盖管理方的新策略。将“可读取”“可提权”“有权且可写”作为独立能力。 | [S03][S03]、[S07][S07]、[S08][S08]、[S10][S10] |

## 3. 管理员权限不能替代的前提

下表为拟定的产品边界；“暂不自动修改”是 Relay 的提案选择，不表示操作系统在所有情形下绝对禁止。

| 对象 | 不得作出的假设 | Relay 提案 | 依据 |
| --- | --- | --- | --- |
| GPO / NRPT | 本地提权就取得域策略编辑权，或本地写入必定成为有效策略。 | 检查有效策略与来源；域管理规则交由管理方处理。 | [S03][S03] |
| MDM 描述文件 | root/管理员身份足以移除任意受管配置，且移除不会影响其他功能。 | 受管网络配置默认只诊断；不以退管或删除描述文件作为网络修复步骤。可修改性未知时保持未知。 | [S10][S10] |
| VPN 厂商配置及扩展 | 系统能看到隧道就能管理厂商配置，或管理员身份可替代 entitlement 与应用归属。 | 厂商配置变更、重连、停用扩展均要求明确的受支持入口；未验证提供方仅报告观察结果。 | [S08][S08] |
| 证书、私钥和信任 | 提权就获得企业 VPN 凭据访问权、修改权或可恢复的私钥备份。 | 本研究只证实 Apple 受管 VPN 凭据的访问与写入限制。两平台的私钥导出、证书增删、信任变更和重新签发均不纳入通用自动修复；分别核查证书来源、用途与管理方。不得通过关闭 TLS 验证宣称修复。 | [S08][S08]、[S10][S10] |

## 4. 未验证项与后续验证边界

- Windows：未实测双网卡、IPv4/IPv6、接口重新枚举、域 GPO 与本地 NRPT、VPN 连接切换，以及用户/服务/PAC 的结果差异。[S01][S01]、[S02][S02]、[S03][S03]、[S04][S04]
- 权限与 IPC：未验证标准用户、管理员未提权、提权取消、跨用户会话与越权请求；macOS helper 的调用者身份校验和逐请求授权机制仍需单独设计。服务注册成功不作为该设计的验证结果。[S05][S05]、[S06][S06]、[S09][S09]
- macOS：SystemConfiguration 归档资料只支持概念分层；当前 SDK 的具体采集/修改权限、SCPreferences 提交与应用、各版系统上的 NE/SMAppService 行为均未实测。[S07][S07]、[S08][S08]、[S09][S09]
- 管理与凭据：没有完成 Windows MDM/GPO 全部优先规则、Windows 证书存储、macOS 各类钥匙串或硬件私钥权限的调查。S03 只证明 NRPT 的特定规则，S08 的证书依据只覆盖受管 VPN 凭据，不外推成所有证书不可修改。[S03][S03]、[S08][S08]、[S10][S10]
- 兼容性：没有验证任何第三方 VPN 组合，也未验证其私有解析、代理或流量拦截行为；当前证据不足以发布跨 VPN 兼容声明。[S02][S02]、[S08][S08]

[S01]: https://learn.microsoft.com/en-us/windows/win32/api/netioapi/nf-netioapi-getbestroute2
[S02]: https://learn.microsoft.com/en-us/windows/security/operating-system-security/network-security/vpn/vpn-name-resolution
[S03]: https://learn.microsoft.com/en-us/windows-server/networking/dns/name-resolution-policy-table
[S04]: https://learn.microsoft.com/en-us/windows/win32/winhttp/setting-wininet-proxy-configurations-in-winhttp
[S05]: https://learn.microsoft.com/en-us/windows/security/application-security/application-control/user-account-control/how-it-works
[S06]: https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights
[S07]: https://developer.apple.com/library/archive/documentation/Networking/Conceptual/SystemConfigFrameworks/SC_Components/SC_Components.html
[S08]: https://developer.apple.com/documentation/networkextension/netunnelprovidermanager
[S09]: https://developer.apple.com/documentation/servicemanagement/smappservice/register%28%29
[S10]: https://support.apple.com/guide/deployment/intro-to-device-management-depc0aadd3fe/web
