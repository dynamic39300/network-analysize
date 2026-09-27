# -*- mode: python ; coding: utf-8 -*-
"""Relay arm64 app. Build through scripts/build-macos.sh from the locked uv env."""
import base64
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]
CODE = ROOT / "code"
sys.path.insert(0, str(CODE))
ICON = Path(os.environ["RELAY_BUILD_ICON"])
release = os.environ.get("RELAY_RELEASE_BUILD") == "1"
config_file = os.environ.get("RELAY_BUILD_COMMERCIAL_CONFIG", "")

datas = [(str(CODE / "assets" / "menubar"), "assets/menubar"), (str(CODE / "app_icon.png"), ".")]
# The registry discovers filenames, so include source paths as well as frozen imports.
datas += [(str(path), "relay/checks") for path in sorted((CODE / "relay/checks").glob("*.py"))]
if release and not config_file:
    raise ValueError("A release build requires an explicit public commercial configuration")
if config_file:
    config_path = Path(config_file).resolve(strict=True)
    config = json.loads(config_path.read_text())
    if set(config) != {"origin", "publicKey", "development"}:
        raise ValueError("Only origin, publicKey and development may enter the app")
    if not isinstance(config["development"], bool):
        raise ValueError("development must be a JSON boolean")
    origin = urlsplit(config["origin"])
    if (not origin.hostname or origin.path or origin.query or origin.fragment
            or origin.username or origin.password):
        raise ValueError("Commercial origin must be a canonical origin without a path")
    if origin.scheme != "https" and not (
        config["development"] and origin.scheme == "http"
        and origin.hostname in {"127.0.0.1", "localhost"}
    ):
        raise ValueError("HTTPS is required except explicit loopback development")
    public_key = config["publicKey"]
    if not isinstance(public_key, str) or len(base64.b64decode(
        public_key + "=" * (-len(public_key) % 4), altchars=b"-_", validate=True
    )) != 32:
        raise ValueError("publicKey must be a base64url Ed25519 public key")
    if release and (config["development"] or origin.scheme != "https"):
        raise ValueError("Release requires HTTPS and development=false")
    # Normalize the input filename at the staging boundary, not inside user data.
    staged = Path(os.environ["RELAY_BUILD_STAGING"]) / "relay-commercial.json"
    staged.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    datas.append((str(staged), "."))

hiddenimports = collect_submodules("relay.checks") + [
    "keyring.backends.macOS", "keyring.backends.fail", "keyring.errors",
    "PyObjCTools.AppHelper", "Foundation", "AppKit", "objc",
    "SystemConfiguration", "CoreFoundation", "ServiceManagement",
    "cryptography.hazmat.primitives.asymmetric.ed25519",
]
# Cocoa/rumps hooks collect imported extension modules; these include objc's own
# native support libraries/data without depending on the build machine's venv.
binaries = collect_dynamic_libs("objc")
binaries.append((str(ROOT / 'build/macos-native/RelayIPC.dylib'), '.'))
datas += collect_data_files("objc")

a = Analysis(
    [str(CODE / "network-doctor-menu.py")],
    pathex=[str(CODE)], binaries=binaries, datas=datas, hiddenimports=hiddenimports,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["tkinter", "pytest", "unittest", "setproctitle"], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="NetCare", debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False, console=False,
    argv_emulation=False, target_arch="arm64", codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="NetCare")
app = BUNDLE(
    coll, name="NetCare.app", icon=str(ICON), bundle_identifier="com.wangxinlei.relay",
    version="3.0.0", info_plist={
        "CFBundleName": "NetCare", "CFBundleDisplayName": "NetCare",
        "CFBundleShortVersionString": "3.0.0", "CFBundleVersion": "3.0.0",
        "LSUIElement": True, "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True, "NSPrincipalClass": "NSApplication",
    },
)
