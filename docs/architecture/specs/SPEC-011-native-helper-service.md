# SPEC-011：Mac 独立配置辅助服务

2026-09-27。承接 SPEC-009 的签名身份与 SPEC-010 的配置事务，新增原生辅助程序、版本化调用、整批协调及显式系统服务管理。**代码和开发联测已实现，不代表 Developer ID、root 命名服务、实际安装或真实网络写入已验收。** 开发机没有注册该服务或修改网络。

## 进程与身份

- 普通 UI/Core 继续使用同一个冻结应用程序；需要高权限的配置事务由独立 Objective-C `RelayHelper` 执行。辅助进程不加载 Python，不接受 shell、可执行文件路径、任意配置字典或调用者指定的数据目录。
- 包内固定路径为 `Contents/Library/LaunchServices/RelayHelper`，固定标识/系统 Mach service 为 `com.wangxinlei.relay.helper`。`Contents/Library/LaunchDaemons/com.wangxinlei.relay.helper.plist` 使用 BundleProgram，按需启动；没有安装脚本中的 sudo、管理员密码框、RunAtLoad 或 KeepAlive。
- 默认入口要求 macOS 13+、euid 0 和有效签名配对。`--identity`、`--self-check`、`--verify-pair` 仅检查签名，不打开配置、创建 root 目录或注册服务。
- 两端均从正在运行的代码签名确定身份。辅助程序验证其固定位置所属的外层 App 及自身；主 App 的受签名 Info 字典固定 `RelayHelperCDHash`。双方 requirement 包含 Developer ID 链、团队、固定标识和准确 cdhash，拒绝 ad-hoc、调试状态和危险 entitlements。
- XPC requirement 在 resume 前设置。客户端用 `NSXPCConnectionPrivileged` 访问系统域，先发送仅含随机数的 hello，再核对内核报告的 root UID、签名要求、实例和 nonce，之后才发送配置内容。服务从实际连接取得 UID/audit session，不接受请求自报身份；拒绝系统账号和无效会话。
- 外层静态验签仍有 Apple 文档明确的并发修改边界，不能称为完整反回滚方案。root 控制记录固定首次接受的 App/helper/team 组合；换构建直接拒绝，需要后续受信任升级流程，不能靠删除恢复目录绕过。

## 调用与整批协调

`RelayRPC` 复用 NSData reply 协议声明。辅助服务语义独立于普通核心，严格 JSON envelope 为 version/instance/nonce/id/client/method/params；每连接仅一次 hello 与一次业务调用，最多 8 个连接、32 KiB 输入、5 秒连接期限。错误不包含配置或私有异常详情。

1. Agent 仍按原方案取得授权。FixEngine 在本地恢复文件先保存 batch ID、方案哈希和每字段 apply/restore ID。
2. `begin` 提交最多 5 对结构化请求。每对恢复须精确反向引用 apply，所有请求同一方案、操作 ID 不重复。root 控制文件记录客户端实例、内核用户/会话、固定清单及辅助进程实例。
3. 持有批次期间，其他用户或其他客户端不能开始新批次、执行本批次或 drain 服务。每次 perform 必须与清单中的完整请求一致，随后才进入 SPEC-010 事务。
4. 正向写入受单调时钟与墙上时钟双重 300 秒期限约束；必要恢复不受正向期限限制。客户端通信失败不重放修改；每项原生收据继续按用户/会话/操作 ID 持久去重。
5. 原核心在批次仍占用时验证网络和保护目标。`end` 只读核对每项现场：verified 要求原收据已 configured 且当前仍等于目标；rolled_back/blocked 要求已写项回到原值，未开始项无需写入。只读核对也可收尾“曾不确定、但现场已经恢复”的同实例意图。
6. 所有项核对后才清空批次。最近的同用户/会话/客户端、同 batch ID/outcome 结束查询幂等，不再写配置。若回复丢失、结清失败或现场漂移，核心保留待核对结果和 `helper_settlement=unconfirmed`，不能展示整体 verified 或开启新的修改。

root 私有目录固定为 `/Library/Application Support/com.wangxinlei.relay/privileged`，从根目录逐级 openat/NOFOLLOW 检查祖先的 root 所有权和不可组/全局写入；产品目录 0700，记录 0600。同时通过 fd 读取扩展 ACL，拒绝额外 allow 条目及无法核验的权限，不能仅凭 Unix mode 宣称私有；只含 deny 的 ACL 可保留。目录在读写记录时再次核验，锁文件、已有记录及写入临时文件也检查 ACL。执行器实例锁保证所有用户走该 helper 时共享一个批次协调点。它不控制其他系统应用写入；SCPreferences 锁只覆盖实际配置事务，仍须做现场漂移核对。

