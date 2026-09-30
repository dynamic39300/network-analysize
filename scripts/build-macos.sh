#!/bin/bash
# Builds an isolated arm64 bundle; never installs, launches the GUI or changes login items.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG=""
RELEASE=0
SELF_CHECK=0
while (($#)); do
  case "$1" in
    --commercial-config) [[ $# -ge 2 ]] || exit 2; CONFIG="$2"; shift 2 ;;
    --release) RELEASE=1; shift ;;
    --self-check) SELF_CHECK=1; shift ;;
    --help) echo 'Usage: scripts/build-macos.sh [--commercial-config /absolute/public.json] [--release] [--self-check]'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || { echo 'Build on an Apple Silicon Mac.' >&2; exit 1; }
command -v uv >/dev/null || { echo 'uv is required.' >&2; exit 1; }
[[ -z "$CONFIG" || ( "$CONFIG" == /* && -f "$CONFIG" ) ]] || { echo 'Configuration must be an existing absolute file.' >&2; exit 1; }
[[ "$RELEASE" == 0 || -n "$CONFIG" ]] || { echo '--release requires --commercial-config.' >&2; exit 1; }
uv sync --directory "$ROOT/apps/macos" --locked
PYTHON="$(uv run --directory "$ROOT/apps/macos" --no-sync python -c 'import sys; print(sys.executable)')"
"$PYTHON" -c 'import platform,sys; assert sys.version_info[:2] == (3,12) and platform.machine() == "arm64"'
"$PYTHON" "$ROOT/scripts/build-native-ipc.py"
STAGING="$ROOT/build/macos-inputs"
ICONSET="$STAGING/Relay.iconset"
mkdir -p "$ICONSET" "$ROOT/dist/macos"
for size in 16 32 128 256 512; do
  /usr/bin/sips -s format png -z "$size" "$size" "$ROOT/assets/logo/Relay-logo-final.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  doubled=$((size * 2))
  /usr/bin/sips -s format png -z "$doubled" "$doubled" "$ROOT/assets/logo/Relay-logo-final.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
/usr/bin/iconutil --convert icns "$ICONSET" --output "$STAGING/Relay.icns"
export RELAY_BUILD_ICON="$STAGING/Relay.icns"
export RELAY_BUILD_STAGING="$STAGING"
export RELAY_BUILD_COMMERCIAL_CONFIG="$CONFIG"
export RELAY_RELEASE_BUILD="$RELEASE"
BUILD_OUTPUT="$(mktemp -d "${TMPDIR:-/tmp}/relay-build.XXXXXX")"
trap 'rm -rf "$BUILD_OUTPUT"' EXIT
"$PYTHON" -m PyInstaller --clean --noconfirm --distpath "$BUILD_OUTPUT" \
  --workpath "$ROOT/build/pyinstaller" "$ROOT/apps/macos/Relay.spec"
APP="$BUILD_OUTPUT/NetCare.app"
mkdir -p "$APP/Contents/Library/LaunchAgents" "$APP/Contents/Library/LaunchDaemons" "$APP/Contents/Library/LaunchServices"
/bin/cp "$ROOT/apps/macos/com.wangxinlei.relay.agent.plist" "$APP/Contents/Library/LaunchAgents/"
/bin/cp "$ROOT/apps/macos/com.wangxinlei.relay.helper.plist" "$APP/Contents/Library/LaunchDaemons/"
/bin/cp "$ROOT/build/macos-native/RelayHelper" "$APP/Contents/Library/LaunchServices/RelayHelper"
clean_signing_metadata() {
  # Remove only Apple's two prohibited signing attributes, never quarantine or
  # other protection metadata. -s handles symlinks without following them.
  while IFS= read -r -d '' item; do
    for attribute in com.apple.FinderInfo com.apple.ResourceFork; do
      if /usr/bin/xattr -ps "$attribute" "$item" >/dev/null 2>&1; then
        /usr/bin/xattr -ds "$attribute" "$item"
      fi
    done
  done < <(/usr/bin/find "$APP" -depth -print0)
}
clean_signing_metadata
/usr/bin/codesign --force --deep --sign - "$APP"
/usr/bin/codesign --verify --deep --strict "$APP"
/usr/bin/lipo "$APP/Contents/MacOS/NetCare" -verify_arch arm64
"$PYTHON" - "$ROOT" "$APP" "$RELEASE" <<'PY'
import hashlib, json, pathlib, platform, sys
from importlib.metadata import version
root, app = map(pathlib.Path, sys.argv[1:3])
paths = [root / 'apps/macos/uv.lock', root / 'apps/macos/Relay.spec',
         root / 'scripts/build-native-ipc.py', root / 'scripts/sign-macos.sh',
         root / 'scripts/build-macos.sh',
         root / 'code/network-doctor-menu.py', root / 'code/relay_config.py',
         root / 'code/relay_app.py', root / 'code/relay_core.py',
         root / 'apps/macos/com.wangxinlei.relay.agent.plist', root / 'code/app_icon.png',
         root / 'apps/macos/com.wangxinlei.relay.helper.plist',
         root / 'assets/logo/Relay-logo-final.png'] + sorted((root / 'code/relay').rglob('*.py')) + sorted((root / 'apps/macos/native').glob('*'))
metadata = {'version':'3.0.0', 'bundleIdentifier':'com.wangxinlei.relay',
            'architecture':platform.machine(), 'python':platform.python_version(),
            'pyinstaller':version('pyinstaller'), 'signing':'ad-hoc, not Developer ID; not notarized',
            'releaseConfigValidated':sys.argv[3] == '1',
            'inputs':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}
config = app / 'Contents/Resources/relay-commercial.json'
metadata['commercialConfigSHA256'] = hashlib.sha256(config.read_bytes()).hexdigest() if config.exists() else None
(root / 'dist/macos/build-metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
PY
if [[ "$SELF_CHECK" == 1 ]]; then
  "$APP/Contents/MacOS/NetCare" --self-check
  "$APP/Contents/Library/LaunchServices/RelayHelper" --self-check
fi
# Finder/tools may restore FinderInfo after the earlier verification. Always
# perform this final narrow cleanup after the optional execution, then verify.
clean_signing_metadata
/usr/bin/codesign --verify --deep --strict "$APP"
ARCHIVE="$ROOT/dist/macos/NetCare-3.0.0-arm64-local.zip"
/usr/bin/ditto -c -k --norsrc --extattr --qtn --keepParent "$APP" "$BUILD_OUTPUT/NetCare.zip"
mkdir -p "$BUILD_OUTPUT/verify"
/usr/bin/ditto -x -k "$BUILD_OUTPUT/NetCare.zip" "$BUILD_OUTPUT/verify"
/usr/bin/codesign --verify --deep --strict "$BUILD_OUTPUT/verify/NetCare.app"
# The ZIP is the verified immutable handoff. Finder may later alter metadata on
# the convenient expanded Documents copy, so archive verification precedes it.
/bin/cp "$BUILD_OUTPUT/NetCare.zip" "$ARCHIVE"
DESTINATION="$ROOT/dist/macos/NetCare.app"
[[ "$DESTINATION" == "$ROOT/dist/macos/NetCare.app" ]] || exit 1
/bin/rm -rf "$DESTINATION"
/usr/bin/ditto --norsrc --extattr --qtn "$APP" "$DESTINATION"
"$PYTHON" - "$ROOT/dist/macos/build-metadata.json" "$ARCHIVE" <<'PY'
import hashlib,json,pathlib,sys
metadata,archive=map(pathlib.Path,sys.argv[1:])
data=json.loads(metadata.read_text())
data['archive']={'filename':archive.name,'bytes':archive.stat().st_size,
                 'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
                 'extractedStrictSignatureVerified':True}
metadata.write_text(json.dumps(data,indent=2)+'\n')
PY
echo "Built $DESTINATION (arm64, local ad-hoc signature; not a notarized release)."
/usr/bin/shasum -a 256 "$ARCHIVE"
