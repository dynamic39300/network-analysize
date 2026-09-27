# QA-002 原生诊断面板

2026-09-26，本机源码运行版。未重新打包或发布安装包。

## 本轮行为

- 启动自动打开原生窗口；关闭窗口保留菜单栏监测，可从菜单再次打开。
- 七个检测环节逐项展示状态、证据、问题解释与处理建议；检测中、未启用、未知不会显示为正常。
- 可安全处理的项目显示单项修复按钮。准备阶段重新检测，只保留所选问题；问题消失时不改修其他项目。
- 修复方案显示网络服务和目标配置，经确认后开始执行；同一窗口展示复查、快照、执行、验证和回退的真实事件，不模拟百分比。
- 成功必须来自修复引擎的验证结果；阻止执行、失败回退、回退失败分别展示。无自动修复方案时提供建议。
- 所有系统命令仍在诊断工作线程执行；确认和进度窗口不阻塞 Cocoa 主线程。原有账号、历史、报告及菜单入口保留。
- 主界面使用原有 Logo、系统字体、SF Symbols 和语义颜色，支持深浅色与最小 760 × 560 内容区域。

## 本机验证

| 范围 | 结果 |
| --- | --- |
| 当前工作区面板语义及修复进度 | 11 项通过 |
| 当前工作区控制器、菜单并发及自检 | 19 项通过 |
| 原生窗口布局与交互 | 4 项通过；含深浅色、窄窗口、逐项按钮、关闭重开、确认和修复结果 |
| 隔离配置后的完整桌面回归 | 88 项通过，4 项原生检查按默认设置跳过，另行执行已通过 |
| 源码应用 self-check | 通过，7 个检测插件与新面板模块可导入 |
| 实际启动 | 已重启源码应用，启动检测日志正常；未点击真实网络修复 |

原生视图截图位于本地忽略目录 `build/ui-preview/`，使用合成网络数据，包括诊断深浅色、检测中、窄窗口以及确认、修复中、成功、回退和回退失败。截图由 Cocoa 视图实际渲染，不是设计稿。

完整回归的对照方法：仅在测试进程内加载 `HEAD:code/relay_config.py`，其余代码均使用当前工作区。没有覆盖磁盘上的配置模块。当前未提交的配置模块不支持已有诊断测试的 `runner` 参数，这是此前确认的独立差异；上述 88 项通过不能代表该本地配置模块也已验收。

所有修复测试均使用模拟 macOS 命令边界，没有修改真实 DNS、代理、路由或 IPv6。真实权限拒绝、用户实际网络切换和第二台 Mac 仍需实机验收。

## 复现

### 手动检测模式

按最新交互要求取消启动检测与定时网络检测，后台定时器只保留账号权益刷新。初始化仅准备诊断组件；首页显示“尚未检测”，点击“检测”后才运行。切换预设清空旧结果并等待手动检测，打开或重开面板不触发检测。主动修复所需的安全预检及复检保留。

回归覆盖启动零次检查、模拟多个定时周期不检查、切换预设不检查、手动点击执行一次，以及原生待检测状态。相关测试共 54 项：22 项控制器、17 项面板语义/修复进度、11 项原生面板、4 项原生报告。原生截图 `build/ui-preview/diagnostics-manual-idle.png` 验证 760 × 560 下的待检测页面；未执行真实网络修复。

### 逐项处理入口

异常项统一显示“立即帮我处理”。具备安全方案时进入原有确认、执行、复检及回退流程；其余问题展开处理步骤，提供适用的“打开网络设置”和“重新检测”。网站服务拒绝访问等远端问题不提供修改本机设置的操作。系统设置打开失败时显示可操作的提示；检测期间禁用处理控件。点击单项按钮重新读取当前状态，已消失的问题不会进入修复。报告增加“处理当前问题”，只返回实时面板，不对脱敏历史快照执行修复。

本轮验证：16 项面板语义/修复进度、10 项原生面板、4 项原生报告、19 项控制器测试，共 49 项。设置打开动作及网络修改均在测试中模拟；检查了原生深浅色、最小窗口和展开处理步骤截图。仍未重新打包 dist 应用，配置模块的既有兼容限制保持不变。

### 长报告窗口关闭修复

原报告入口将最长 6500 字符的多行 JSON 交给 `rumps.alert`，没有内部滚动容器。原生回归测试通过实际入口和 NSAlert 布局（只拦截阻塞式 `runModal`）复现：报告窗口高 6409 像素，屏幕可用高 949 像素，关闭控件不能保持在屏幕内。

现在报告使用独立、可缩放、非模态窗口，默认显示易读概览，第二个页签提供完整脱敏数据，不再截断到 6500 字符。正文内部滚动；复制与关闭按钮固定在底部。关闭按钮、系统关闭入口、Escape、Command-W 均可关闭，重复打开复用同一个窗口，不阻塞诊断面板。报告固定为打开时的脱敏快照，避免阅读期间被后台检测改变。

验证：3 项原生报告回归测试通过，覆盖超长数据、关闭控件可见性、快捷键、关闭重开、非模态、脱敏及完整数据；19 项控制器/自检测试通过。已查看概览深浅色和 640 × 440 内容区域的详细数据截图，位于 `build/ui-preview/report-*.png`。

```sh
RELAY_NATIVE_UI_TESTS=1 python -m unittest discover -s tests -p test_report_native.py -v
```

### 紧凑首页追加验证

同日按用户反馈，将默认内容区域缩为 900 × 580，七项以 01–07 编号显示。顺序对应引擎实际顺序：连接、VPN、代理软件、网站地址查找、联网方式、上网转发设置、网页访问。目前检测是串行，没有虚构并行分支。首页仅显示简短结论，原因及原始结果按需展开。

新增“一键优化”汇总入口，仅选择当前仍存在且有安全方案的问题。检测进行中或检查出错时不可执行，不处理已经消失的问题。执行前仍在后台复检，并确认具体方案；单项入口与原有回退机制保留。

追加验收：15 项面板语义/修复进度测试、19 项控制器测试、6 项原生 UI 检查通过，共 40 项。原生检查断言七项在 760 × 560 最小内容尺寸内无需滚动；展开长详情后才需要滚动，并复验深浅色、逐项展开/收起、一键优化选取范围及运行中禁用。此轮系统写操作仍为模拟，未实际优化用户网络。原有本地配置兼容差异没有改动，前述对照回归限制仍然适用。

使用已安装锁定依赖的 Python 环境：

```sh
python -m unittest discover -s tests -p test_panel.py
python -m unittest discover -s tests -p test_menu.py
RELAY_NATIVE_UI_TESTS=1 python -m unittest discover -s tests -p test_panel_native.py
python code/network-doctor-menu.py --self-check
```

### 本地 Agent 核心

按 PRD-002 新增本地网络保障 Agent 核心。该核心把健康目标、检测证据、同类问题事件、授权前处理方案、成熟修复工具执行和验证/回退结果组织为一次 Agent run；控制器的检测、单项处理与一键优化均经由该 Agent 调度。当前仍为手动触发和逐次授权，不开启后台守护、动态命令、Windows 适配或特权执行器。

新增回归覆盖：Agent 生成处理方案前不修改网络；执行时复用现有成熟修复工具并以验证结果为准；HTTP 403 等服务拒绝不会被当作本机可修问题；相同问题会复用同一个事件，恢复健康后事件清空。配置管理器同步恢复为可注入 runner 的新实现，首次安装保持观察模式，坏配置不被覆盖。self-check 也已调整为核心能力严格、账号/Keychain/签名依赖可选报告，避免商业组件缺失阻塞基础 Agent。

本轮验证：

```sh
python -m unittest discover -s tests -p test_agent.py -v
python -m unittest discover -s tests -p test_diagnostics.py -v
python -m unittest discover -s tests -p test_menu.py -v
python -m unittest discover -s tests -p test_panel.py -v
```

### 2026-09-27 授权、持久化与中断核对

本地 Agent 增加独立于界面状态的单次授权，绑定原方案哈希、修改对象、目标配置和有效期；执行器直接核对获准动作。每次写入前复核授权，过期、撤销、变更和复用均不能启动新动作。应用退出撤销后续动作，已产生的写入仍按原修复引擎验证或回退。

任务、授权依据、处理时间线和动作收据写入私有 SQLite。进程所有权锁阻止第二实例接管仍活跃的记录。旧授权重启失效；中断任务先只读核对原值、目标值和保护目标，不重放网络写入。发现外部配置变更或无法确认状态时保留记录并阻止冲突修改。尚未完成独立特权服务和 Windows 的任务锁/恢复集成。

原生面板和菜单增加免费的“处理记录”，可看最近 50 次处理时间线和脱敏数据。原始快照、授权凭证及恢复路径不进入普通报告。恢复资料仍在私有本地目录。记录存储不可用时只读检测保持可用；修复停止。日志保留策略和完整事件搜索/差异视图继续列为待实现项。

使用现有锁定虚拟环境运行：

