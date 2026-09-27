"""Signed, session-bound Cocoa transport for the managed ordinary-user core."""
import os
import threading
import time
import uuid

import objc
from Foundation import NSData, NSObject, NSXPCConnection, NSXPCListener

from .ipc import HEX, MAX_FRAME, TIMEOUT, VERSION, RpcError, ipc_directory, packed
from .mac_identity import MACH_SERVICE, native_bridge
from .models import strict_json
from .private_files import lock_runtime, open_private_file

BRIDGE = native_bridge()
PROTOCOL = objc.protocolNamed('RelayCoreRPC')
objc.registerMetaDataForSelector(b'NSObject', b'exchange:reply:', {'arguments': {
    3: {'type': b'@?', 'callable_retained': True, 'callable': {
        'retval': {'type': b'v'}, 'arguments': {0: {'type': b'@?'}, 1: {'type': b'@"NSData"'}}}}}})


def encoded(value):
    data = packed(value)
    if len(data) > MAX_FRAME:
        raise RpcError('frame_limit')
    return NSData.dataWithBytes_length_(data, len(data))


def decoded(data):
    if not isinstance(data, NSData) or not 0 < data.length() <= MAX_FRAME:
        raise RpcError('frame_limit')
    value = strict_json(bytes(data))
    if not isinstance(value, dict):
        raise RpcError('invalid_request')
    return value


def session_identifier():
    value = BRIDGE.sessionIdentifier()
    if value is None:
        raise PermissionError('The current audit session is unavailable')
    return int(value)


class RelayCoreXPCPeer(NSObject, protocols=[PROTOCOL]):
    def exchange_reply_(self, data, reply):
        server = self.server
        connection = self.connection
        with server.condition:
            if server.stopped or self.used or self.busy or connection is None or time.monotonic() >= self.deadline:
                if connection is not None:
                    connection.invalidate()
                return
            server.active += 1
            self.busy = True
        try:
            connection = NSXPCConnection.currentConnection()
            if (connection is None or int(connection.effectiveUserIdentifier()) != server.uid
                    or int(connection.auditSessionIdentifier()) != server.session):
                raise PermissionError('Wrong user session')
            request = decoded(data)
            if self.nonce is None:
                if set(request) != {'hello'} or not isinstance(request['hello'], str) or not HEX.fullmatch(request['hello']):
                    raise RpcError('invalid_request')
                self.nonce = uuid.uuid4().hex
                reply(encoded({'hello': request['hello'], 'nonce': self.nonce, 'version': VERSION, 'instance': server.instance}))
                return
            self.used = True
            if (set(request) != {'version', 'instance', 'nonce', 'id', 'client', 'method', 'params'}
                    or request['version'] != VERSION or request['instance'] != server.instance or request['nonce'] != self.nonce
                    or not isinstance(request['id'], str) or not HEX.fullmatch(request['id'])
                    or not isinstance(request['client'], str) or not HEX.fullmatch(request['client'])
                    or not isinstance(request['method'], str) or len(request['method']) > 64
                    or not isinstance(request['params'], dict)):
                raise RpcError('invalid_request')
            try:
                payload = {'result': server.handler(request['method'], request['params'], request['id'], request['client'])}
            except RpcError as exc:
                payload = {'error': exc.reason}
            except Exception:
                payload = {'error': 'operation_failed'}
            reply(encoded({'instance': server.instance, 'id': request['id'], 'nonce': self.nonce, **payload}))
        except Exception:
            # No handler invocation or reflected private error on bad envelopes.
            connection.invalidate()
        finally:
            with server.condition:
                server.active -= 1
                self.busy = False
                server.condition.notify_all()


class RelayCoreXPCDelegate(NSObject):
    def listener_shouldAcceptNewConnection_(self, listener, connection):
        server = self.server
        with server.condition:
            if (server.stopped or len(server.connections) >= 8
                    or int(connection.effectiveUserIdentifier()) != server.uid
                    or int(connection.auditSessionIdentifier()) != server.session):
                return False
            peer = RelayCoreXPCPeer.alloc().init()
            peer.server, peer.connection = server, connection
            peer.nonce, peer.used, peer.deadline = None, False, time.monotonic() + TIMEOUT
            peer.busy = False
            connection.setCodeSigningRequirement_(server.requirement)
            connection.setExportedInterface_(BRIDGE.interface())
            connection.setExportedObject_(peer)
            timer = threading.Timer(TIMEOUT, connection.invalidate)
            timer.daemon = True
            def invalidated():
                timer.cancel()
                with server.condition:
                    server.connections.pop(connection, None)
                    server.condition.notify_all()
                peer.connection = None
            connection.setInvalidationHandler_(invalidated)
            connection.setInterruptionHandler_(connection.invalidate)
            server.connections[connection] = peer
            connection.resume()
            timer.start()
            return True


