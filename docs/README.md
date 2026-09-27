# NetCare 文档导航

目录参考 `202609-typeleast` 的职责划分；旧的编号文档保留原位置。

- [NetCare 命名与兼容性](product/BRAND-001-netcare.md)：对外名称、内部保留标识和本次更名范围。
- [当前产品与实现全景](product/REVIEW-001-mac-agent-current-state.md)：整体目标暂停后的功能、交互、架构和阶段验收 review。
- [商业化 PRD](product/prds/PRD-001-commercialization.md)：用户、免费/付费范围、初始价格提案与发布条件。
- [自主网络保障 Agent 目标](product/prds/PRD-002-autonomous-network-agent.md)：最新确认的主动发现、授权执行与恢复验证方向；实施及默认权限策略待细化。[领域术语](../CONTEXT.md)。
- [自主网络保障终局蓝图](architecture/BLUEPRINT-001-autonomous-network-agent.md)：已获准按步骤实施的功能组合、信息架构、UI 品质、本地 Agent 技术架构与演进路径。
- [Agent 实施与验收台账](engineering/tickets/TKT-002-autonomous-agent-delivery.md)：完整范围、当前证据、未完成项和实施顺序。
- [只读核心与平台证据契约](architecture/specs/SPEC-002-readonly-core.md)：Windows 作用域、无界面核心生命周期、记录锁与脱敏输出；[Windows 预览运行](../apps/windows/README.md)。
- [模型调查循环](architecture/specs/SPEC-003-reasoning-loop.md)：显式同意、v2 最小证据、假设/工具/验证、授权后同任务续接、累计预算、离线降级及 CLI 入口。
- [动态命令与执行收据](architecture/specs/SPEC-004-dynamic-jobs.md)：macOS 核心的逐次交互确认、作业监督、崩溃收据与人工核对；不是网络沙箱，Windows/提权及完整桌面集成仍待完成。
- [本地核心通信与连接式桌面](architecture/specs/SPEC-005-local-core-ipc.md)：版本化私有 Unix socket、同用户/实例校验、操作去重、断线查询、原生方案/收据审阅、模型设置与独立上传同意；不是签名应用或特权身份认证。
- [成熟修复范围信任](architecture/specs/SPEC-006-scoped-repair-trust.md)：默认逐次确认，可显式选择任务或当前核心内持续信任；具体范围、额度、撤销、风险升级和重启暂停。
- [核心生命周期](architecture/specs/SPEC-007-core-lifecycle.md)：独立 Agent 启动、停机收尾回执、守护选择恢复、macOS 用户登录服务与保留数据的卸载准备；真实服务注册及签名安装仍待验收。
- [默认 Agent 工作台与兼容](architecture/specs/SPEC-008-default-agent-desktop.md)：默认入口切换、共享账号/Pro 历史、免费任务记录、核心检测设置与旧目录保护；不替代真实安装升级验收。
- [签名桌面与核心通信](architecture/specs/SPEC-009-signed-core-transport.md)：Developer ID 构建的双向 XPC 身份、用户/会话绑定和先认证后发业务参数；不降级为开发 socket，尚未验收正式签名服务或高权限执行器。
- [Mac 原生配置事务](architecture/specs/SPEC-010-native-config-mutations.md)：准确服务标识、持锁比较原值、先记录后写入、私有收据去重和绑定原操作的恢复；已与 Agent 模拟联测，尚未部署 root 辅助服务。
- [Mac 独立配置辅助服务](architecture/specs/SPEC-011-native-helper-service.md)：配对签名、内核身份、固定 root 数据目录、整批配置协调和显式系统批准/注销；实际安装和升级仍待验收或实现。
- [Mac 中断配置恢复](architecture/specs/SPEC-012-helper-recovery.md)：双份记录核对、同账号重新审阅、精确恢复/保留、终态历史与 Cocoa 入口；开发联测通过，真实 root/实网和跨版本迁移未验收。
- [商业化 Spec](architecture/specs/SPEC-001-commercialization.md)：HTTP、签名权益、桌面与官网契约。
- [实施任务](engineering/tickets/TKT-001-commercialization.md)：分工、依赖、测试与回退。
- [官网设计](design/DESIGN-001-relay-website.md)：页面、主题、交互与浏览器验证。
- [安全与数据](security/SEC-001-data-and-threat-model.md)：身份、支付、诊断数据与保留边界。
- [交付验收](quality/reports/QA-001-commercialization.md)：实测结果、证据与生产待办。
- [已有品牌设计](06-品牌设计.md)、[通用化方案](07-通用性改造方案.md)。
- [09-26 网络诊断记录](09-2026-09-26-Codex连接诊断.md)：本次商业化前的实机现状。
- [通用网络 Agent 研究](research/RESEARCH-002-general-network-agent.md)：行业实践、场景矩阵、工作流与安全修复提案；附 [Windows/macOS 能力与权限研究](research/RESEARCH-001-platform-network-capabilities.md)。均为研究提案，不代表已实现。

产品提案、实现验证、本地演示与正式上线是不同状态。真实收款和公开分发须有商户、签名公证、SMTP 与生产环境验证记录。