```sh
apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p '*native.py' -v
apps/macos/.venv/bin/python code/network-doctor-menu.py --self-check
```

结果：发现 134 项测试，118 项通过、16 项原生测试按默认设置跳过；16 项原生测试另行执行全部通过。self-check 通过。新增授权测试覆盖缺失/伪造授权、方案篡改、过期、撤销、重复执行、目标与原配置变更、磁盘写入失败、进程所有权冲突、写入后中断、重启核对及脱敏输出。所有系统写操作均由 FakeMac 模拟。

已实际查看 Cocoa 渲染的 `build/ui-preview/agent-records-narrow.png` 和 `diagnostics-light.png`：640×440 的长处理记录可滚动且关闭按钮固定可见，诊断面板新增入口后七行布局保持紧凑。没有启用真实守护，没有修改本机网络配置，没有打包或替换旧安装副本。完整剩余范围见 [实施台账](../../engineering/tickets/TKT-002-autonomous-agent-delivery.md)。

### 2026-09-27 显式启用的 macOS 守护

在原生面板底部与菜单中加入守护开关。首次安装默认关闭；显式启用后保存选择，重启时恢复。守护使用 SystemConfiguration 变化通知和有预算的低频检查，默认 5 秒稳定等待，连续变化合并最多 30 秒；相邻检查至少 60 秒，健康时 5 分钟复查，失败重试退避至 30 分钟。每小时最多 12 次自动检查，次数写入私有数据库并跨重启恢复。

每轮探测最多 60 秒、80 条检测命令、8 个 HTTP 请求；单次响应体设置 64 KiB 阈值。响应体超过阈值但收到有效 HTTP 状态时保留该状态证据，不谎报连接失败。该阈值不是链路上 TLS、报头及系统缓冲区的总字节硬上限。检查未覆盖的目标仍显示未知。命令数、请求数、耗时与停止原因进入记录的详细数据。

守护只生成待确认方案，不自行取得网络写权限。异常在原页面出现“查看方案”，同一事件只通知一次；用户决定处理时重新检测并生成新鲜授权方案。后台事件不打开确认 sheet、不锁住手动检查入口。暂停立即使排队任务失效，进行中的只读命令结束后不再发新探测；正在执行的已授权修复继续完成验证或回退。账号刷新仍独立，未启用守护时启动/打开窗口不扫描。

已结束任务保留上限为 30 天或 1000 条；未核对的中断任务不被自动清理。恢复文件的独立保留/清理与安全删除仍未交付。

新增验证包括：虚拟时钟下的抖动/事件风暴、冷却、离线退避、暂停恢复不重置预算、跨重启预算、忙碌队列不丢早到事件、UI 回调失败不停止调度、命令时间限额、访问目标超额不报健康、真实诊断循环中暂停、临时回环 HTTP 大响应、显式启用/保存失败、排队取消、去重通知、授权修复安全收尾和记录保留。

最终验证命令同上一节。完整测试发现 158 项：140 项通过、18 项原生测试按默认设置跳过；原生测试另行运行 18 项通过（17 项窗口交互/布局与 1 项原生网络订阅注册/停止）。self-check 通过，包括新增 SystemConfiguration 依赖。静态错误检查和 `git diff --check` 通过。

实际查看了 `build/ui-preview/guard-attention-narrow.png`：760×560 内容区域保留完整七项、守护开关、待确认状态及处理记录入口；自动方案没有模态确认弹窗。该截图使用合成数据。本机原生订阅已验证能注册及停止；未进行真实网络切换、睡眠唤醒、热点/电池长时间压力或 Windows 实机验证。本轮没有改变系统网络配置，没有为当前用户实际启用常驻守护，没有重新打包发布。

### 2026-09-27 网络健康档案与逐目标验证

新增 `profiles.py`、`target_paths.py`、`profiles_window.py`，接入原生主窗口、控制器、Agent、SQLite 及修复验证。定义、版本和最近验证独立存储；建立、复制、修订、切换、移除、导入草稿和原始定义导出可实际操作。修改不触发系统配置写入；守护已启用时只安排预算内重新检查。无效存储不退回默认目标来执行修复，保留只读检测。

档案最多 32 个 HTTP(S) 目标，访问路径、VPN 条件、超时和成功条件可编辑。系统路径中的 PAC、例外或分接口规则未验证时不代用直连；明确选择“不经代理”不代表绕开 VPN。VPN 目标核对已知服务归属、系统解析所得目的地址的路由、固定 curl 地址/接口及请求后的远端和路由。系统解析路径仍需后续单独验证，不声称 DNS 防泄漏。服务正常需要 HTTP 2xx，不验证业务正文；401 或 3xx 不能建立服务健康记录。

新增测试覆盖严格导入边界/重复字段、同名目标独立 ID、原配置不被重写、坏现场不学习为期望、版本冲突、保存与激活失败的事务回退、重启读取、目标定义哈希、十分钟过期、范围未知/不适用、HTTP 认证和重定向、最近验证保存失败、档案变化后授权失效、服务退化后的配置回滚、VPN 归属/路由变化/远端地址不符、IPv6 固定地址及代理例外边界。使用临时回环 HTTP 服务验证系统 curl 的地址固定、接口参数、禁止 URL 展开、忽略 `.curlrc` 及不跟随重定向；没有访问真实公司目标。

原生测试覆盖保存全部字段、脏编辑保护、检测期间禁用、内联错误、导入/复制为草稿、同名档案选择、长目标滚动、深浅色及 760×560 布局。已查看 Cocoa 实渲染截图 `build/ui-preview/profiles-narrow.png`、`profiles-many-targets.png`；底部保存按钮不随目标列表滚动，输入保留完整值。

完整测试发现 200 项：174 项通过、26 项原生测试默认跳过；原生测试另行运行 26 项全部通过。档案专项 28 项，原生档案 8 项。使用锁定 `apps/macos/.venv` 环境，执行命令同上一节。所有系统写入仍使用 FakeMac；回环 HTTP 与短暂原生事件订阅不改变网络配置。没有启用当前用户常驻守护、替换安装副本或发布安装包。

最终再以 `RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` 合并运行，200 项全部通过。`--self-check`、`ruff check code tests --select E9,F63,F7,F82` 和 `git diff --check` 通过。

保留的验收缺口：真实 VPN/多接口/热点、企业策略/复杂身份、跨轮目标轮换、Windows。当前每轮守护仍受 8 请求等预算限制；超额会显示未完成，不能视为所有目标已被持续覆盖。完整剩余范围继续由 TKT-002 跟踪。

### 2026-09-27 Windows 只读适配与无界面核心

新增 Windows 结构化采集和平台证据契约；接口身份、索引和名字分开，多地址族、路由、接口 DNS 与有效 NRPT 分作用域保存。WinINet 当前进程用户代理与 WinHTTP 静态默认配置分开，不互相替代。用户/会话不匹配、服务会话、未知 VPN、不可读来源、代理规则和旧 DNS/IPv6 策略未验证，均不能仅凭 HTTP 成功报告整体健康或建立正常记录。

`CoreRuntime`/CLI 复用 Agent、档案、journal 和预算，支持显式检查、前台只读守护及档案管理。默认脱敏，原始证据需 `--raw`。CLI 不暴露网络写入或授权端点，不安装后台服务。事件回调在核心诊断锁外发布；关闭核心会停止后续命令、等待已开始的只读命令，再释放记录锁。旧未执行的守护方案在下一轮/退出撤销。

新增 39 项 Windows 合成证据/契约测试、12 项无界面核心与 CLI 测试。覆盖 GUID 重命名/索引变化、IPv4/IPv6、缺失/陈旧/重复/错误数据、无 DNS 服务器的 DNSSEC NRPT、作用域错配、代理来源与格式、API 内存释放、VPN 不回退直连、服务需认证、Windows curl 命令及请求预算、暂停/并发/退出、事件部分注册/注销失败和 callback 保留、DACL 验证契约、档案导入/导出/激活/移除、记录/初始化失败、跨重启读取和脱敏边界。Windows 系统调用与 ACL/事件行为在这些测试中为模拟，不代表真实 Windows 验收。

另新增 3 项 Windows 原生测试，仅在真实 Windows 且显式设置 `RELAY_WINDOWS_NATIVE_TESTS=1` 时运行：临时私有目录与跨进程记录锁、结构化采集/当前用户代理作用域、IP Helper 注册/注销。**当前均未执行**，本机为 macOS，不能据此承诺 Windows 兼容。测试不会切换网络或执行网络写入，也不发送 HTTP 请求。

本次最终命令与结果：

```sh
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
apps/macos/.venv/bin/python code/network-doctor-menu.py --self-check
apps/macos/.venv/bin/python code/relay_core.py --help
apps/macos/.venv/bin/ruff check code tests --select E9,F63,F7,F82
uv lock --check --project apps/windows --default-index https://pypi.org/simple
git diff --check
```