class MacXPCServer:
    identity_mode = 'signed_build_and_session'
    def __init__(self, data_dir, handler, instance=None, *, requirement, listener=None):
        self.directory = ipc_directory(data_dir, create=True)
        self.instance, self.handler, self.requirement = instance or uuid.uuid4().hex, handler, requirement
        self.uid, self.session = os.geteuid(), session_identifier()
        self.condition = threading.Condition()
        self.connections, self.active, self.stopped = {}, 0, False
        self.owned = False
        self.fd = open_private_file(self.directory / 'owner.lock')
        self.listener = listener
        self.delegate = None
        try:
            lock_runtime(self.fd)
            self.owned = True
            self.listener = listener or NSXPCListener.alloc().initWithMachServiceName_(MACH_SERVICE)
            self.delegate = RelayCoreXPCDelegate.alloc().init()
            self.delegate.server = self
            self.listener.setDelegate_(self.delegate)
            # Routing data is not a trust root. Both peers independently pin
            # their own validated build identity before exchanging application data.
            from .lifecycle import write_private_json
            write_private_json(self.directory / 'connection.json', {'version': VERSION, 'transport': 'mac-xpc-v1',
                'instance': self.instance, 'service': MACH_SERVICE, 'uid': self.uid, 'session': self.session})
        except BaseException:
            self.close()
            raise

    def start(self):
        self.listener.resume()

    def close(self):
        with self.condition:
            self.stopped = True
            connections = list(self.connections)
        if self.listener:
            self.listener.invalidate()
        for connection in connections:
            connection.invalidate()
        # CoreService cancels work before closing transport. Do not release the
        # database owner while an authenticated request is still accessing it.
        with self.condition:
            while self.active:
                self.condition.wait(0.1)
        if self.owned:
            (self.directory / 'connection.json').unlink(missing_ok=True)
            self.owned = False
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class MacXPCClient:
    def __init__(self, descriptor, *, requirement, endpoint=None):
        if (set(descriptor) != {'version', 'transport', 'instance', 'service', 'uid', 'session'}
                or descriptor['version'] != VERSION or descriptor['transport'] != 'mac-xpc-v1'
                or descriptor['service'] != MACH_SERVICE or descriptor['uid'] != os.geteuid()
                or descriptor['session'] != session_identifier() or not isinstance(descriptor['instance'], str)
                or not HEX.fullmatch(descriptor['instance'])):
            raise RpcError('version_or_descriptor')
        self.instance, self.requirement, self.endpoint = descriptor['instance'], requirement, endpoint
        self.client_id = uuid.uuid4().hex

    def call(self, method, params=None, request_id=None):
        request_id, nonce = request_id or uuid.uuid4().hex, uuid.uuid4().hex
        deadline = time.monotonic() + TIMEOUT
        connection = (NSXPCConnection.alloc().initWithListenerEndpoint_(self.endpoint) if self.endpoint is not None
                      else NSXPCConnection.alloc().initWithMachServiceName_options_(MACH_SERVICE, 0))
        connection.setCodeSigningRequirement_(self.requirement)
        connection.setRemoteObjectInterface_(BRIDGE.interface())
        connection.resume()
        try:
            def exchange(value):
                done, result, errors = threading.Event(), [], []
                def failed(error):
                    errors.append(int(error.code()))
                    done.set()
                def received(data):
                    result.append(data)
                    done.set()
                proxy = connection.remoteObjectProxyWithErrorHandler_(failed)
                proxy.exchange_reply_(encoded(value), received)
                if not done.wait(max(0, deadline - time.monotonic())):
                    raise TimeoutError('XPC response unconfirmed; request was not replayed')
                if errors or not result:
                    raise ConnectionError('XPC identity or connection unavailable: ' + str(errors))
                return decoded(result[0])
            # A signing requirement validates received messages, not the first
            # outbound payload. Authenticate a nonce-only reply before secrets.
            hello = exchange({'hello': nonce})
            if (set(hello) != {'hello', 'nonce', 'version', 'instance'} or hello['hello'] != nonce
                    or hello['version'] != VERSION or hello['instance'] != self.instance
                    or not isinstance(hello['nonce'], str) or not HEX.fullmatch(hello['nonce'])
                    or int(connection.effectiveUserIdentifier()) != os.geteuid()
                    or int(connection.auditSessionIdentifier()) != session_identifier()):
                raise RpcError('core_changed')
            response = exchange({'version': VERSION, 'instance': self.instance, 'nonce': hello['nonce'],
                'id': request_id, 'client': self.client_id, 'method': method, 'params': params or {}})
            if (set(response) not in ({'id', 'nonce', 'instance', 'result'}, {'id', 'nonce', 'instance', 'error'})
                    or response['id'] != request_id or response['nonce'] != hello['nonce'] or response['instance'] != self.instance):
                raise RpcError('invalid_response')
            if 'error' in response:
                raise RpcError(response['error'])
            return response['result']
        finally:
            connection.invalidate()
