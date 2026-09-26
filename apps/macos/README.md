# Relay macOS 构建

版本 **3.0.0**，Bundle ID **com.wangxinlei.relay**，Apple Silicon `arm64`。依赖由本目录 `pyproject.toml` 与 `uv.lock` 固定，构建使用 Python 3.12 和 PyInstaller。当前最低系统字段为 macOS 12.0；这是构建目标，尚不代表每个系统版本或第二台 Mac 已验收。

从仓库根目录执行：

```sh
scripts/build-macos.sh --self-check
```

输出 `dist/macos/Relay.app`、`dist/macos/Relay-3.0.0-arm64-local.zip` 与 `dist/macos/build-metadata.json`。脚本同步锁定环境，使用确认的 `assets/logo/Relay-logo-final.png` 生成多尺寸 ICNS；随包包含 Python、PyObjC、本机 Keychain 后端、cryptography、检测插件和菜单图标。插件源文件也被收集，供现有文件名发现机制使用。

构建不安装到 `/Applications`、不启动 GUI、不改变登录项或当前网络。`--self-check` 仅运行菜单提供的无 GUI 检查，不读取用户配置、访问钥匙串或执行诊断命令。默认不包含商业配置，Free 本地能力仍可用。

本机产物是 **ad-hoc 自签名、未经公证的开发包**，不能当作正式 Developer ID 发布。锁文件、输入哈希和固定构建路径支持重建与追溯；macOS 签名、系统工具和原生依赖使它不承诺逐字节相同。

构建、签名、自检和归档均在私有临时 staging 完成，避开 Documents 中观察到的元数据写回竞争。自检后仅清理 `com.apple.FinderInfo` / `com.apple.ResourceFork`，再次验签，再以 `ditto --norsrc --extattr --qtn` 生成 ZIP；保留 quarantine 与其它保护属性。脚本将 ZIP 解压到另一个 staging 子目录并严格验签，通过后才复制 ZIP 和便于查看的 App 到 dist，ZIP 哈希写入 metadata。将 ZIP 作为固定交付制品。Finder 或其它工具可能给展开的 App 重新附加签名不允许的元数据；本次只观察到自检后它出现，没有证明是哪个进程写入。[Apple QA1940](https://developer.apple.com/library/archive/qa/qa1940/_index.html) 说明了该限制以及 Finder 浏览包内容可能添加 FinderInfo 的情况。

## 商业公钥配置

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
scripts/sign-macos.sh --app /absolute/Relay.app \
  --identity 'Developer ID Application: YOUR NAME (TEAMID)' \
  --notarize --keychain-profile EXISTING_PROFILE \
  --archive /absolute/unused/Relay-3.0.0-arm64.zip
```

它先复核 Bundle ID、版本和生产公钥配置，再从内向外签署 Mach-O 与 framework，最后签 App；显式要求公证时才上传到 Apple、等待 Accepted、staple 并评估 Gatekeeper。压缩包路径必须尚不存在，避免覆盖已发版制品。

正式发布前需第二台 Mac 验证首次安装、菜单启动、Keychain 与撤销会话、Free 离线、Pro 授权与到期、更新保留本地记录，以及签名、公证和制品 SHA-256。只有完成后才将该下载标记为可用。
