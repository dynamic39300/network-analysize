"""Explicit ordinary-user core lifecycle. Never kills a PID or deletes recovery data."""
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .ipc import LocalClient, RpcError
from .mac_identity import production_requirement
from .private_files import check_private, create_private_directory, lock_runtime, open_private_file


def read_private_json(path):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return None
    check_private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        content = stream.read(16385)
    if len(content) > 16384:
        raise ValueError('Lifecycle record exceeds limit')
    from .models import strict_json
    return strict_json(content)


def write_private_json(path, value):
    path = Path(path)
    create_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        check_private(path)
    temporary = path.parent / (uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def inhibited(data_dir):
    value = read_private_json(Path(data_dir) / 'lifecycle/control.json')
    if value is None:
        return False
    if not isinstance(value, dict) or set(value) != {'version', 'inhibited'} or value['version'] != 1 or type(value['inhibited']) is not bool:
        raise ValueError('Invalid lifecycle control record')
    return value['inhibited']


def set_inhibited(data_dir, value):
    write_private_json(Path(data_dir) / 'lifecycle/control.json', {'version': 1, 'inhibited': bool(value)})


def core_command(data_dir, launch_id):
    base = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, str(Path(__file__).resolve().parents[1] / 'relay_app.py')]
    return [*base, '--core-service', '--data-dir', str(data_dir), '--launch-id', launch_id]


