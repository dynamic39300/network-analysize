"""Private local storage and runtime locks with native POSIX/Windows semantics."""
import os
from pathlib import Path
import stat
import sys
from contextlib import contextmanager


class WindowsPrivateFiles:
    def __init__(self):
        import pywintypes
        import win32api
        import win32con
        import win32file
        import win32security
        self.types, self.api, self.constants = pywintypes, win32api, win32con
        self.files, self.security = win32file, win32security
        with win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY) as token:
            self.sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        self.sid_text = win32security.ConvertSidToStringSid(self.sid)

    @staticmethod
    def no_reparse(path):
        path = Path(path).absolute()
        if str(path).startswith('\\\\'):
            raise PermissionError('Agent records require a local, non-UNC directory')
        for candidate in (path, *path.parents):
            if candidate.exists() or candidate.is_symlink():
                info = candidate.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise PermissionError('Agent records cannot use reparse points')

    def mkdir(self, path):
        path = Path(path)
        self.no_reparse(path)
        if path.exists():
            self.check(path, directory=True)
            return
        if not path.parent.exists():
            self.mkdir(path.parent)
        descriptor = self.security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            f'O:{self.sid_text}D:P(A;OICI;FA;;;{self.sid_text})(A;OICI;FA;;;SY)',
            self.security.SDDL_REVISION_1)
        attributes = self.types.SECURITY_ATTRIBUTES()
        attributes.SECURITY_DESCRIPTOR = descriptor
        try:
            self.files.CreateDirectory(str(path), attributes)
        except self.types.error as exc:
            if exc.winerror != 183:
                raise
        self.check(path, directory=True)

    def check(self, path, directory=False):
        self.no_reparse(path)
        info = Path(path).lstat()
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise PermissionError('Agent record has the wrong file type')
        security = self.security
        descriptor = security.GetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT,
            security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION)
        if security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner()) != self.sid_text:
            raise PermissionError('Agent records belong to another Windows user')
        acl = descriptor.GetSecurityDescriptorDacl()
        control, _ = descriptor.GetSecurityDescriptorControl()
        if acl is None or (directory and not control & 0x1000):
            raise PermissionError('Agent records require a protected private DACL')
        entries = [acl.GetAce(index) for index in range(acl.GetAceCount())]
        owner_full_access = False
        for entry in entries:
            (kind, flags), mask, sid = entry
            allowed = security.ConvertSidToStringSid(sid)
            if kind != security.ACCESS_ALLOWED_ACE_TYPE or allowed not in (self.sid_text, 'S-1-5-18'):
                raise PermissionError('Agent records grant access outside the user and SYSTEM')
            if allowed == self.sid_text and not flags & 0x8 and mask & 0x1F01FF == 0x1F01FF:
                owner_full_access = True
        if not owner_full_access:
            raise PermissionError('Agent record ACL does not grant the owner full access')


_windows = None


def windows_files():
    global _windows
    if _windows is None:
        _windows = WindowsPrivateFiles()
    return _windows


def create_private_directory(path):
    if sys.platform == 'win32':
        windows_files().mkdir(path)
    else:
        Path(path).mkdir(parents=True, exist_ok=True, mode=0o700)
        check_private(path, directory=True)


def check_private(path, directory=False):
    if sys.platform == 'win32':
        windows_files().check(path, directory)
        return
    info = Path(path).lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError('Agent records must be owned by the current user and private')


def open_private_file(path):
    if Path(path).exists() or Path(path).is_symlink():
        check_private(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        check_private(path)
    except BaseException:
        os.close(fd)
        raise
    return fd


def lock_runtime(fd):
    if sys.platform == 'win32':
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def execution_directory():
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA', '')
        if not base or not Path(base).is_absolute():
            raise PermissionError('Private execution coordination directory is unavailable')
        return Path(base) / 'Relay' / 'execution'
    return Path.home() / 'Library/Application Support/Relay/execution'


@contextmanager
def execution_slot(directory=None):
    """Serialize cooperative writes for this OS user, across Relay data directories."""
    directory = Path(directory) if directory is not None else execution_directory()
    create_private_directory(directory)
    fd = open_private_file(directory / 'mutation.lock')
    try:
        lock_runtime(fd)
        yield
    finally:
        os.close(fd)
