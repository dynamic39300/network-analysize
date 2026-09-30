# NetCare macOS 构建

产品显示名、App 与主程序已统一为 NetCare；`Relay.spec`、内部原生模块和 `com.wangxinlei.relay` 身份仍保留。用户数据目录与钥匙串不迁移、不重新初始化。完整更名边界见 [命名说明](../../docs/product/BRAND-001-netcare.md)。

版本 **3.0.0**，Bundle ID **com.wangxinlei.relay**，Apple Silicon `arm64`。依赖由本目录 `pyproject.toml` 与 `uv.lock` 固定，构建使用 Python 3.12 和 PyInstaller。当前最低系统字段为 macOS 12.0；这是构建目标，尚不代表每个系统版本或第二台 Mac 已验收。

从仓库根目录执行：

```sh
scripts/build-macos.sh --self-check
```

输出 `dist/macos/NetCare.app`、`dist/macos/NetCare-3.0.0-arm64-local.zip` 与 `dist/macos/build-metadata.json`。脚本同步锁定环境，使用确认的 `assets/logo/Relay-logo-final.png` 生成多尺寸 ICNS；随包包含 Python、PyObjC、本机 Keychain 后端、cryptography、检测插件和菜单图标。插件源文件也被收集，供现有文件名发现机制使用。

构建不安装到 `/Applications`、不启动 GUI、不改变登录项或当前网络。`--self-check` 仅运行菜单提供的无 GUI 检查，不读取用户配置、访问钥匙串或执行诊断命令。默认不包含商业配置，Free 本地能力仍可用。

本机产物是 **ad-hoc 自签名、未经公证的开发包**，不能当作正式 Developer ID 发布。锁文件、输入哈希和固定构建路径支持重建与追溯；macOS 签名、系统工具和原生依赖使它不承诺逐字节相同。

