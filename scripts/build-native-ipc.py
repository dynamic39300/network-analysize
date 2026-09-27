#!/usr/bin/env python3
"""Compile the small Cocoa protocol/identity bridge. No service registration."""
from pathlib import Path
import subprocess
import sys


def main():
    if sys.platform != 'darwin':
        raise SystemExit('The native IPC bridge requires macOS and Command Line Tools')
    root = Path(__file__).resolve().parents[1]
    output = root / 'build/macos-native/RelayIPC.dylib'
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['/usr/bin/xcrun', 'clang', '-dynamiclib', '-fobjc-arc', '-Wall', '-Wextra', '-Werror',
        '-mmacosx-version-min=12.0', '-arch', 'arm64', '-framework', 'Foundation', '-framework', 'Security',
        '-framework', 'SystemConfiguration', '-lbsm', '-install_name', '@rpath/RelayIPC.dylib',
        str(root / 'apps/macos/native/RelayIPC.m'), str(root / 'apps/macos/native/RelayMutation.m'),
        str(root / 'apps/macos/native/RelaySignature.m'), str(root / 'apps/macos/native/RelayHelperService.m'),
        '-o', str(output)], check=True)
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(output)], check=True)
    helper = output.with_name('RelayHelper')
    subprocess.run(['/usr/bin/xcrun', 'clang', '-fobjc-arc', '-Wall', '-Wextra', '-Werror',
        '-mmacosx-version-min=13.0', '-arch', 'arm64', '-framework', 'Foundation', '-framework', 'Security',
        '-framework', 'SystemConfiguration', str(root / 'apps/macos/native/RelayHelper.m'),
        str(root / 'apps/macos/native/RelayMutation.m'), str(root / 'apps/macos/native/RelaySignature.m'),
        str(root / 'apps/macos/native/RelayHelperService.m'), '-sectcreate', '__TEXT', '__info_plist',
        str(root / 'apps/macos/native/RelayHelper-Info.plist'), '-o', str(helper)], check=True)
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', '--identifier', 'com.wangxinlei.relay.helper', str(helper)], check=True)
    print(output)


if __name__ == '__main__':
    main()