完整发现 **254 项，251 项通过、3 项 Windows 原生测试跳过**；包含原有 26 项 macOS 原生检查。其余命令通过。Windows 独立依赖锁使用 PyPI，`pywin32` 固定为 311；未在当前 Mac 安装或伪造 Windows 原生运行。没有改变用户实际网络配置，没有启用常驻守护，没有替换运行中的安装副本或发布新包。

保留的范围：Windows 真机/厂商/策略矩阵、Windows 写入及桌面、实际目标路径验证、桌面到独立核心的身份校验 IPC、系统特权服务、受控动态命令、模型推理循环、任务/持续授权、完整五视图以及安装升级。当前 CLI 可单独运行，不代表现有桌面已迁移为三进程架构。实现契约见 [SPEC-002](../../architecture/specs/SPEC-002-readonly-core.md)，Windows 运行说明见 [只读核心预览](../../../apps/windows/README.md)。完整产品目标继续推进。

### 2026-09-27 可选模型调查与工具循环

真实 Responses 客户端、可终止的 HTTP 子进程与主 Agent 接通。工具包括假设记录/修订、全量复查、已有目标定向复查、成熟方案准备和结束；本地验证恢复，不依赖模型口头结论。CLI 需显式模型名与本次上传同意，单独设置密钥不会启用模型。模型没有授权/执行端点，外部内容不能扩展目标、工具或权限。

新增 22 项调查测试、13 项协议/真实回环 HTTP 测试、3 项 CLI 同意测试。覆盖同一任务内假设/探测/修订、证据引用校验、伪造授权与未知工具、重复 ID、无新证据停止、档案变更、模型实例同意绑定、撤销、未知用量、记录失败、单目标成功不能替代全档案验证、共用探测预算、跨重启模型额度、健康时不调用模型、中断不重放，以及模型离线时保留本地成熟方案。明确用户授权后的成熟执行继续使用 FakeMac 验证，不发生系统写入。

HTTP 测试使用临时回环服务而非云端或真实公司目标，验证 JSON 请求、授权头不入模型正文、`store:false`、禁止重定向、体积上限、错误内容拒绝、超时与撤销后子进程退出。新增核心集成场景在请求等待期间关闭 Core，确认不会执行返回工具、请求进程退出、原记录锁先保持占用后释放且可重新打开。模型/平台输入为合成数据；不以这些测试声称真实模型诊断准确率、供应商零保留或 Windows 原生行为已通过。

修正一处审计边界：发起调用后若失败或未收到有效用量，`usage_known` 必须为 false；保守预留额度不是“实际消耗为零”或账单。完整数据/预算/运行合同见 [SPEC-003](../../architecture/specs/SPEC-003-reasoning-loop.md)。

最终验证：

```sh
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
apps/macos/.venv/bin/python code/network-doctor-menu.py --self-check
apps/macos/.venv/bin/python code/relay_core.py investigate --help
apps/macos/.venv/bin/ruff check code tests --select E9,F63,F7,F82
apps/macos/.venv/bin/ruff check code/relay/reasoning.py code/relay/models.py code/relay/model_transport.py tests/test_reasoning.py tests/test_models.py --select E9,F
uv lock --check --project apps/macos --default-index https://pypi.org/simple
uv lock --check --project apps/windows --default-index https://pypi.org/simple
git diff --check
```

完整发现 **292 项，289 项通过、3 项 Windows 原生测试跳过**，耗时约 12 秒；包含既有 26 项 macOS 原生检查。其余命令全部通过。两平台新增 `jsonschema`，既有依赖版本与 PyPI 来源保持，Windows 未在 Mac 安装。没有新增 UI，所以本轮没有将旧截图视为新模型视图的验收；没有真实云端调用、网络配置修改、用户常驻守护启用、替换旧安装副本或发布新安装包。

后续仍包括动态执行、模型驱动的已授权操作、任务/持续信任、独立特权服务与身份校验 IPC、完整桌面/隐私/设置视图、真实供应商质量对比、Windows 写入/桌面/真机以及跨平台发布验收。本轮五工具集不是完整目标的永久边界，整体目标仍进行中。

### 2026-09-27 macOS 动态作业、终端确认与崩溃收据

模型增加 `propose_command`，只准备非预置 argv/stdin；动态授权需要独立风险确认，不能复用模型上传同意或成熟工具确认。CLI 默认命令预览不执行，交互模式仍需真实终端和完整方案哈希。执行绑定身份、二进制内容、现场、档案和时限；写前保留快照与启动意图。当前动态运行只接 macOS 源码核心，Windows 和原生桌面尚未接入。

新增 **26 项**测试使用真实 Python/临时脚本和临时文件，网络检查/成熟修复始终由 FakeMac 模拟。覆盖精确内容审阅、缺失风险确认、伪造/过期/复用授权、参数/脚本/可执行文件/配置变化、额外权限字段与档案外目标、写前记录失败、stdout/stderr 与 stdin、API 密钥不被自动继承到命令环境、输出超限、超时/撤销、合作进程组停止、同用户跨核心冲突锁和关闭时 journal 锁收尾。

追加用例先复现主命令退出后继承输出管道的后台子进程仍会继续写临时文件，再修正为一旦观察到前台退出即停止原进程组，并继续收集已有输出。修正后该用例通过，不必等后台管道关闭或整个超时到期。

真实进程死亡试验启动一个仅在临时目录更新心跳的作业，在观察到启动后强制结束其核心父进程。验证监督进程通过控制连接 EOF 停止作业、回收命令、释放持有的写锁，并将带正确作业哈希的取消收据落盘。此试验不涉及用户网络、其他应用进程或管理员权限。不能由此推断能终止主动逃离进程组/委托系统服务的任意代码。

终态输出可在核心结果丢失后重新附加到 journal；错误哈希、部分文件、存储失败不伪造可用记录或覆盖已有文件。网络后检发现保护目标退化时保留需核对状态，不自动回滚覆盖配置。重启不重放；人工核对需要精确收据及说明，只记为 `reviewed`，不冒充 Agent 验证任意副作用。原始命令/输出不进入普通摘要，可能写入时不谎报 `network_writes:0`。

验证命令：

```sh
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
apps/macos/.venv/bin/python code/network-doctor-menu.py --self-check
apps/macos/.venv/bin/python code/relay_core.py command --help
apps/macos/.venv/bin/python code/relay_core.py review-command --help
apps/macos/.venv/bin/ruff check code tests --select E9,F63,F7,F82
apps/macos/.venv/bin/ruff check code/relay/jobs.py code/relay/dynamic.py code/relay_core.py tests/test_dynamic.py --select E9,F
git diff --check
```

完整发现 **318 项，315 项通过、3 项 Windows 原生测试跳过**，约 17 秒，包含既有 26 项 macOS 原生检查。自检、CLI 帮助和静态检查通过。此轮没有新增 UI，既有原生回归不证明动态确认视图已交付；没有云端模型、真实网络修改、常驻守护启用、系统提权或发布安装包。

剩余要求未改变：Windows 动态控制/成熟写入/完整产品、桌面/核心/特权 IPC、授权后模型继续推进、动态效果验证/补偿、高信任模式、完整 UI 与隐私/设置、原始作业资料清理、真实模型效果及跨平台发布验收。源码合同和风险见 [SPEC-004](../../architecture/specs/SPEC-004-dynamic-jobs.md)。

### 2026-09-27 授权执行后的同任务调查续接

同进程 Core 保留等待用户确认的调查上下文，核对已执行方案、持久收据和最新快照后继续同一 AgentRun。CLI 支持成熟修复和动态命令逐次确认；后续方案需要独立授权，历史动作收据不被覆盖。数据合同升级为 `relay-minimal-evidence-v2` 并绑定上传同意，只新增有限收据摘要，不上传动态命令、标准输出或错误输出。

新增 `test_reasoning_resume.py` **21 项**测试，覆盖同任务上下文/证据/预算延续、三目标默认档案复用新鲜且完整的已验证后检、过期后检重新探测、未执行/伪造/变更收据及快照拒绝、一次性交接、更换模型/撤销上传/更换档案、120 秒确认等待不重置累计预算、15 分钟总时限阻止新确认及续接、时限后不能靠本地兜底另起方案、暂停守护后的显式用户接管、重启保留已执行结果但不自动续跑、调用上限、回退后第二次确认、新发现问题更新修复选项、回退失败不能由模型清除、执行后模型离线不谎报未写入、动态输出不上传且未知效果阻止新命令，以及两类 CLI 后续取消路径。

其中一项针对刚完成成熟修复且网络已健康、用户拒绝后续动态方案的场景，确认 CLI 仍返回取消状态和退出码 2，不以健康读数掩盖用户的取消决定。另一项确认回退失败保持 `needs_reconciliation`，不能被模型结束工具覆盖。等待与独立执行阶段暂停的是调查活动计时，不是执行器自身限制；计数不是整个任务所有系统命令的总量。

最终验证命令：