def core_environment():
    return {**{key: os.environ[key] for key in ('HOME', 'USER', 'LOGNAME', 'TMPDIR', 'LANG') if key in os.environ},
            'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'PYINSTALLER_RESET_ENVIRONMENT': '1'}


class CoreLifecycle:
    def __init__(self, data_dir, *, service=None, helper=None, command=core_command, spawn=subprocess.Popen, timeout=15):
        self.data_dir = Path(data_dir).absolute()
        self.directory = self.data_dir / 'lifecycle'
        self.service, self.command, self.spawn, self.timeout = service, command, spawn, timeout
        self.helper = helper
        self.child = None
        self.pending_stop = None

    @contextmanager
    def locked(self):
        create_private_directory(self.data_dir)
        create_private_directory(self.directory)
        fd = open_private_file(self.directory / 'operation.lock')
        try:
            lock_runtime(fd)
            yield
        finally:
            os.close(fd)

    def connected(self):
        try:
            return LocalClient(self.data_dir).call('status')
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                raise
            return None

    def owners_idle(self):
        for name in ('agent/owner.lock', 'ipc/owner.lock'):
            path = self.data_dir / name
            if not path.exists() and not path.is_symlink():
                continue
            check_private(path.parent, directory=True)
            fd = open_private_file(path)
            try:
                lock_runtime(fd)
            except OSError:
                return False
            finally:
                os.close(fd)
        return True

    def status(self):
        state = self.connected()
        registration = self.service.status() if self.service else {'available': False, 'state': 'unavailable'}
        return {'core': 'draining' if state and state.get('draining') else 'running' if state else
                'unavailable' if not self.owners_idle() else 'stopped',
                'core_instance': state.get('core_instance') if state else None,
                'inhibited': inhibited(self.data_dir), 'background': registration,
                'helper': self.helper.status() if self.helper else {'available': False, 'state': 'unavailable'},
                'signed_service_required': bool(production_requirement()),
                'data_preserved': True}

    def start(self, *, explicit=False):
        with self.locked():
            state = self.connected()
            if state:
                if state.get('draining'):
                    raise RuntimeError('Core is still draining; no restart was attempted')
                return state
            if not self.owners_idle():
                raise RuntimeError('The data directory is owned by another runtime; it was not stopped')
            if inhibited(self.data_dir) and not explicit:
                return None
            if self.child and self.child.poll() is None:
                raise RuntimeError('The previous launch is still running; no duplicate was started')
            registration = self.service.status() if self.service else {'state': 'not_registered'}
            if registration['state'] not in ('enabled', 'not_registered', 'unavailable'):
                raise PermissionError('Background service approval is required; no fallback was started')
            if production_requirement() and registration['state'] != 'enabled':
                # Registration requires the user's explicit system-service choice.
                # Never create a weaker socket listener for a signed client.
                return None
            set_inhibited(self.data_dir, False)
            launch_id = uuid.uuid4().hex
            if registration['state'] == 'enabled':
                self.service.start()
            else:
                self.child = self.spawn(self.command(self.data_dir, launch_id), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(self.data_dir),
                    env=core_environment(), start_new_session=True, close_fds=True)
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                state = self.connected()
                if state:
                    if registration['state'] != 'enabled' and state.get('launch_id') != launch_id:
                        raise RuntimeError('A different core became available; it was not replaced')
                    return state
                if registration['state'] != 'enabled' and self.child and self.child.poll() is not None:
                    raise RuntimeError('Core exited before becoming available')
                time.sleep(0.05)
            raise TimeoutError('Core startup is unconfirmed; no process was killed or restarted')

    def _stop(self, purpose, timeout):
        set_inhibited(self.data_dir, True)
        state = self.connected()
        if not state and self.pending_stop is None:
            if not self.owners_idle():
                raise RuntimeError('An unconnected runtime still owns the data; it was not killed')
            return {'state': 'already_stopped', 'data_preserved': True}
        if self.pending_stop is None:
            client = LocalClient(self.data_dir)
            self.pending_stop = (state['core_instance'], uuid.uuid4().hex)
            try:
                client.call('shutdown', {'purpose': purpose}, request_id=self.pending_stop[1])
            except RpcError as exc:
                if exc.reason in ('invalid_request', 'request_conflict', 'core_stopping'):
                    self.pending_stop = None
                raise
            except (OSError, EOFError, TimeoutError):
                pass  # Query the original instance/receipt; never resend an uncertain shutdown.
        instance, request_id = self.pending_stop
        if state and instance != state['core_instance']:
            raise RuntimeError('Core changed during shutdown; the new core was not stopped')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            receipt = read_private_json(self.directory / 'stopped.json')
            if (receipt and receipt.get('core_instance') == instance and receipt.get('request_id') == request_id
                    and receipt.get('state') == 'stopped' and self.owners_idle()):
                self.pending_stop = None
                if self.child:
                    self.child.wait(timeout=max(0.1, deadline - time.monotonic()))
                return receipt
            time.sleep(0.05)
        raise TimeoutError('Core shutdown is still unconfirmed; no force-stop or unregister was attempted')

    def stop(self, *, purpose='stop', timeout=90):
        if purpose not in ('stop', 'service_change', 'uninstall'):
            raise ValueError('Unsupported shutdown purpose')
        with self.locked():
            return self._stop(purpose, timeout)

    def background(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Background choice must be a boolean')
        with self.locked():
            if not self.service or not self.service.status()['available']:
                raise NotImplementedError('Background registration requires the supported app bundle')
            self._stop('service_change', 90)
            if enabled:
                set_inhibited(self.data_dir, False)
                try:
                    self.service.register()
                except Exception:
                    set_inhibited(self.data_dir, True)
                    raise
            else:
                self.service.unregister()
            state = self.service.status()
            expected = ('enabled', 'requires_approval') if enabled else ('not_registered',)
            if state['state'] not in expected:
                set_inhibited(self.data_dir, True)
                raise RuntimeError('Background service state was not confirmed')
            if enabled and state['state'] == 'enabled' and self.connected() is None:
                self.service.start()
            return state

    def privileged(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('System repair choice must be a boolean')
        with self.locked():
            if not self.helper or not self.helper.status()['available']:
                raise NotImplementedError('System repair requires the signed app bundle')
            self._stop('service_change', 90)
            if enabled:
                self.helper.register()
            else:
                self.helper.unregister()
            state = self.helper.status()
            expected = ('enabled', 'requires_approval') if enabled else ('not_registered',)
            if state['state'] not in expected:
                raise RuntimeError('System repair registration was not confirmed')
            return state

    def prepare_uninstall(self):
        with self.locked():
            receipt = self._stop('uninstall', 90)
            if not self.service or not self.service.status()['available']:
                raise NotImplementedError('Core stopped, but background registration could not be verified')
            if self.helper:
                if not self.helper.status()['available']:
                    raise NotImplementedError('Core stopped, but system repair registration could not be verified')
                self.helper.unregister()
                if self.helper.status()['state'] != 'not_registered':
                    raise RuntimeError('System repair removal is not confirmed')
            self.service.unregister()
            if self.service.status()['state'] != 'not_registered':
                raise RuntimeError('Background removal is not confirmed')
            return {**receipt, 'ready_to_remove_app': True, 'data_preserved': True}
