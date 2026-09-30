#!/bin/bash
# Package the verified local ZIP as a drag-to-Applications disk image.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCHIVE="$ROOT/dist/macos/NetCare-3.0.0-arm64-local.zip"
IMAGE="$ROOT/dist/macos/NetCare-3.0.0-arm64-local.dmg"
[[ "$(uname -s)" == Darwin && -f "$ARCHIVE" ]] || { echo 'Build the macOS ZIP first.' >&2; exit 1; }
STAGING="$(mktemp -d "${TMPDIR:-/tmp}/netcare-dmg.XXXXXX")"
trap 'rm -rf "$STAGING"' EXIT
mkdir "$STAGING/content"
/usr/bin/ditto -x -k "$ARCHIVE" "$STAGING/content"
/usr/bin/codesign --verify --deep --strict "$STAGING/content/NetCare.app"
ln -s /Applications "$STAGING/content/Applications"
cat > "$STAGING/content/安装说明.txt" <<'TEXT'
NetCare 3.0.0 · Apple Silicon Mac 本地预览版

将 NetCare.app 拖到 Applications 文件夹，然后从“应用程序”启动。
当前应用图标为银色信号弧，默认打开 NetCare 网络保障工作台。

此包是本机 ad-hoc 签名的开发预览，尚未完成 Apple Developer ID 签名和公证。
如果系统阻止启动，请保留系统保护设置；本机开发预览可从项目源码运行。
普通诊断可用；需要正式签名和系统批准的特权修复、登录服务尚不属于本包的已验收能力。

现有本地数据继续保存在 ~/Library/Application Support/Relay/。
本安装包不自动注册登录项、安装特权服务或更改网络配置。
TEXT
/usr/bin/hdiutil create -quiet -volname 'NetCare' -srcfolder "$STAGING/content" -format UDZO "$STAGING/NetCare.dmg"
/usr/bin/hdiutil verify "$STAGING/NetCare.dmg"
/bin/cp "$STAGING/NetCare.dmg" "$IMAGE"
/usr/bin/shasum -a 256 "$IMAGE" > "$IMAGE.sha256"
echo "$IMAGE"