```sh
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
apps/macos/.venv/bin/python code/network-doctor-menu.py --self-check
apps/macos/.venv/bin/python code/relay_core.py investigate --help
apps/macos/.venv/bin/ruff check code tests --select E9,F63,F7,F82
apps/macos/.venv/bin/ruff check code/relay/reasoning.py code/relay/models.py code/relay/model_transport.py code/relay/guard.py code/relay/agent.py code/relay/agent_store.py code/relay/core.py code/relay_core.py tests/test_reasoning_resume.py --select E9,F
git diff --check
```

完整发现 **339 项，336 项通过、3 项 Windows 原生测试跳过**，耗时约 17 秒，包含既有 26 项 macOS 原生检查；自检、CLI 帮助和静态检查通过。网络和模型响应为合成输入，动态作业只写临时文件。没有真实云端调用、当前电脑网络修改、常驻守护启用、替换安装副本、提权或打包发布。没有新增 UI，原生回归不等于模型交互视图已交付。

完整目标仍进行中：跨进程/重启调查恢复、桌面与核心身份校验 IPC、独立特权服务、任务/持续信任、动态结构化验证/补偿、Windows 执行及完整产品、五视图和权限隐私、真实模型与跨平台场景验收、打包更新均未由本步完成。同进程续接合同见 [SPEC-003](../../architecture/specs/SPEC-003-reasoning-loop.md)。

### 2026-09-27 独立核心通信与连接式原生桌面

新增版本化私有 Unix socket、同用户/核心实例/消息校验、后台操作调度、持久请求去重，以及 `serve` / `desktop` 源码入口。原生桌面不持有 Agent 或 journal，通过本地连接获取状态、检测/调查、管理档案、停止任务、撤销上传及查看完整具体方案。确认令牌绑定当前客户端和完整方案，一次消费；每个后续动作仍独立确认，动态操作另需风险勾选。

新增 **26 项**真实 socket/核心/客户端测试、**2 项** CLI 入口测试、**5 项**原生 Cocoa 测试：

- 真实本机 peer UID、私有描述符/socket 权限、错误凭据、注入不同 UID 的拒绝分支、核心实例更换、错误 nonce、超长帧、重复 JSON 键、第二个服务不能删掉正在使用的端点。
- 严格方法/参数、写前操作落盘失败不执行、相同请求只返回原操作、修改内容拒绝、审阅绑定不同客户端/具体内容/期限、档案漂移拒绝写入、等待确认时不被新任务替换。
- 核心忙时仍能查询/停止；暂停后重新启用创建新的网络通知对象；有效方案暂缓守护，过期后旧确认失效且已启用守护能重新检查。
- 断开客户端后任务和守护留在核心，重新连接可查同一任务；客户端丢失 ACK 后只查询原请求，不自动重发。实际 spawn 的 FakeMac 核心在客户端退出后仍存活；强制结束该测试进程后重启，把旧请求标记 interrupted，重复请求不执行。
- 成熟修复通过 socket 确认后继续同一个模型任务；动态命令缺少独立风险确认时不能启动。获准后实际运行的 Python 只读取临时测试文件并打印内容，结果保留需核对，私有 stdout 不进入普通状态或后续模型请求。
- 实际 Cocoa 桌面通过真实 socket 连接测试核心，检测完成后显示本地状态，退出客户端不关闭核心。完整方案窗口验证深浅色、760×560、长内容滚动、非编辑文本、勾选/取消/一次提交，以及断线、换核心、过期后禁用确认。

回归过程中修正新入口预算对旧 CLI 的两个影响：保留旧 `check` 的中断记录核对和 Windows 参数合同，新连接入口单独使用预算；其恢复回读使用剩余预算。所有网络与成熟修改均由 FakeMac/FakeWindows 模拟；未用测试通过代替真实网络修复或模型诊断质量结论。

最终验证命令：

```sh
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
apps/macos/.venv/bin/python code/network-doctor-menu.py --self-check
apps/macos/.venv/bin/python code/relay_core.py serve --help
apps/macos/.venv/bin/python code/relay_core.py desktop --help
apps/macos/.venv/bin/ruff check code tests --select E9,F63,F7,F82
apps/macos/.venv/bin/ruff check code/relay/ipc.py code/relay/core_service.py code/relay/core.py code/relay/agent_store.py code/relay/remote_controller.py code/relay/proposal_window.py code/relay/remote_desktop.py code/relay_core.py tests/test_ipc.py tests/test_proposal_native.py tests/test_core.py --select E9,F
git diff --check
```

完整发现 **372 项，369 项通过、3 项 Windows 原生测试跳过**，耗时约 27 秒。包含原有 26 项 macOS 原生检查和新增 5 项。自检、CLI 帮助及静态检查通过。实渲染截图已查看：`build/ui-preview/proposal-review-light.png`、`proposal-review-dark.png`；两张均为 760×560 内容尺寸，布局无越界且长文本可滚动。

测试核心、socket 和动态作业只使用临时目录；没有真实云端调用、用户网络配置修改、常驻守护启用、替换旧安装、系统提权或发布包。原安装桌面未默认迁移，当前 UID/凭据校验不证明可信应用签名，不能防御恶意同用户代码；Windows 本地传输和原生行为未验证。

整体目标继续进行。仍缺默认安装/服务生命周期、签名应用与提权认证、Windows 写入/传输/完整产品、桌面收据人工核对、模型配置与完整权限隐私/设置、跨核心重启推理、高信任模式、动态效果验证/补偿、真实模型/跨平台支持矩阵以及打包升级。通信合同和源码试用见 [SPEC-005](../../architecture/specs/SPEC-005-local-core-ipc.md)。

### 2026-09-27 桌面模型管理、隐私与收据核对

新增 `preferences.py`、`management_window.py`、`records_window.py`、`receipt_window.py`，扩展核心服务/存储和连接客户端。新增 **25 项测试**：16 项本地管理/客户端检查，9 项实际 Cocoa 检查。

- 模型配置不探测、不调用模型、不启用守护或授予上传同意；数据库、状态和操作记录不含测试密钥明文。换地址不能保留原密钥；已知密钥不能写入名称/地址。重启只还原名称/地址，不恢复密钥或同意。
- 上传确认独立绑定模型修订、地址/数据合同哈希、核心、客户端和用途；换配置、过期、另一客户端、另一用途和撤销后的旧确认均拒绝。撤销能在调查占有核心执行门时生效；隐私审计或操作记录失败不会恢复上传权限。
- 动态私有输出只出现在显式收据读取，不在普通记录中。人工说明、哈希、当前档案和新鲜只读检查共同约束核对；变更收据/档案、缺失勾选/说明、重复确认或取消过程不能解除未知效果。内存和持久任务同步，后续保存保留人工说明，结果不是 `verified`。
- 最近列表以外的未核对任务仍可查询；版本化保留设置拒绝旧版本和额外授权字段。按时间/数量清理已结束任务，不清除十天前的待核对任务。桌面通知默认关闭，首次连接、忙状态和重复状态不发送提醒。
- 实际 Cocoa + 本地 socket 完成配置保存、独立上传同意、撤销、记录列表、私有收据及人工核对往返；此过程未调用真实模型。原生安全密钥输入提交/关闭后清空；模型换址、失联、过期、修订变化、勾选和重复提交均覆盖。
- 实渲染设置、隐私、记录列表与长收据的 760×560 深浅色视图；检查布局范围、文本高度和滚动。截图已查看：`build/ui-preview/settings-light.png`、`privacy-dark.png`、`records-light.png`、`receipt-light.png`，其余深浅色同名变体一并生成。

```sh
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
apps/macos/.venv/bin/python code/network-doctor-menu.py --self-check
apps/macos/.venv/bin/ruff check code tests --select E9,F63,F7,F82
git diff --check
```

完整发现 **397 项，394 项通过、3 项 Windows 原生测试跳过**，耗时约 35 秒。自检及静态检查通过。回归发现并修复普通无脱敏组件导出的字段扩张；新本地记录控制标识单独启用，未扩大旧的普通导出范围。

未修改本机网络、启用用户守护、使用真实云端密钥或真实模型、替换安装副本或打包发布。多原生窗口尚不是统一五视图；真实通知送达、安全凭据库存储、系统权限/安装/卸载、Windows、任务/持续信任、动态结构化效果验证、真实模型诊断收益和跨平台发布验收继续保留。源码合同见 SPEC-005，不以当前 UI 与模拟通过冒充整体验收。

### 2026-09-27 统一工作台、目标总览与修改回读

新增 `workspace_window.py`、`overview.py`、`overview_page.py`、`task_details.py`，连接现有五页与真实 IPC。成熟执行器持久化既有写前/验证/回退读数，不为报告额外调用 OS 工具。新增 **17 项测试**：10 项后端/投影/总览检查，7 项原生工作台检查。

