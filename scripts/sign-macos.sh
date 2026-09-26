#!/bin/bash
# Explicit release operation. Writing this script does not sign, notarize or upload anything.
set -euo pipefail
APP=""
IDENTITY=""
PROFILE=""
ARCHIVE=""
NOTARIZE=0
while (($#)); do
  case "$1" in
    --app) [[ $# -ge 2 ]] || exit 2; APP="$2"; shift 2 ;;
    --identity) [[ $# -ge 2 ]] || exit 2; IDENTITY="$2"; shift 2 ;;
    --archive) [[ $# -ge 2 ]] || exit 2; ARCHIVE="$2"; shift 2 ;;
    --keychain-profile) [[ $# -ge 2 ]] || exit 2; PROFILE="$2"; shift 2 ;;
    --notarize) NOTARIZE=1; shift ;;
    --help) echo 'Usage: sign-macos.sh --app /absolute/Relay.app --identity "Developer ID Application: …" [--archive /absolute/new.zip] [--notarize --keychain-profile EXISTING_PROFILE]'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ "$(uname -s)" == Darwin && "$APP" == /* && -d "$APP/Contents" ]] || { echo 'An explicit absolute macOS app path is required.' >&2; exit 1; }
[[ "$IDENTITY" == "Developer ID Application: "* ]] || { echo 'An explicit Developer ID Application identity is required; ad-hoc is not a release.' >&2; exit 1; }
[[ "$NOTARIZE" == 0 || -n "$PROFILE" ]] || { echo '--notarize requires an existing --keychain-profile.' >&2; exit 1; }
[[ "$NOTARIZE" == 1 || -z "$PROFILE" ]] || { echo 'Use --notarize explicitly when passing a profile.' >&2; exit 1; }
[[ -z "$ARCHIVE" || ( "$ARCHIVE" == /* && ! -e "$ARCHIVE" ) ]] || { echo 'Archive must be an absolute, unused path.' >&2; exit 1; }
python3 - "$APP" <<'PY'
import base64, json, pathlib, plistlib, sys
from urllib.parse import urlsplit
app = pathlib.Path(sys.argv[1])
info = plistlib.loads((app/'Contents/Info.plist').read_bytes())
if info.get('CFBundleIdentifier') != 'com.wangxinlei.relay' or info.get('CFBundleShortVersionString') != '3.0.0':
    raise SystemExit('Unexpected Relay bundle identity/version')
config = json.loads((app/'Contents/Resources/relay-commercial.json').read_text())
if set(config) != {'origin','publicKey','development'} or config['development'] is not False:
    raise SystemExit('Release requires public-only configuration and development=false')
origin = urlsplit(config['origin'])
if origin.scheme != 'https' or not origin.hostname or origin.path or origin.query or origin.fragment or origin.username or origin.password:
    raise SystemExit('Release requires a fixed HTTPS origin')
key = config['publicKey']
if not isinstance(key,str) or len(base64.b64decode(key+'='*(-len(key)%4), altchars=b'-_', validate=True)) != 32:
    raise SystemExit('Release requires the pinned Ed25519 public key')
PY
clean_signing_metadata() {
  while IFS= read -r -d '' item; do
    for attribute in com.apple.FinderInfo com.apple.ResourceFork; do
      if /usr/bin/xattr -ps "$attribute" "$item" >/dev/null 2>&1; then
        /usr/bin/xattr -ds "$attribute" "$item"
      fi
    done
  done < <(/usr/bin/find "$APP" -depth -print0)
}
# Preserve quarantine and all other protection metadata; clear only prohibited
# FinderInfo/ResourceFork before signing inner code and then the envelope.
clean_signing_metadata
# Re-sign inner Mach-O files first, then framework envelopes and finally the app.
while IFS= read -r -d '' item; do
  if /usr/bin/file -b "$item" | /usr/bin/grep -q 'Mach-O'; then
    /usr/bin/codesign --force --options runtime --timestamp --sign "$IDENTITY" "$item"
  fi
done < <(/usr/bin/find "$APP/Contents" -type f -print0)
while IFS= read -r -d '' framework; do
  /usr/bin/codesign --force --options runtime --timestamp --sign "$IDENTITY" "$framework"
done < <(/usr/bin/find "$APP/Contents" -depth -type d -name '*.framework' -print0)
/usr/bin/codesign --force --options runtime --timestamp --sign "$IDENTITY" "$APP"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$APP"
if [[ "$NOTARIZE" == 1 ]]; then
  TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/relay-notary.XXXXXX")"
  trap 'rm -rf "$TEMP_DIR"' EXIT
  /usr/bin/ditto -c -k --norsrc --extattr --qtn --keepParent "$APP" "$TEMP_DIR/Relay.zip"
  # This is the only upload operation and runs only with explicit --notarize.
  xcrun notarytool submit "$TEMP_DIR/Relay.zip" --keychain-profile "$PROFILE" --wait --output-format json > "$TEMP_DIR/result.json"
  python3 - "$TEMP_DIR/result.json" <<'PY'
import json,sys
if json.load(open(sys.argv[1])).get('status') != 'Accepted':
    raise SystemExit('Notarization was not accepted; do not distribute this build')
PY
  xcrun stapler staple "$APP"
  xcrun stapler validate "$APP"
  /usr/sbin/spctl --assess --type execute --verbose=2 "$APP"
fi
if [[ -n "$ARCHIVE" ]]; then
  clean_signing_metadata
  /usr/bin/codesign --verify --deep --strict "$APP"
  /usr/bin/ditto -c -k --norsrc --extattr --qtn --keepParent "$APP" "$ARCHIVE"
  /usr/bin/shasum -a 256 "$ARCHIVE"
fi
if [[ "$NOTARIZE" == 0 ]]; then
  echo 'Developer ID signing completed; notarization was not requested. Do not mark the download as release-verified.'
else
  echo 'Signing, notarization and stapling completed. A second-Mac installation check is still required.'
fi
