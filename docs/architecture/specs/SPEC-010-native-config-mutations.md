# SPEC-010：Mac 原生配置事务

后续更新：独立辅助程序、鉴权调用、整批租约与系统注册入口现已接入，见 [SPEC-011](SPEC-011-native-helper-service.md)。下文保留本阶段首次交付边界；实际 root 安装/系统写入仍未验收。

2026-09-27。用户已将当前交付聚焦为完整 Mac 版本，Windows 后续可选。这一阶段实现独立特权执行器所需的原生配置事务及其 Agent 接口，**尚未实现或启用 root 辅助服务的发行调用路径**。默认 CoreRuntime 仍使用原有普通权限修复；没有通过自制密码框或 sudo 临时替代系统授权。

## 实现与接口

`apps/macos/native/RelayMutation.h/.m` 包含事务执行器与 SystemConfiguration 适配。`RelayPreferencesAccess` 提供持锁读取、提交、重新读取和解锁；生产适配使用 SCPreferences/SCNetworkService/SCNetworkProtocol，原生测试注入内存配置。现编入既有 RelayIPC 原生库，不是独立 daemon。

`code/relay/mutations.py` 的 `MacMutationWriter` 接收显式 transport 与 target_resolver。它不是自带认证的传输，也不会根据用户文件取得提权能力。后续辅助服务必须从实际连接取得可信 UID/audit session，再调用原生执行器；不能信任请求里自报的用户身份。目前测试中的身份是合成参数，不代表 OS 鉴权已通过。

FixEngine 可注入该 writer。原生目标包含系统 service ID、服务名和 BSD 接口，并在方案中固定；授权后的再次规划和写前复核不能将同名新服务当作原目标。范围信任也包含该标识。Agent 向修复引擎传入真实授权方案哈希；没有授权回调或有效哈希时，不允许使用此写入路径。默认未注入 writer 的源码/开发包行为保持不变。

## 单项事务

1. 严格核验协议、操作 ID、方案哈希、目标、字段、期望值、目标值、代理端点及恢复来源。只支持最多 16 个字面量 DNS 地址（不含 IPv6 zone）、IPv6 Off/Automatic/Link-local only、HTTP/HTTPS/SOCKS 开关，不接受 shell、路径或完整任意配置字典。
2. 执行器目录要求当前进程所有、私有且叶节点非链接；单实例文件锁和进程内请求锁防止同目录并发执行。用户/session/操作 ID 组成收据键，内容冲突拒绝，同一请求返回已有结果而不重写。持久化记录损坏、权限异常或链接均拒绝。
3. 系统适配非阻塞取得 SCPreferences 锁，核对当前网络位置中的准确服务 ID、名称、接口、启用状态和协议。比较当前字段与期望值；代理还核对原 host/port。任何不符都在写入前停止。
4. 完整原协议配置、拟改配置、实际调用作用域和 prepared 意图先以 0600 原子文件及 fsync 保存。没有可持久化的恢复材料，不调用系统提交。FixEngine 自身也在调用前保存 apply/restore 操作 ID，以便追踪丢失响应。
5. 仅修改当前字段，保留无关配置；IPv6 Off 使用协议启用状态，不把字符串 Off 当作不存在的 ConfigMethod。禁用时保留原协议参数。先提交、再请求应用，随后通过新的 SCPreferences 读取器回读持久状态，不把写入会话的缓存当成系统回读。
6. 提交/应用成功且完整回读匹配才记 configured；应用失败、回读不符或中断均保留未确认状态。不重试写入，不把配置成功称作网络恢复；收据明确 `network_verified=false`。原核心仍负责保护目标复测、退化检查和最终 verified/rolled_back 判定。

普通写入最多保留 1000 个记录，另留恢复容量至 2000 条，满后拒绝新操作；尚无产品化清理策略。prepared/needs_verification 记录阻止新普通写入。恢复请求可以继续，但必须引用同用户/会话的原操作，目标、字段、端点、方案哈希及正反值都匹配。恢复只在现场仍等于该操作目标值时执行；保留第三方值及无关新配置，并保留原字段“缺失”和“空值”的区别。

成功恢复可完成对应中断意图的收尾，原重复请求的历史结果不改写为一次新的写入。第三方漂移、已经由外部恢复、原会话消失或记录损坏仍可能需要人工核对；当前尚未提供这些 root 收据的用户可操作核对入口。

## 仍未完成

- 独立辅助服务二进制、可信调用身份/凭据、系统注册与批准、撤销/注销和默认 CoreRuntime 接线；当前模块不可作为已经安全部署的提权服务。
- 固定 root 所有目录及全部祖先验证、全设备整批任务的跨用户协调。当前锁保护一个执行器目录与单项配置事务，不保证从整批首次写入到网络验证期间排除其他用户任务。
- 辅助进程退出/重启、会话失效、已经恢复但未结清意图的只读核对、私有收据保留与清理。不会为绕过未知状态而盲重放或覆盖第三方配置。
- 真实系统权限拒绝、SCPreferences 写入/应用/回读、网络退化与恢复、MDM 管理策略、Developer ID 安装/升级。编译与内存模拟不能证明这些 API 在目标系统和配置组合上已验收。
- 通用动态命令的高权限执行仍是另一条待完成通道。本模块的固定字段不是对整个 Agent 能力目标的永久收缩。

## 验证与依据

新增 22 项原生内存配置/真实临时文件检查，以及 6 项 Python 调用合同检查。Agent → 授权哈希 → FixEngine → 原生事务 → FakeMac 观测 → 网络复测/恢复已经贯通；过程中没有调用本机 SystemConfiguration 写入。普通配置提交和网络恢复分别断言，操作去重、错误记录、未知回读、外部漂移与恢复来源均有正负例。实际结果见 [QA-002](../../quality/reports/QA-002-diagnostic-panel.md)。

系统适配依据本机公开 SDK 的 `SCNetworkConfiguration.h`、`SCPreferences.h`、`SCSchemaDefinitions.h`，以及 Apple 的 [SCPreferences 概览](https://developer.apple.com/documentation/systemconfiguration/scpreferences-ft8)。配置锁、持久化提交和请求应用是不同步骤，不能省略业务层网络验证；相关 API 为 [SCPreferencesLock](https://developer.apple.com/documentation/systemconfiguration/scpreferenceslock(_:_:))、[SCPreferencesCommitChanges](https://developer.apple.com/documentation/systemconfiguration/scpreferencescommitchanges(_:)) 和 [SCNetworkProtocolSetEnabled](https://developer.apple.com/documentation/systemconfiguration/scnetworkprotocolsetenabled(_:_:))。
