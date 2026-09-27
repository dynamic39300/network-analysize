# SPEC-009：签名桌面与普通核心通信

2026-09-27。实现位于 `mac_identity.py`、`mac_xpc.py`、`apps/macos/native/RelayIPC.m`，复用 SPEC-005 的业务协议、操作去重和 SPEC-007 的生命周期。这是独立特权执行器之前的调用身份基础，不是已完成的管理员权限通道。

## 传输选择

源码和有效 ad-hoc 开发包继续使用同用户 Unix socket，安全边界不变。冻结 Developer ID 版本从当前运行代码的 Security.framework 签名信息选择 XPC：签名有效、Developer ID 证书链、固定应用标识 `com.wangxinlei.relay`、有效 Team ID、hardened runtime、非调试状态，并拒绝关闭库验证等不安全 entitlement。无效或不支持的非 ad-hoc 签名明确失败，不回退为开发传输。

签名版要求 macOS 13+、默认数据目录及已明确启用/批准的用户 LaunchAgent。包内 plist 声明固定 Mach service `com.wangxinlei.relay.core`；不是全局 root daemon。未注册或待批准时，桌面显示等待状态并禁用启动核心，用户仍可选择登录服务或打开系统设置。不会自动注册服务，也不会启动临时 socket 核心兜底。源码、开发包和自定义目录原有行为不变。

双方从各自已验证的当前可执行文件提取 Team ID 和 cdhash，构造 Apple Developer ID 链、固定应用标识、同团队及精确代码哈希的 requirement。UI 和核心是同一冻结可执行文件的不同角色，不接受任意同团队应用或旧构建。身份条件不来自环境变量、命令参数、连接描述文件或用户可编辑配置。

## 消息与身份

- 接受连接和接收消息时核对有效 UID 与内核 audit session。双方在 `resume` 之前设置 `setCodeSigningRequirement`。描述文件只提供协议版本、固定服务、实例及用户/会话路由信息，不是信任根。
- 客户端先发送仅含随机 hello 的消息；验证受签名约束的回复、实例、UID 和会话后，才发送业务参数。签名条件检查收到的消息，不保证第一次发出的内容不会到达错误服务，因此不能在首条消息中附密钥、方案或用户数据。
- 每连接仅允许一次握手和一次业务请求。随机 nonce、核心实例、客户端 ID、请求 ID 与协议版本绑定；严格 JSON 对象、精确字段和 UUID 格式。每帧最多 1 MiB、每连接最多 5 秒、同时最多 8 个连接。应用层限制不能等同于操作系统解码前的内存上限或完整拒绝服务防护。
- 原生接口只交换 NSData，不解码自定义对象、pickle 或任意 Python 类。Objective-C 协议由 clang 编译，PyObjC 以匹配的 NSData block 元数据桥接；不使用动态创建、缺少扩展类型签名的 Python 协议替代。
- 断线/超时不重放请求。已经被核心接受的动作可能继续验证或回退，客户端用原操作 ID 查询；沿用 journal 去重，而不是声称超时意味着零写入。非法信封不进入业务处理器，不反射私有异常内容。
- 关闭先停止接收并使连接失效，等待已进入的业务处理退出，再释放通信所有权；核心既有撤销、验证/回退及记录锁释放合同不变。

原生桥通过 `scripts/build-native-ipc.py` 构建为 `RelayIPC.dylib`，冻结到 Frameworks。冻结自检验证桥可加载与身份策略可计算；开发包显示 `optional.signedIPC=false`，不能把桥存在当成 Developer ID 验收通过。

## 明确边界

XPC 只保护桌面到普通用户核心的应用身份，不代替方案授权、OS 管理员授权、模型上传同意或网络策略。未增加 root helper、自制密码框、真实服务注册或任意动态命令提权。两端是相同可执行文件，协议不声称区分该文件的其他受支持 CLI 角色。

有效开发包仍是较弱的同用户模式，不能防御具有同用户文件访问权限的恶意代码。签名模式的用户数据也仍在同用户可写目录，不因此变成抗篡改审计库；授权和实例 nonce 不从该目录恢复。没有宣称抵御已控制进程、被替换运行时或全部本机拒绝服务。

精确 cdhash 有意拒绝旧构建。升级必须先由匹配的旧客户端安全收尾旧核心，再切换签名制品；尚无可信自动更新器，不能绕过该限制接受旧核心或降级 socket。正式签名构建的 macOS 12/自定义目录路径不受支持，不套用开发包临时目录冒烟结果。

## 验证与待验收

新增 8 项身份/传输选择检查、7 项真实原生 XPC 检查、1 项 Cocoa 生命周期检查。原生测试使用当前 Python 的实际 ad-hoc cdhash 和同进程匿名 NSXPC endpoint，验证系统真实签名条件拒绝、UID/会话与实例、帧限制、超时不重放、关闭等待，以及经 XPC 的 FakeMac 方案审阅/确认/操作去重/收据。测试注入没有命令行或环境绕过入口。

Developer ID 正向策略以合成签名信息测试，未持有或使用真实发行证书。匿名同进程往返不证明已注册命名服务、不同进程、真正 Developer ID/hardened runtime 发行包或系统登录/升级行为已通过。后续必须使用真实签名制品验证这些项目，并测试被拒绝的异签名客户端、更新前后版本与卸载。详细测试、构建哈希和截图见 [QA-002](../../quality/reports/QA-002-diagnostic-panel.md)。

## 官方依据

- [Apple：XPC peer code-signing checks](https://developer.apple.com/forums/thread/681053)，签名 requirement 应在连接恢复前设置，macOS 13+ 提供该 API。
- [Apple DTS：首条出站消息与对端认证](https://developer.apple.com/forums/thread/837286)，消息接收方检查发送者身份；先完成无敏感内容的握手。
- [PyObjC：NSXPCInterface](https://pyobjc.readthedocs.io/en/latest/notes/using-nsxpcinterface.html)，原生编译协议的扩展类型签名要求；[block 元数据](https://pyobjc.readthedocs.io/en/latest/core/blocks.html)。
