# macOS 包本机验证

2026-09-26，在 Apple Silicon Mac 以 `scripts/build-macos.sh --self-check` 构建。

- 产物：`dist/macos/Relay.app`，普通文件合计 **40,166,467 字节（约 38.3 MiB）**，不重复计算 bundle 内符号链接。
- Bundle ID `com.wangxinlei.relay`，版本 `3.0.0`，`arm64`，Python **3.12.14**，PyInstaller **6.22.3**。
- `codesign --verify --deep --strict` 与 `lipo ... -verify_arch arm64` 成功。签名是 **ad-hoc**，没有 Developer ID、公证、上传、安装或 GUI 启动。
- 包内 `--self-check` 成功：dns、ipv6、proxy、reachability、system_proxy、vpn、wifi 共 7 插件，菜单图标存在，商业模块可导入，errors 为空。该版本自检还实际导入 rumps、macOS Keychain backend、AppKit、AppHelper，并生成 Ed25519 临时密钥进行本地签名验证；不接触用户钥匙串或执行网络诊断命令。
- 15 个原生库的 `otool -L` 检查未发现构建机 venv、Homebrew 或其他外部私有绝对依赖，仅使用包内相对位置与 macOS 系统库。
- `dist/macos/build-metadata.json` 输入哈希与构建后当前菜单及 `code/relay` 全部源文件一致，包含最新会话损坏防护。
- 未提供商业 JSON：当前是 Free 开发包，`releaseConfigValidated=false`。没有伪造生产 origin、公钥或账户服务。
- 负向参数验证通过：`build-macos.sh --release` 缺少公钥配置拒绝构建；`sign-macos.sh --identity -` 拒绝 ad-hoc 作为正式签名。

初次构建发现源 logo 虽名为 PNG 实际为 JPEG，已在 sips 生成 iconset 时显式指定 PNG。原始资产与已安装 App 未更改。

后续复核观察到自检之后 App 根目录存在 `com.apple.FinderInfo`，严格验签因此失败；没有证明写入元数据的具体进程。[Apple QA1940](https://developer.apple.com/library/archive/qa/qa1940/_index.html) 明确禁止 ResourceFork / FinderInfo，并说明 Finder 浏览包内容可能添加它。构建已改成自检后按深度顺序只移除这两类属性（App 根最后处理），再验签和归档。quarantine、provenance 及其它保护属性不被清除。

固定交付归档为 `dist/macos/Relay-3.0.0-arm64-local.zip`，使用 `ditto --norsrc --extattr --qtn`，SHA-256 与大小见同目录构建 metadata。归档解压至独立临时目录的副本也执行严格验签；未启动副本 GUI。

最终脚本验证成功：PyInstaller 输出、ad-hoc 签名、自检、窄范围清理和 ZIP 全在私有临时 staging 执行，独立解压副本严格验签通过后才复制产物到 dist。这样固定归档不依赖 Documents 内展开 App 后续是否被附加 FinderInfo。最终 ZIP SHA-256 为 `968928dd8c6b3c259cfc5e1ee170479e8a1b692b27b42adf693e81dd881f0c40`；证据 metadata 已刷新为本轮成功产物。

部署部分：Compose YAML 可解析，模板关键 fail-closed 条件、Shell 语法通过；Docker 不在本机 PATH，因此没有 Docker build / Compose 运行验收。正式签名、公证、第二台 Mac、TLS 代理、生产 SMTP 与真实商户仍是独立上线条件。
