"""Bounded, authenticated local JSON RPC. No TCP listener or pickle decoding."""
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import sys
import threading
import time
import uuid

from .models import strict_json
from .private_files import check_private, create_private_directory, lock_runtime, open_private_file
from .mac_identity import production_requirement

VERSION = 'relay-local-v1'
MAX_FRAME = 1024 * 1024
TIMEOUT = 5
HEX = re.compile(r'^[0-9a-f]{32}$')


class RpcError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def packed(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def proof(secret, role, value):
    return hmac.new(secret, role.encode() + b'\0' + packed(value), hashlib.sha256).hexdigest()


def receive(sock, deadline):
    def read(count):
        data = bytearray()
        while len(data) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('IPC deadline')
            sock.settimeout(remaining)
            part = sock.recv(count - len(data))
            if not part:
                raise EOFError('IPC disconnected')
            data.extend(part)
        return bytes(data)
    size = struct.unpack('!I', read(4))[0]
    if not 0 < size <= MAX_FRAME:
        raise RpcError('frame_limit')
    value = strict_json(read(size))
    if not isinstance(value, dict):
        raise RpcError('invalid_request')
    return value


def send(sock, value, deadline):
    data = packed(value)
    if len(data) > MAX_FRAME:
        raise RpcError('frame_limit')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('IPC deadline')
    sock.settimeout(remaining)
    sock.sendall(struct.pack('!I', len(data)) + data)


def peer_uid(sock):
    if sys.platform == 'darwin':
        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.getpeereid
        function.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        function.restype = ctypes.c_int
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if function(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise OSError(ctypes.get_errno(), 'Peer identity unavailable')
        return uid.value
    if sys.platform.startswith('linux'):
        return struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    raise NotImplementedError('Native peer identity transport is not implemented on this platform')


def private_socket(path):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError('Private socket required')


def ipc_directory(data_dir, create=False):
    if sys.platform not in ('darwin', 'linux'):
        raise NotImplementedError('This local transport requires macOS or Linux peer credentials')
    if os.geteuid() == 0 or os.getuid() != os.geteuid():
        raise PermissionError('The local core transport must run as an ordinary user')
    path = Path(data_dir).absolute() / 'ipc'
    # Resolve system aliases (/var -> /private/var on macOS), then reject a linked leaf.
    if path.is_symlink() or Path(data_dir).is_symlink():
        raise PermissionError('Linked IPC directory')
    path = path.parent.resolve() / path.name
    if create:
        create_private_directory(path)
    check_private(path, directory=True)
    if len(os.fsencode(path / 'core.sock')) >= 104:
        raise ValueError('IPC directory path is too long for a local socket')
    return path


class LocalServer:
    identity_mode = 'same_user'
    def __init__(self, data_dir, handler, instance=None):
        self.directory = ipc_directory(data_dir, create=True)
        self.handler, self.instance = handler, instance or uuid.uuid4().hex
        self.secret = secrets.token_bytes(32)
        self.stopped = threading.Event()
        self.clients = threading.BoundedSemaphore(8)
        self.threads, self.thread_lock = set(), threading.Lock()
        self.fd = open_private_file(self.directory / 'owner.lock')
        self.listener = None
        self.thread = None
        self.owned = False
        try:
            lock_runtime(self.fd)
            self.owned = True
            path = self.directory / 'core.sock'
            if path.exists() or path.is_symlink():
                private_socket(path)
                path.unlink()
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(str(path))
            os.chmod(path, 0o600)
            self.listener.listen(8)
            self.listener.settimeout(0.2)
            descriptor = self.directory / 'connection.json'
            if descriptor.exists() or descriptor.is_symlink():
                check_private(descriptor)
            temporary = self.directory / (uuid.uuid4().hex + '.tmp')
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(packed({'version': VERSION, 'instance': self.instance, 'secret': self.secret.hex()}))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, descriptor)
            finally:
                temporary.unlink(missing_ok=True)
        except BaseException:
            self.close()
            raise

    def start(self):
        self.thread = threading.Thread(target=self._accept, name='Relay local listener', daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self.clients.acquire(blocking=False):
                connection.close()
                continue
            thread = threading.Thread(target=self._client, args=(connection,), daemon=True)
            with self.thread_lock:
                self.threads.add(thread)
            thread.start()

    def _client(self, connection):
        try:
            with connection:
                if peer_uid(connection) != os.geteuid():
                    raise PermissionError('Wrong peer user')
                deadline = time.monotonic() + TIMEOUT
                hello = {'version': VERSION, 'instance': self.instance, 'nonce': secrets.token_hex(32)}
                send(connection, {**hello, 'proof': proof(self.secret, 'server', hello)}, deadline)
                request = receive(connection, deadline)
                signature = request.pop('proof', None)
                if (not isinstance(signature, str) or not hmac.compare_digest(signature, proof(self.secret, 'request', request))
                        or set(request) != {'version', 'instance', 'nonce', 'id', 'client', 'method', 'params'}
                        or any(request.get(key) != hello[key] for key in ('version', 'instance', 'nonce'))
                        or not isinstance(request['id'], str) or not HEX.fullmatch(request['id'])
                        or not isinstance(request['client'], str) or not HEX.fullmatch(request['client'])
                        or not isinstance(request['method'], str) or len(request['method']) > 64
                        or not isinstance(request['params'], dict)):
                    raise PermissionError('Invalid authentication or envelope')
                try:
                    result = self.handler(request['method'], request['params'], request['id'], request['client'])
                    payload = {'result': result}
                except RpcError as exc:
                    payload = {'error': exc.reason}
                except Exception:
                    payload = {'error': 'operation_failed'}
                response = {'id': request['id'], 'nonce': hello['nonce'], 'instance': self.instance, **payload}
                send(connection, {**response, 'proof': proof(self.secret, 'response', response)}, deadline)
        except Exception:
            # No unauthenticated diagnostic output or reflected system error details.
            pass
        finally:
            with self.thread_lock:
                self.threads.discard(threading.current_thread())
            self.clients.release()

    def close(self):
        self.stopped.set()
        if self.listener:
            self.listener.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(TIMEOUT + 1)
        with self.thread_lock:
            threads = list(self.threads)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(TIMEOUT + 1)
        if self.owned:
            for name in ('core.sock', 'connection.json'):
                (self.directory / name).unlink(missing_ok=True)
            self.owned = False
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class LocalClient:
    def __init__(self, data_dir):
        self.directory = ipc_directory(data_dir)
        self.client_id = uuid.uuid4().hex
        descriptor = self.directory / 'connection.json'
        check_private(descriptor)
        fd = os.open(descriptor, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            value = strict_json(stream.read(4097))
        self.native = None
        requirement = production_requirement()
        if requirement:
            from .mac_xpc import MacXPCClient
            if not isinstance(value, dict) or value.get('transport') != 'mac-xpc-v1':
                raise RpcError('signed_transport_required')
            self.native = MacXPCClient(value, requirement=requirement)
            self.instance, self.client_id = self.native.instance, self.native.client_id
            return
        if isinstance(value, dict) and value.get('transport') == 'mac-xpc-v1':
            raise RpcError('signed_client_required')
        if (not isinstance(value, dict) or set(value) != {'version', 'instance', 'secret'}
                or value['version'] != VERSION or not HEX.fullmatch(value['instance'])
                or not re.fullmatch('[0-9a-f]{64}', value['secret'])):
            raise RpcError('version_or_descriptor')
        self.instance, self.secret = value['instance'], bytes.fromhex(value['secret'])

    def call(self, method, params=None, request_id=None):
        if self.native:
            self.native.client_id = self.client_id
            return self.native.call(method, params, request_id)
        request_id = request_id or uuid.uuid4().hex
        deadline = time.monotonic() + TIMEOUT
        check_private(self.directory, directory=True)
        path = self.directory / 'core.sock'
        private_socket(path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(TIMEOUT)
            connection.connect(str(path))
            if peer_uid(connection) != os.geteuid():
                raise PermissionError('Wrong core user')
            hello = receive(connection, deadline)
            signature = hello.pop('proof', None)
            if (set(hello) != {'version', 'instance', 'nonce'} or hello['version'] != VERSION
                    or hello['instance'] != self.instance or not isinstance(signature, str)
                    or not hmac.compare_digest(signature, proof(self.secret, 'server', hello))):
                raise RpcError('core_changed')
            request = {**hello, 'id': request_id, 'client': self.client_id, 'method': method, 'params': params or {}}
            send(connection, {**request, 'proof': proof(self.secret, 'request', request)}, deadline)
            response = receive(connection, deadline)
            signature = response.pop('proof', None)
            if (not isinstance(signature, str) or not hmac.compare_digest(signature, proof(self.secret, 'response', response))
                    or response.get('id') != request_id or response.get('nonce') != hello['nonce']
                    or response.get('instance') != self.instance):
                raise RpcError('invalid_response')
            if 'error' in response:
                raise RpcError(response['error'])
            return response['result']


def core_server(data_dir, handler, instance, *, managed=False):
    requirement = production_requirement()
    if not requirement:
        return LocalServer(data_dir, handler, instance)
    from .core import default_directory
    if not managed or Path(data_dir).absolute() != default_directory().absolute():
        raise PermissionError('Signed cores require the managed default-directory service')
    from .mac_xpc import MacXPCServer
    return MacXPCServer(data_dir, handler, instance, requirement=requirement)