关闭服务先停止接收请求、断开连接并等待已接受调用退出。进程被系统强制终止时，已有 prepared/batch 文件仍是恢复依据。重新启动保留未结清批次，拒绝原实例写入/重放。后续 [SPEC-012](SPEC-012-helper-recovery.md) 已接入同账号显式恢复审阅、终态历史和桌面核对；不会恢复旧授权。动态命令仍使用此前普通用户作业协调，不在这个设备级批次租约内。

## 用户选择与生命周期

`MacPrivilegedService` 只在受信任签名包、macOS 13+、默认数据目录提供 SMAppService daemon 适配。查询状态只读系统注册信息，不调用 register 或启动 helper；注册状态不冒充运行健康。

设置中的“允许系统配置修复”和“登录时启动网络保障核心”是独立选择。前者有独立确认，先正常停止普通核心，再申请系统注册；系统已批准时经签名连接 resume，批准待定则保留等待状态。用户批准后可用旁边的核对按钮明确激活。变更后核心保持停止，不自动恢复模型上传、密钥、任务授权或守护。

关闭修复/准备卸载必须先正常停止核心，再取得 helper 的持久 drain 确认，最后注销服务并复核系统状态。任何批次未结束、服务不可达或状态未知都不能强制注销。drain 持久绑定发起用户，另一用户不能替换或解除；同用户生命周期操作还受默认数据目录的操作锁串行化。成功卸载准备先注销 helper，再注销用户 LaunchAgent；保留应用、任务和恢复资料。

批准被撤销或待批准时，无法确认 helper 是否曾经运行，不能把连接失败当“没有任务”；当前保守要求先恢复系统批准，再核对/收尾。发起 drain 的账号被删除等跨用户管理恢复仍待完成，不宣称所有服务管理场景已覆盖。

CLI 的 `--lifecycle enable-helper` / `disable-helper` 与桌面使用同一管理对象，不是跳过系统批准的路径。Developer ID CoreRuntime 默认注入 `MacMutationWriter`，helper 不可用时修复失败，不回退普通 setter 或临时 sudo。源码/ad-hoc 预览保留原有普通用户行为，不能验证提权。

## 构建与验证

`build-native-ipc.py` 编译桥和独立辅助程序；本地构建复制 helper 和 daemon plist 后进行 ad-hoc 验签。冻结自检分别报告 signedIPC 和 signedHelperPair，并检查文件存在。

显式发布脚本先签署辅助程序，再以其实际签名 cdhash 更新主 Info，最后签署外层 App；严格验签后执行只读 `--verify-pair`。签名/公证脚本只在用户明确进行发布操作时运行，本阶段没有调用发行证书或公证服务。

测试运行真实 Objective-C、匿名 NSXPC、内核签名条件、临时文件和内存网络配置，并贯通 Agent → 原生辅助调用 → 普通核心验证。生命周期使用 fake SMAppService，GUI 使用实际 Cocoa 控件；这些不是 root 跨进程或 Developer ID 正向安装测试。数量、构建哈希与截图证据见 [QA-002](../../quality/reports/QA-002-diagnostic-panel.md)。

同一构建的中断恢复开发证据已补充至 SPEC-012，真实 root 环境仍待验收。后续必须完成：保留清理、动态执行与设备级协调、受信任升级/反回滚策略、完整首次启用/无障碍，以及真实权限拒绝、MDM、配置应用、网络恢复、签名安装与第二台 Mac 验收。Windows 后续可选，不阻塞当前 Mac 目标；上述 Mac 要求不因此缩减。

## 官方依据

- Apple [SMAppService](https://developer.apple.com/documentation/servicemanagement/smappservice)、[register](https://developer.apple.com/documentation/servicemanagement/smappservice/register())：LaunchDaemon 需要系统管理员批准，注册请求成功不等于批准完成。
- Apple [迁移辅助可执行程序](https://developer.apple.com/documentation/servicemanagement/updating-helper-executables-from-earlier-versions-of-macos)：包内辅助程序、LaunchDaemons 与 BundleProgram 布局。
- Apple [SecStaticCodeCheckValidity](https://developer.apple.com/documentation/security/secstaticcodecheckvalidity(_:_:_:))：资源完整性检查及并发修改的边界。另参考本机公开 Security/NSXPCConnection SDK 的有效用户、audit session 与系统 Mach service 标志；并不按可复用 PID/进程名授权。
- XPC 首次消息与 requirement 设置规则沿用 [SPEC-009](SPEC-009-signed-core-transport.md) 的 Apple 官方依据。
- ACL 使用本机公开 `sys/acl.h` 与 Apple 的 [acl_get_fd_np 手册](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man3/acl_get_link_np.3.html)、[libc fd 读取实现](https://github.com/apple-oss-distributions/Libc/blob/main/posix1e/acl_file.c) 和 [条目读取实现](https://github.com/apple-oss-distributions/Libc/blob/main/posix1e/acl_entry.c)。正常无 ACL 与读取失败分开处理，原生临时文件正负例验证实际系统语义。