构建、签名、自检和归档均在私有临时 staging 完成，避开 Documents 中观察到的元数据写回竞争。自检后仅清理 `com.apple.FinderInfo` / `com.apple.ResourceFork`，再次验签，再以 `ditto --norsrc --extattr --qtn` 生成 ZIP；保留 quarantine 与其它保护属性。脚本将 ZIP 解压到另一个 staging 子目录并严格验签，通过后才复制 ZIP 和便于查看的 App 到 dist，ZIP 哈希写入 metadata。将 ZIP 作为固定交付制品。Finder 或其它工具可能给展开的 App 重新附加签名不允许的元数据；本次只观察到自检后它出现，没有证明是哪个进程写入。[Apple QA1940](https://developer.apple.com/library/archive/qa/qa1940/_index.html) 说明了该限制以及 Finder 浏览包内容可能添加 FinderInfo 的情况。

## Agent 入口与生命周期

开发 App 默认双击进入 Agent 工作台，仍支持显式 `--agent-desktop`。工作台会重连已有核心或在数据目录空闲且未明确停止时启动独立核心；关闭桌面不退出核心。账号、商业历史/比较/导出与检测预设已接入新界面，不改变原权益规则或赋予网络修改权限。`--legacy-desktop` 保留旧菜单回退入口，新旧入口不能同时拥有同一数据目录；同目录重复桌面启动也被拒绝。源码和自定义目录可使用临时核心，但不提供登录服务注册。

包内包含 ServiceManagement、工作台图标及 `Contents/Library/LaunchAgents/com.wangxinlei.relay.agent.plist`。macOS 13+ 默认目录的 App 入口可请求用户登录服务、查询批准状态；停止和注销前等执行验证/回退结束，不强杀未知进程。当前只验证模拟服务状态及本地冻结核心，未在开发机实际注册登录项，不能声称系统服务或签名安装已验收。

构建脚本另外以 clang 编译原生 `RelayIPC.dylib`，冻结自检验证可加载。Developer ID 版从自身真实签名导出身份条件，经用户 LaunchAgent 的固定 Mach service 使用双向签名 XPC；要求 macOS 13+、默认目录和用户明确启用/批准登录服务，否则等待，不启动弱身份临时核心。源码/ad-hoc 仍为同用户开发 socket。签名模式拒绝其他构建，升级前需匹配旧客户端安全收尾；尚未提供自动签名升级。运行身份不能由配置文件或环境覆盖，具体证据和未验收项见 [SPEC-009](../../docs/architecture/specs/SPEC-009-signed-core-transport.md)。

包内另有 `Contents/Library/LaunchServices/RelayHelper` 原生程序及 `Library/LaunchDaemons/com.wangxinlei.relay.helper.plist`。系统修复与普通登录服务分开批准，只有配对 Developer ID 构建可启用；本地 ad-hoc 包仅用于检查文件、编译和普通核心行为。发布脚本将 helper 的真实 cdhash 固定在外层签名 Info 中，并在外层签名后核对配对，不直接注册服务。未完成任务会阻止注销；root 记录绑定当前构建，尚未提供跨构建迁移，不能直接替换包或删除记录强行升级。详见 [SPEC-011](../../docs/architecture/specs/SPEC-011-native-helper-service.md)。

同一构建中断后的配置恢复已接到处理记录：私有收据显示准确服务和原值/现值，勾选后选择恢复或保留并复验。外部漂移/未知状态禁用恢复；断线不重发。终态记录与本地快照不一致时仍保留待核对，不因单次网络检测正常而清空系统批次。见 [SPEC-012](../../docs/architecture/specs/SPEC-012-helper-recovery.md)；不是实网或正式 root 安装的验收证明。

ad-hoc 构建后可运行 `apps/macos/.venv/bin/python scripts/verify_lifecycle_bundle.py`：仅临时空闲配置、独立进程启动/重连/停机，不探测网络、不注册服务、不接触默认用户资料；不适用于要求默认目录和已注册服务的 Developer ID 模式。所有权与授权区别、停机回执、卸载保留资料及剩余边界见 [SPEC-007](../../docs/architecture/specs/SPEC-007-core-lifecycle.md)，默认入口与既有数据兼容见 [SPEC-008](../../docs/architecture/specs/SPEC-008-default-agent-desktop.md)。新构建不替换正在运行的旧安装副本；真实安装升级、Keychain/官网往返与系统登录服务仍需另行验收。

## 商业公钥配置

本地 ZIP 构建完成后，运行 `scripts/package-macos-dmg.sh` 生成可拖入 Applications 的 `dist/macos/NetCare-3.0.0-arm64-local.dmg`，附安装说明和 SHA-256。它仍为未公证的 Apple Silicon 开发预览版。ad-hoc 默认目录只启动临时核心，不提供后台服务注册；Developer ID 版本仍要求系统批准。构建支持通过 `UV_PROJECT_ENVIRONMENT` 指定项目目录外的虚拟环境。

内置的公开 Google 保护目标在无 VPN、无代理且手工 DNS 的系统路径访问超时时，会对固定的 `www.google.com` 向 `1.1.1.1` 做一次参考解析，并对参考地址做只读、保留 HTTPS 主机名的访问测试；不会把自定义目标或内网域名交给公共解析器。如果系统解析地址无法连接、参考地址实测能响应，总览展示证据与“审阅修复方案”。用户确认后才尝试修改当前网络服务 DNS；执行前重新检查、保存原值，执行后复验保护目标，失败时尝试恢复。该方案只支持逐次授权，不能授予自动持续修复信任。它证明的是当前解析结果或到该地址的路径异常，不能仅凭地址不同断言 DNS 服务本身故障；其他无成熟方案的异常仍应显示需要进一步调查。

可选 `--commercial-config /absolute/public-config.json` 将公开配置复制为 `Contents/Resources/relay-commercial.json`，只允许三个键：

```json
{
  "origin": "https://relay.example.invalid",
  "publicKey": "替换为真实的32字节Ed25519公钥的base64url文本",
  "development": false
}
```

这是结构示例，不能直接构建正式版本。不得包含私钥、数据库/SMTP/支付秘密。公钥应来自独立服务端签名密钥；不能使用测试夹具作为生产信任根。开发配置只在显式 `development=true` 时允许 localhost / 127.0.0.1 HTTP。

```sh
scripts/build-macos.sh --release --commercial-config /absolute/relay-production-public.json --self-check
```

`--release` 在构建前强制 HTTPS origin、32 字节公钥、`development=false`，仍然只生成本机 ad-hoc 包。冻结包只读 bundle 内配置，源码开发的环境覆盖不能替换发行信任根。

## 签名与公证操作

以下脚本已提供但本次没有执行。需操作人明确指定已有 Developer ID Application identity；公证还需显式 `--notarize` 与已有 Keychain profile。脚本不创建证书、不保存 Apple ID 密码，也不自动发布到官网。

```sh
scripts/sign-macos.sh --app /absolute/NetCare.app \
  --identity 'Developer ID Application: YOUR NAME (TEAMID)' \
  --notarize --keychain-profile EXISTING_PROFILE \
  --archive /absolute/unused/NetCare-3.0.0-arm64.zip
```

它先复核 Bundle ID、版本和生产公钥配置，再从内向外签署 Mach-O 与 framework，最后签 App；显式要求公证时才上传到 Apple、等待 Accepted、staple 并评估 Gatekeeper。压缩包路径必须尚不存在，避免覆盖已发版制品。

正式发布前需第二台 Mac 验证首次安装、菜单启动、Keychain 与撤销会话、Free 离线、Pro 授权与到期、更新保留本地记录，以及签名、公证和制品 SHA-256。只有完成后才将该下载标记为可用。