- 成熟成功、回退成功、回退失败、历史多次收据及旧记录缺失字段；实际回读与计划值分开，先比较再脱敏，未知结果不补造。修改后记录读取不增加系统调用。
- 脚本化模型假设、引用与追加目标复查，部分证据不代表全量；步骤不包含工具参数。动态 stdout/stderr、人工说明和恢复路径不在普通详情；无脱敏模块时保持最小投影。
- 核心执行门被探测占用时，记录、任务详情及设置仍可读取；不访问活跃引擎或改变授权。健康标题服从目标而非局部技术项；未检测、过期、断线、待核对、待确认和模型不可用分别表达。
- 五页共享一个窗口，导航不触发 FakeMac 命令、写入或启用守护；保留未保存档案、字段焦点，迟到响应不抢页或丢草稿；关闭工作台清除未提交密钥但核心继续运行。
- 从真实 FakeMac 修复后的总览定位任务、查看回读标签、打开脱敏报告；忽略其他已不再选中任务的迟到详情。待确认、核心忙碌和断线时禁用相应动作。
- 760×560 所有五页深浅色、窄内容布局、长字段文本高度、32 个长目标、滚动位置和内联错误范围检查；新增 Logo/导航采样像素变化检查，不仅检查控件几何。

实际截图揭示内容背景会覆盖侧栏，而视图几何断言仍通过。修正 `FlippedView` 只填充自身 bounds，增加像素回归；这符合新 AppKit 下 dirtyRect 可超出视图范围的绘制要求。[Apple NSView clipsToBounds](https://developer.apple.com/la/documentation/appkit/nsview/clipstobounds)。新增详情分段控件最初先引用尚未创建的 actions，完整回归检出 17 个原生初始化错误；调整创建顺序后，17 项新增检查与全套回归重新通过。

```sh
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_workspace*.py'
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
apps/macos/.venv/bin/python code/network-doctor-menu.py --self-check
apps/macos/.venv/bin/ruff check code tests --select E9,F63,F7,F82
apps/macos/.venv/bin/ruff check code/relay/task_details.py code/relay/overview.py code/relay/overview_page.py code/relay/workspace_window.py code/relay/records_window.py tests/test_workspace.py tests/test_workspace_native.py --select E9,F
git diff --check
```

最终完整发现 **414 项，411 项通过、3 项 Windows 原生测试跳过**，约 48 秒；新增测试单独 17 项通过，约 13 秒。自检和静态检查通过。生成 `build/ui-preview/workspace-{overview,records,profiles,privacy,settings}-{light,dark}.png`；已查看总览浅色、档案深色、隐私深色，以及真实模拟修改后的 `workspace-changes.png`。后者为 1060×700，其余五页为 760×560；截图为实际 Cocoa 控件，不是设计稿。

仅使用临时核心/文件、FakeMac/FakeWindows 和回环模型测试。没有真实网络修改、云端模型调用、用户常驻服务启用、系统提权、安装替换或发布包。完整无障碍、全部五页产品契约、真实通知、签名身份与特权 IPC、安装生命周期、Windows、任务/持续信任、跨核心推理、动态效果/补偿和真实效果/跨平台发布仍需继续完成。当前源码工作台不代替整体交付。

### 2026-09-27 成熟修复任务/持续范围信任

新增 `trust.py`、授权审计及 IPC/原生模式选择，默认仍逐次确认。任务模式最多 15 分钟/3 批，当前核心持续模式最多 8 小时/12 批；绑定具体成熟作用范围，不把模型风险判断当成授权。新增 **26 项测试**：21 项真实本地通信/核心测试，5 项原生 Cocoa 测试。

- 未授权默认不执行；审阅明确字段、值、服务、接口、档案、VPN 和额度。模型同一任务两次匹配方案可使用任务信任，保留同一任务和不同执行收据；后续任务与守护可使用持续范围信任。创建信任不启用守护或上传，只读 `check` 不执行修复。
- 动态命令、额外字段/权限、不同目标值、接口、服务、档案或 VPN 归属均不能继承范围。代理写入前改变 VPN 归属时被拒绝，未写入。Wi-Fi 观测补充既有配置的接口名，代理/IPv6 也复核已有 VPN 上下文。
- 壁钟过期/倒退、单调时钟到期、额度耗尽、任务结束和撤销阻止后续操作。真实额度预留达到上限后不能再签发，准备失败不退款，预留写盘失败时零写入。
- 审阅绑定客户端、内容、用途、期限和修订；重复请求返回原操作不再执行。接受后等待执行门期间撤销，使尚未开始的范围授权失效。第一字段写入后撤销，下一字段不写，已写字段回退并记录实际读数。
- 非验证成功结果暂停自动执行。记录失败仍撤销内存权限；正常/异常重启不恢复旧可执行信任。关闭时即使撤销审计失败也释放核心和记录锁，可再次打开数据目录。
- 已启用守护使用范围信任时，真实状态查询显示 `applying` 和对应自动处理标题，不把写入显示成普通检查；完成后清除活跃执行状态。通过 IPC 在前置核对期间暂停守护，暂停立即返回且该批不写入，不等待整个处理完毕。
- 原生确认默认逐次，选择任务/持续模式需独立勾选；切换模式或重新审阅清空选择/勾选。取消不创建权限。失联或信任修订变化使确认失效。真实原生界面经 IPC 完成持续授权、成熟修复和隐私页独立撤销。
- 760×560 深浅色审阅页、长范围/JSON 滚动与控件边界，活动范围隐私页文字高度/滚动检查通过。已查看 `build/ui-preview/trust-review-light.png`、`trust-review-dark.png`、`trust-privacy-dark.png` 实际 Cocoa 渲染。

回归发现通用脱敏器按设计隐藏 `authorization` 字段，导致新详情读取字符串而非映射。修正为专用 `permission`/`basis` 枚举投影，未放宽凭据脱敏规则；追加报告依据与敏感认证头继续隐藏的回归检查。暂停守护改为紧急权限收缩方法，确保不会排队等到自动修改结束才生效。

```sh
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_trust*.py'
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
apps/macos/.venv/bin/python code/network-doctor-menu.py --self-check
apps/macos/.venv/bin/ruff check code tests --select E9,F63,F7,F82
apps/macos/.venv/bin/ruff check code/relay/trust.py code/relay/core.py code/relay/core_service.py code/relay/proposal_window.py code/relay/management_window.py code/relay/overview.py tests/test_trust.py tests/test_trust_native.py --select E9,F
git diff --check
```

最终全量 **440 项，437 项通过、3 项 Windows 原生跳过**，约 69 秒；自检、静态检查和 diff 检查通过。早期新增套件 25 项单独通过，后增加关闭失败回归一项并在全量中通过。所有网络修改是 FakeMac/FakeWindows，模型为脚本和既有回环测试。未修改本机网络、启用真实用户守护、使用云端模型/密钥、提权、替换安装或发布包。

该证据证明源码核心内的成熟范围授权合同，不证明任意动态脚本、跨核心静默恢复、可信应用身份/系统提权或真实长期稳定性。Windows、安装生命周期、动态效果/补偿、跨核心推理、无障碍、真实模型与跨平台支持矩阵及发布仍未完成，继续按完整蓝图实施。合同见 SPEC-006。

### 2026-09-27 独立启动、收尾停机与服务生命周期

新增 23 项生命周期检查、6 项实际 Cocoa 检查。网络命令仍全部在 FakeMac 中；SMAppService register/unregister/kickstart 仅模拟，不改本机登录项。

- 实际子进程以固定命令、私有临时目录和过滤环境启动；第二客户端重连同实例，普通重开尊重停机抑制位，显式启动获得新实例。已有未连接运行时持有记录锁时拒绝启动/结束它。重复生命周期操作被锁拒绝；启动超时不杀子进程或在同管理器里盲目再起。
- 用户开启守护的意图可经服务变更重启恢复；完整停机清除意图，模型同意和范围信任不恢复。未启用守护的空闲生命周期不产生 FakeMac 探测或写入。
- shutdown 受理/收尾分离、重复 ID 去重、draining 拒绝新操作；回执在数据库锁释放后可见。第一字段写入后停止，第二字段保持原值，第一字段实际回退并保存读数。操作日志或回执写失败不冒充已完成停机。
- 丢失停机响应只接收原实例/请求的回执，调用一次；旧回执不能证明当前停机，超时不注销。注册待系统批准时不启动临时进程兜底；已注册但干净停止的任务重新启用时显式启动。注册失败、未确认注销和不支持状态查询均不报告卸载准备完成，资料保留。
- 原生确认取消不注册；待批准状态与系统设置入口可见；收尾异步进行，期间正常退出不打断服务序列。卸载准备完成不退出桌面、不删除资料。760×560 深浅色与长状态行均检查；实际截图发现离线占位遮住生命周期控制，修复为离线可见区域并增加控件可见性和像素断言。

```sh
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
apps/macos/.venv/bin/ruff check code tests --select E9,F63,F7,F82
scripts/build-macos.sh --self-check
apps/macos/.venv/bin/python scripts/verify_lifecycle_bundle.py
git diff --check
```

最终全量 **469 项，466 项通过、3 项 Windows 原生跳过**，约 75 秒。新增检查早期 25 项通过，补充 4 项边界后全量通过。最后复查修正前台 watch 的持久意图回归，并加强原测试直接读取数据库；普通关闭先撤销执行权限，再等待通信客户端退出，新增顺序断言。核心不知道系统注册状态时返回 null，而非错误声称未启用。静态检查与 diff 检查通过。实际查看 `build/ui-preview/lifecycle-light.png`、`lifecycle-dark.png`，修复后两种外观再次查看；五视图原有深浅色/文本范围测试全量保留。

新构建补齐工作台图标、Agent/ServiceManagement 模块、LaunchAgent plist，冻结包自检通过。锁定依赖增加 ServiceManagement 11.1；重新解析时四个间接/开发依赖降为 filelock 4.0.1、platformdirs 4.11.12、pyparsing 3.3.2、ruff 0.16.8，尝试保留原版本被索引中 ruff 0.16.9 不可取得所阻断，完整测试/构建基于新锁文件。本地 arm64 ad-hoc 签名、ZIP 解压严格验签通过；不是 Developer ID 公证发行。最终 ZIP SHA-256 和详细输入哈希在 `dist/macos/build-metadata.json`。

`verify_lifecycle_bundle.py` 从该冻结 App 启动预置中性配置的临时核心，启动器进程退出后另一进程再次连接同核心，再正常停机。断言守护未启用、没有开始检查、模型无上传同意、无范围授权、未注册服务、数据保留。最终源码输入哈希与 metadata 全部一致，ZIP SHA-256 为 `e3d23d311347e02a19fe64fc98ef016f0d06afa48eb5c85d96a904eb9a3b429e`。只在临时目录运行，不接触默认用户数据。构建未替换 `/Applications` 安装副本，未调用真实模型、修改本机网络或提权。

默认入口仍保留旧账号/商业历史桌面；新 Agent 需显式入口。真实服务注册/系统批准/登录退出/卸载、签名身份与特权执行器、Windows、跨核心续接、动态结构化效果、真实模型效果、无障碍和升级/发布仍未完成。当前证据是普通核心生命周期阶段，不是整个 PRD-002 的验收完成。

### 2026-09-27 默认 Agent 工作台与兼容迁移

源码 `network-doctor-menu.py` 和新构建 App 默认进入连接独立核心的工作台。提取共享 `DesktopServices`，接入既有账号、Pro 历史和报告逻辑；旧菜单仅以 `--legacy-desktop` 显式运行。检测预设/重新识别从界面移入核心操作门，保留严格修订与外部文件变更检查。新增 **26 项测试**：14 项桌面服务/默认入口、7 项配置 IPC、5 项实际 Cocoa 工作流；旧菜单 35 项和原商业签名/历史套件保持回归。

- 免费单次导出仅含脱敏字段、私有文件权限、不创建 Pro 历史；账号恢复延迟后补记当前快照，已提交结果去重；旧 SQLite 内容可继续读取。真实内存 Ed25519 权益离线可用、过期拒绝导出，目录选择后再次核验权益，数据保留。
- 慢权益查询与不可用历史库不阻塞界面缓存、基础报告或核心检查。实际 Cocoa/IPC 中账号同步阻塞时仍能检测、打开免费任务记录并导出。账号登录取消的迟到成功被清除；登录/退出不改变核心守护、范围修订或独立模型上传同意。没有向真实 Keychain、浏览器或账号服务发请求。
- 原生设置登录/取消/退出，记录页历史/比较/导出真实点击往返；Pro 到期仅拒绝商业动作，Agent 记录保留。历史概览按实际状态显示中文摘要，完整脱敏数据独立查看；缺少检查结果或错误不补造健康。
- 检测预设仅接受四个枚举与配置修订；待确认方案期间拒绝修改，旧修订或磁盘外部改动不会覆盖文件。只读重新识别保持 HealthProfile 及明确公司策略。保存失败保留内存和文件、恢复原守护；取消期间不保存、不重启守护。成功配置使旧范围信任暂停失效，但保留独立模型同意。
- 默认/显式服务角色转发不初始化旧运行时，显式旧入口拒绝混合参数；同目录第二桌面启动被拒绝。既有原生测试全部注入未配置账号夹具，避免偶然读取开发者商业配置。
- 760×560 长账号字段的设置深浅色及离线可见性检查；核心断开后账号仍可用，核心写控件禁用/隐藏。已实际查看 `build/ui-preview/account-workspace-light.png`、`account-workspace-dark.png` 和最终文字版 `history-report.png`，没有文本溢出或侧栏遮挡。

首次新配置测试发现两处测试错误（`patch` 应为 `patch.object`，配置变化应断言 suspended 而非用户 revoked），修正后通过。原生真实点击抓到 `compare:` 被 PyObjC 按 Cocoa 排序方法解释为需返回值，已改用独立选择器 `compareHistory:`。离线测试改为等待实际 UI 收到状态，而非只等待后台缓存变化。增加账号初始化晚于首次检测的回归，避免当次历史永久漏记。

```sh
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
apps/macos/.venv/bin/python code/network-doctor-menu.py --self-check
apps/macos/.venv/bin/ruff check code tests scripts/verify_lifecycle_bundle.py --select E9,F63,F7,F82
scripts/build-macos.sh --self-check
dist/macos/Relay.app/Contents/MacOS/Relay --help
apps/macos/.venv/bin/python scripts/verify_lifecycle_bundle.py
git diff --check
```

最终全量 **495 项，492 项通过、3 项 Windows 原生跳过**，约 89 秒；新增桌面相关 19 项单独通过，原全量 494 项通过后补充一项摘要语义检查并重跑全部。静态检查（全库关键错误和变更模块 E9/F）、源码自检、差异空白检查通过。

最终 arm64 ad-hoc 开发包在私有 staging 构建，自检报告 `desktopEntry=agent`；ZIP 解压严格验签通过。冻结入口 `--help` 返回 Agent 生命周期参数；临时空闲核心再次完成父启动器退出、重连同实例、正常停机并保留数据，断言未启动检测/守护、未注册服务。67 个输入哈希与当前源码一致，最终 ZIP SHA-256 为 `8ef9242266f354fd9f8cdabf9ea26d793d3f0dc2ebbfcf71385b2cf18f1c8099`，详见 `dist/macos/build-metadata.json`。

未替换已安装 App、读取真实账号秘密、操作本机网络、调用云端模型、注册用户常驻服务、提权或正式发布。Pro 历史不是后台 Agent 日志，暂不承诺桌面关闭期间完整商业快照；真实双击安装/升级、Keychain/官网往返、Windows、可信签名与独立特权执行、跨核心推理/信任、动态效果/补偿、无障碍、真实模型质量和整体验收仍在完整目标中。合同见 SPEC-008。

### 2026-09-27 签名桌面与普通核心通信

新增 8 项身份/选择检查、7 项真实原生 NSXPC 检查、1 项实际 Cocoa 生命周期检查。签名构建不使用用户文件作身份根，不降级开发 socket；用户服务 plist 声明固定 Mach service。复用既有 CoreService、业务授权与 journal，而不是新增另一套执行规则。

- 合成签名信息验证 Developer ID 链、hardened runtime、调试状态、固定标识、团队、代码哈希及危险 entitlement；无效签名、缺少原生桥、签名模式系统版本不足失败。有效 ad-hoc 仍明确为开发模式。签名客户端拒绝旧 socket，开发客户端不能凭描述文件冒充签名连接；签名服务拒绝未管理/自定义目录入口。
- 真实编译的 Objective-C 协议与 NSData block 经匿名 NSXPC endpoint 完成往返。双方以当前 Python 的实际 ad-hoc cdhash 为测试 requirement；错误服务端签名条件时只有无敏感握手可能发出，业务处理器不被调用。错误客户端条件也被系统拒绝。
- 用户/登录会话不匹配、伪造实例、错误 UUID 和超过 1 MiB 的业务帧拒绝；描述文件不能指定其他服务或替换签名信任条件。接收方核对内核身份，不按 PID 查进程名授权。
- 响应超时不重放；断线后已接受处理仍由既有核心负责。实际阻塞处理器验证服务关闭等待活动请求退出，避免提前释放所有权。错误仅返回固定业务原因/系统数值码，不泄露私有参数或异常内容。
- 注入真实 XPC 服务工厂的 CoreService 在 FakeMac 上完成检测、方案审阅、确认、请求 ID 去重和私有任务收据读取；结果为 verified，第二次相同请求未重复执行。传输变化不绕过原授权流程。
- 签名服务未注册/待批准时原生启动按钮禁用，登录服务和系统设置入口可用；显示明确等待状态，不自动调用注册。模拟批准后可显式启动。760×560 两种外观、文本高度与像素检查通过，已查看 `build/ui-preview/signed-service-light.png`、`signed-service-dark.png`。

原生调试最初发现 reply block 的 PyObjC 元数据与 clang 协议的 NSData 签名不一致，系统以 4097 拒绝解码；改为准确的 block/NSData 元数据后真实往返通过。测试曾错误读取收据投影层级，按实际 `task_detail.record.outcome` 修正断言，不修改业务结果迎合测试。旧 plist 测试更新为精确校验所需 MachServices，同时保留无 root UserName 和干净停止不重启的约束。

```sh
apps/macos/.venv/bin/python scripts/build-native-ipc.py
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
apps/macos/.venv/bin/ruff check code tests scripts/build-native-ipc.py --select E9,F63,F7,F82
scripts/build-macos.sh --self-check
apps/macos/.venv/bin/python scripts/verify_lifecycle_bundle.py
git diff --check
```

最终全量 **511 项，508 项通过、3 项 Windows 原生跳过**，91.502 秒；添加最后一项 Cocoa 检查前的 510 项全量也通过。关键静态检查和新模块完整 E9/F 检查通过，差异空白检查通过。

arm64 ad-hoc 包冻结自检报告 `desktopEntry=agent`、`optional.signedIPC=false`、无错误；后者准确说明这不是 Developer ID 包。原生桥已打包，ZIP 解压严格验签通过。冻结临时空闲核心完成独立启动、重连、正常停机和数据保留，未发起检测/守护或注册服务。71 个输入哈希与当前源码一致，最终 ZIP SHA-256 为 `ac0e74aeb5d6dacef433281c8b3e2857b824260d0b84757a0b6fa425b18bff0d`，见 `dist/macos/build-metadata.json`。

这些原生证据使用同进程匿名 endpoint 和开发代码哈希；Developer ID 正向策略仅有合成信息测试，不能证明真实签名、不同进程命名服务、SMAppService 安装/登录/批准或升级已验收。没有注册真实服务、调用模型/账号、修改本机网络、使用发行证书、提权、替换安装或发布。普通核心 XPC 不是高权限 helper，不能据此勾选 F5.4 完成；精确构建匹配要求更新前先安全停旧核心。其余完整蓝图要求继续推进，合同和官方依据见 SPEC-009。

### 2026-09-27 Mac 优先与原生配置事务

当前交付范围按用户新决定聚焦 Mac，Windows 后续可选，原生 Windows 跳过项不再作为本轮完成阻塞。新增 **22 项 Objective-C 执行器检查与 6 项 Python 调用合同检查**。SystemConfiguration 生产适配编译进原生桥，但测试只注入内存配置，文件仅为私有临时目录；没有对当前电脑调用网络配置写入 API。

- prepared 意图在提交回调前已经落盘，含精确请求、原配置与目标配置；文件 0600。已有 ID 返回原收据，冲突内容拒绝；释放执行器再打开仍去重，第二执行器不能同时持有同目录。
- 未知字段、额外键、非法 ID/哈希、DNS 名称/zone/超长列表、错误类型、不可支持 IPv6 模式和不完整端点拒绝。用户及会话是传入的测试参数，拒绝系统账号/未知会话，不把这项单元检查冒充 OS 对端鉴权。
- 原值漂移、同名不同 service ID、代理端点变化均不写入。恢复必须引用原用户/会话/方案/目标的正向操作，错误反向值和外部第三方值保留。DNS 空/缺失、代理 Enable 缺失、IPv6 禁用前参数及无关新配置均有恢复断言。
- 提交失败、应用失败、回读不同或未知不记 configured。重复请求不再次提交；未知记录在当前实例与重新打开后阻止新普通写入，针对原操作的成功恢复可收尾。未测试、未提供全部原会话消失/外部已恢复场景的用户核对入口。
- 不安全目录、叶节点链接、收据链接/损坏/宽权限拒绝；目录失去写权限时意图写失败且没有配置提交。另一请求在正在执行时得到 busy。进程间锁针对同目录，不等同于尚未实现的全设备整批修复租约。
- Agent 没有 grant 时原生零写入；取得授权后，其真实方案哈希同时出现在本地恢复记录与原生请求。原生 configured 收据明确网络尚未验证，Agent 经过 FakeMac 复测才得到 verified；模拟退化后通过引用原操作恢复，结果为 rolled_back，没有再调用 networksetup setter 双写。
- 同名服务重建使获准方案失效；系统服务 ID 也进入成熟范围信任，不能仅凭相同服务名继承。调用层拒绝不匹配的操作 ID/结果/布尔状态、过滤私有错误内容；超时仅记录未确认，不重试。

联测修正了 Objective-C 与 MacTypes 的 Boolean 名称冲突，并将测试边界统一为真实 JSON 编解码：不能把 PyObjC 映射代理/NSNumber 对象误当普通 Python dict/bool，也没有放宽生产收据的严格类型要求。本机原生地址解析路径曾接受带 zone 文本，现显式限制地址字符范围；重读目录索引前重置游标，避免共享目录文件描述符偏移漏掉中断记录。原生回读使用新 SCPreferences 会话，代码不以当前写入缓存证明持久成功；实际系统 API 行为仍待隔离实机验证。

```sh
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_mutation*.py'
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
apps/macos/.venv/bin/ruff check code tests scripts/build-native-ipc.py --select E9,F63,F7,F82
scripts/build-macos.sh --self-check
apps/macos/.venv/bin/python scripts/verify_lifecycle_bundle.py
git diff --check
```

新增 28 项单独全部通过。最终完整 **539 项，536 项通过、3 项 Windows 原生跳过**，92.072 秒；早期 528 项全量也通过。关键静态检查、变更模块 E9/F 和差异空白检查通过。本轮没有改界面布局，既有实际 Cocoa 深浅色及最小窗口检查继续包含在全量回归中。

arm64 ad-hoc 开发包自检 `ok=true`、`desktopEntry=agent`、`optional.signedIPC=false`；解压严格验签通过。临时冻结核心独立启动、重连、正常停机、资料保留，检测/守护/服务注册均未发生。74 个构建输入哈希全部匹配，最终 ZIP SHA-256 为 `72be3c35c4bd88f7c66c8a8e64412d7dc6c68860ce7b39eacca6ccb78acfd51a`，见 `dist/macos/build-metadata.json`。

原生模块现为可注入的事务实现，默认 CoreRuntime 尚未切换至辅助服务；没有 root daemon、OS 授权及高权限调用鉴权的完成证据。固定 root 目录、整批跨用户协调、未知意图核对/清理、进程生命周期及真实 SCPreferences/MDM/签名安装必须继续完成；不能把内存配置测试当成已具备提权能力。未修改本机网络、提权、注册服务、使用真实账号/模型、替换安装或正式发布。完整 Mac 目标仍进行中，见 SPEC-010。

### 2026-09-27 独立原生辅助服务与显式系统批准

新增 15 项原生 helper 检查、9 项服务/生命周期检查、3 项 Cocoa 检查，以及 1 项原生 ACL 正负例，共 28 项。测试编译实际 Objective-C helper/协议/配置执行器，但只使用匿名 XPC endpoint、当前 Python 的实际开发 cdhash、内存网络配置和私有临时文件；不会启动 root daemon 或调用宿主网络写入。

- 握手后实际取得内核身份再发业务请求，错误服务端 requirement/UID 或错误客户端 requirement 均拒绝，配置提交为零。schema 拒绝缺失/错误反向关系/过多动作；未取得批次、清单外动作、其他客户端写入均拒绝。系统域使用 SDK 的 `NSXPCConnectionPrivileged`，不是自行猜测其数值。
- 正确调用经过 begin/perform/end，重复原操作只提交一次，读取服务状态显示批次从占用到结清。正向时限过期仍可恢复；未确认提交不能按 verified 结清，现场为原值时只读核对收尾。恢复过程保留原值，不重复网络 setter。
- 活动批次阻止其他客户端 begin/drain；drain 后不能写入，显式 resume 后才可新建。构造另一用户所有的持久 drain 状态，当前真实连接不能解除或替换。此项不是双真实登录账号矩阵测试。
- 辅助实例重建保留 batch，但旧客户端实例绑定阻止写入；修改持久身份时启动拒绝。关闭原生服务等待已进入的内存 commit 退出，提交仅一次。辅助二进制的只读身份检查通过，非 root 默认入口返回 77；ad-hoc `--verify-pair` 返回失败且没有网络或文件写入。
- Agent 的真实授权方案经过辅助传输、原生事务和 FakeMac 网络复查得到 verified；networksetup setter 没有被再次调用。模拟 end 丢失回执时，最终 outcome 为待核对，恢复文件为 needs_verification/unconfirmed。重复结束查询仅返回原准确结果，不再改配置。
- 系统服务适配在源码/不受支持入口不注册、不连接。注册待批准时不调用 helper；已批准时独立 resume。未结束任务、未知状态或权限撤销后的连接失败，不执行 unregister。drain 成功且系统注销状态确认才完成；重复已注销操作无额外调用。
- 生命周期先正常停止核心并取得所有权释放，再变更系统服务；helper 失败阻止卸载普通服务。卸载顺序 helper → 用户 LaunchAgent，保留数据。无效布尔选项和不可用入口不先停止核心；登录启动与系统修复互不隐式注册。
- Cocoa 独立确认可取消，取消恢复复选框。批准等待、批准后明确激活、不可用/忙时禁用均覆盖。760×560 深浅色下检查文本高度/边界/像素，查看 `build/ui-preview/helper-approval-light.png` 与 `helper-approval-dark.png`，滚动后的权限核对、系统设置与移除入口无重叠。

```sh
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_helper_native.py'
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_lifecycle_native.py'
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
apps/macos/.venv/bin/ruff check code tests scripts/build-native-ipc.py scripts/verify_lifecycle_bundle.py --select E9,F63,F7,F82
apps/macos/.venv/bin/python code/network-doctor-menu.py --self-check
bash -n scripts/build-macos.sh scripts/sign-macos.sh
```

ACL 补强前全量 **566 项，563 项通过、3 项 Windows 原生跳过**，97.043 秒；补充跨用户 drain 与批准后激活前的 564 项全量也通过。原生 helper 15 项、Cocoa 生命周期 10 项分别全通过，服务/生命周期 32 项通过。新模块 E9/F、全库关键静态错误、源码自检与 shell 语法检查通过。

收尾检查补上扩展 ACL 校验：临时目录、锁文件和收据保持 0700/0600，但增加 allow ACE 时均拒绝；仅 deny ACE 不扩大权限，原收据仍可读取。初始化失败也关闭已打开的描述符。首次实现把正常无 ACL 的 fd 返回当错误，导致用例准备失败；依据本机 API 实测及 Apple libc 核对后区分 ENOENT 与其他失败，新增负例及原生事务 23 项全部通过。只改变本轮临时目录的 ACL，未修改系统或用户既有目录权限。

ACL 补强后最终全量 **567 项，564 项通过、3 项 Windows 原生跳过**，96.291 秒。再次通过关键静态检查、变更文件 E9/F 与差异空白检查。

最终 arm64 ad-hoc 开发包包含独立 helper 与 LaunchDaemon plist，自检 `ok=true`、`desktopEntry=agent`、`signedIPC=false`、`signedHelperPair=false`，准确表明尚非发行身份。辅助程序只读自检报告有效 ad-hoc 签名及固定 helper ID；ZIP 解压严格验签通过。最终 ZIP SHA-256 为 `40b6c82b9d7e4ea690e4a801ddc033c67abc685bcbb8a7b9bb9b813aa31f4711`，85 个输入哈希全部匹配当前源码，见 `dist/macos/build-metadata.json`。

`scripts/verify_lifecycle_bundle.py` 再次通过：冻结父入口退出后独立核心可重连同一实例，正常停止并释放记录所有权，资料保留；没有开始检测、开启守护或注册服务。实际构建命令为 `scripts/build-macos.sh --self-check`，核验只使用私有 staging 和临时数据目录，未替换安装副本。

签名脚本增加显式 helper ID、外层 Info 的 cdhash 固定、签名完成后的只读配对检查；没有实际执行 Developer ID 签名/公证，因此不把脚本实现或 ad-hoc 负例当正向发行证明。SCPreferences 的真实拒绝/应用/回读、MDM、root 服务注册/系统批准、跨登录会话、第二台 Mac 及真实模型质量均未验收。跨实例恢复和受信任版本迁移仍需实现，详见 SPEC-011。未操作本机网络、注册常驻服务、读取真实账号秘密、使用发行证书、提权、替换安装或发布。

### 2026-09-27 中断配置的重新审阅与恢复

新增 **30 项**，均使用真实 Objective-C/匿名 NSXPC、内存配置、私有临时文件与 FakeMac 检测；不是实网或 root 安装验收。

- 原批次 pending 时，即使当前 DNS 看似健康，普通只读检测也不解除本地任务。原生终态的 batch ID、方案哈希和完整 apply/restore 清单必须与本地恢复资料一致；缺失或不符保持待核对。
- 恢复审阅输出准确目标与原值/现值，私有完整协议配置不上传到客户端。其他客户端不能接管有效租约；期限/辅助实例变化后允许同 UID 新客户端重新审阅，旧令牌/旧 perform 拒绝。
- 审阅绑定当前私有配置与记录；包括无关配置的漂移也会使旧确认失效。第三方字段值和未知读取均禁止覆盖。确认单次、绑定客户端/会话/实例，已用令牌不能再次写入。
- 恢复接管及新补偿 ID 先落盘，断线后没有重发；应答丢失但配置已恢复时，新审阅只读结清，提交次数保持 2（一次原修改、一次恢复）。helper 重启后也不重放恢复。
- 未确认的正向/回退记录可经明确审阅、现场核对补齐终态，但原历史 result 不会被改写为成功执行。正常 end 如遇未知回退则保持批次，不提前清空。
- 独立终态历史在后续批次后仍可查询。构造终态落盘但 control 尚未清空的崩溃窗口，只补元数据。没有开始的操作不做无意义网络读写。
- Core 的确认拒绝缺少勾选、非法选择/额外字段、他人客户端、旧哈希、过期、取消、档案变化、落盘失败或修改后的清单；原生请求不由桌面提供。失去 recovery_apply 回复后，相同 IPC ID 只返回原失败结果，新审阅可只读完成。
- 保留现值必须通过新鲜网络复验；模拟网络退化后原生与本地批次仍待核对，后续明确恢复原值可结清。取消发生在复验中也不自动完成；配置已经恢复不冒充网络健康。
- 完整 Cocoa 按钮 → Unix IPC → Core → 原生辅助 XPC → 内存事务 → FakeMac 复验通过。原任务持久结果与内存同步，原执行收据保持不变，没有重复普通 networksetup setter。recovery_end 真实完成但回复丢失后，通过历史终态及只读检查解除本地阻止。
- 最小 760×560 深浅色、说明文本、原值/现值、底部授权与两个按钮边界/不重叠检查通过。查看 `build/ui-preview/recovery-light.png`、`recovery-dark.png`；断线、核心变化、忙、窗口关闭、过期和不可恢复状态禁用按钮。动态人工核对入口保持独立。

最初原生恢复检查因 Objective-C 复合布尔表达式被装箱为 JSON 0/1 而被 Python 拒绝，已改为明确 Boolean，未放宽契约。贯通 fixture 的 FakeMac 初始化曾改变 DNS 而未同步内存原生配置，已统一初始化顺序，且重启 fixture 不重置当前现场。

早期完整 596 项联跑有 13 项在 helper 初始调用五秒未获回复，全部在配置写入前安全停止；494/330 项顺序组合也曾复现，独立/分组测试通过。随后完整 **596 项（101.981 秒）与 597 项（104.432 秒）通过**，但未稳定隔离超时根因。诊断没有放宽签名、期限、授权或补发写请求。测试之后避免重编当前进程已加载的原生 Mach-O，防止改变在用测试产物；不能据此断言超时根因已消除，仍保留长时间运行与独立 root 服务验证事项。

```sh
RELAY_NATIVE_UI_TESTS=1 apps/macos/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
apps/macos/.venv/bin/ruff check code tests scripts/build-native-ipc.py scripts/verify_lifecycle_bundle.py --select E9,F63,F7,F82
bash -n scripts/build-macos.sh scripts/sign-macos.sh
scripts/build-macos.sh --self-check
apps/macos/.venv/bin/python scripts/verify_lifecycle_bundle.py
git diff --check
```

helper 安全保留清理和跨版本配对迁移、任意动态效果/设备级协调、真实 root/MDM/管理员批准/系统网络读写、真实模型质量和第二台 Mac 均不由这些开发测试替代。没有改宿主网络、注册服务、使用真实凭据、提权、替换安装或正式发布。Windows 原生跳过不阻塞本轮 Mac 目标。

避免重编已加载原生库后的最终全量 **597 项，594 项通过，3 项 Windows 原生跳过**，101.430 秒。全库关键静态检查、变更模块完整 E9/F、构建/签名脚本语法及差异空白检查通过。此前两轮通过不替代早期超时的根因定位；该未关闭验证事项继续保留。

最终 arm64 ad-hoc 开发包自检 `ok=true`、`desktopEntry=agent`、`signedIPC=false`、`signedHelperPair=false`，准确反映开发身份。独立 helper 只读签名自检通过；ZIP 解压严格验签通过。85 个构建输入哈希全部匹配，ZIP SHA-256 为 `d3afc49f6a40b137f173451f19d8c41befa54bfaa4c1f529a293ddb7f89610e8`，见 `dist/macos/build-metadata.json`。

`verify_lifecycle_bundle.py` 使用私有临时数据目录完成冻结核心独立启动、同实例重连、正常停机、锁释放与数据保留；输出明确 `guard_enabled=false`、`check_started=false`、`service_registered=false`。没有替换安装副本，也没有发起真实网络检测或系统修改。
