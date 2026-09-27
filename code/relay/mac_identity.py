"""Identity comes from the executing code signature, never from a user file."""
import ctypes
from functools import lru_cache
from pathlib import Path
import re
import platform
import sys

IDENTIFIER = 'com.wangxinlei.relay'
MACH_SERVICE = IDENTIFIER + '.core'
UNSAFE_ENTITLEMENTS = ('com.apple.security.get-task-allow', 'com.apple.security.cs.disable-library-validation',
    'com.apple.security.cs.allow-dyld-environment-variables', 'com.apple.security.cs.allow-unsigned-executable-memory',
    'com.apple.security.cs.disable-executable-page-protection')


@lru_cache(maxsize=1)
def native_bridge():
    if sys.platform != 'darwin':
        raise OSError('macOS identity is unavailable')
    if getattr(sys, 'frozen', False):
        path = Path(sys.executable).resolve().parents[1] / 'Frameworks/RelayIPC.dylib'
    else:
        path = Path(__file__).resolve().parents[2] / 'build/macos-native/RelayIPC.dylib'
    ctypes.CDLL(str(path))
    import objc
    return objc.lookUpClass('RelayNativeIPC')


def requirement_for(identity):
    if not identity.get('valid'):
        raise PermissionError('The running code signature is not valid')
    if identity.get('adhoc') and not identity.get('team'):
        return None
    if (not identity.get('developer_id') or not identity.get('hardened') or identity.get('debugged')
            or identity.get('identifier') != IDENTIFIER
            or not re.fullmatch('[A-Z0-9]{10}', identity.get('team', ''))
            or not re.fullmatch('[a-f0-9]{40,64}', identity.get('cdhash', ''))
            or any(identity.get('entitlements', {}).get(key) for key in UNSAFE_ENTITLEMENTS)):
        raise PermissionError('A hardened NetCare Developer ID build is required')
    # UI and core are roles of the same frozen executable. Pin its exact code
    # hash as well as its signer; older signed builds do not inherit access.
    return ('anchor apple generic and identifier "' + IDENTIFIER + '"'
        ' and certificate 1[field.1.2.840.113635.100.6.2.6] exists'
        ' and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
        ' and certificate leaf[subject.OU] = "' + identity['team'] + '"'
        ' and cdhash H"' + identity['cdhash'] + '"')


def production_requirement():
    if sys.platform != 'darwin' or not getattr(sys, 'frozen', False):
        return None
    requirement = requirement_for(dict(native_bridge().identity()))
    if requirement and int(platform.mac_ver()[0].split('.')[0]) < 13:
        raise PermissionError('Signed XPC requires macOS 13 or later')
    return requirement
