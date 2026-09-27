"""Bounded current-user POSIX jobs. A process group is lifecycle control, not a sandbox."""
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time

from .private_files import check_private, execution_slot


def _persist_receipt(spec, receipt):
    path = Path(spec['receipt_path'])
    check_private(path.parent, directory=True)
    job_hash = hashlib.sha256(json.dumps(spec, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    payload = json.dumps({'schema': 'relay-job-receipt-v1', 'job_hash': job_hash, 'job': receipt},
                         ensure_ascii=True, allow_nan=False).encode('utf-8')
    # One immutable terminal receipt per job. A partial file remains explicitly unreadable after a crash.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    check_private(path)


def executable_identity(filename):
    path = Path(filename)
    if not path.is_absolute():
        raise ValueError('An absolute executable path is required')
    path = path.resolve(strict=True)
    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_IWOTH)
                or not info.st_mode & 0o111 or info.st_size > 128 * 1024 * 1024):
            raise PermissionError('Executable type, privilege bits or size is unsupported')
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'path': str(path), 'sha256': digest, 'device': info.st_dev, 'inode': info.st_ino,
            'size': info.st_size, 'mode': stat.S_IMODE(info.st_mode)}


def current_identity():
    if os.name != 'posix':
        raise NotImplementedError('Windows dynamic job containment is not connected')
    if os.geteuid() == 0 or os.getuid() != os.geteuid() or os.getgid() != os.getegid():
        raise PermissionError('Dynamic jobs require an unprivileged, non-setuid process')
    return {'uid': os.getuid(), 'gid': os.getgid(), 'groups': sorted(os.getgroups()), 'session': os.getsid(0)}


def job_environment(directory):
    return {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C', 'LANG': 'C',
            'HOME': str(directory), 'TMPDIR': str(directory)}


def _kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _supervise_locked(control, output, spec):
    """Parent EOF also stops the job after abrupt core death; no write is replayed."""
    process = None
    group_stopped = False
    streams = selectors.DefaultSelector()
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    reason, started = 'launch_failed', time.monotonic()
    receipt = {'pid': None, 'returncode': None, 'started_at': time.time(),
               'containment': 'posix_process_group_not_sandbox', 'output_truncated': False}
    try:
        output.send_bytes(b'{"event":"locked"}')
        if not control.poll(65) or control.recv_bytes() != b'go':
            raise PermissionError('Launch was not approved after preflight')
        started = time.monotonic()
        if (current_identity() != spec['identity'] or executable_identity(spec['argv'][0]) != spec['executable']
                or control.poll()):
            reason = 'precondition_changed'
        else:
            process = subprocess.Popen(spec['argv'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=spec['cwd'], env=spec['environment'], shell=False,
                start_new_session=True, close_fds=True, umask=0o077)
            receipt['pid'] = process.pid
            output.send_bytes(json.dumps({'event': 'started', 'pid': process.pid}).encode())
            pending = memoryview(spec['stdin'].encode('utf-8'))
            for name in ('stdout', 'stderr'):
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                streams.register(stream, selectors.EVENT_READ, name)
            if pending:
                os.set_blocking(process.stdin.fileno(), False)
                streams.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
            else:
                process.stdin.close()
            reason = 'exited'
            while streams.get_map() or process.poll() is None:
                if process.poll() is not None and not group_stopped:
                    _kill_group(process.pid)
                    group_stopped = True
                if control.poll():
                    reason = 'cancelled'
                    break
                if time.monotonic() - started >= spec['timeout']:
                    reason = 'timeout'
                    break
                for key, _mask in streams.select(0.025):
                    if key.data == 'stdin':
                        try:
                            written = os.write(key.fd, pending[:8192])
                            pending = pending[written:]
                        except BrokenPipeError:
                            pending = pending[:0]
                        if not pending:
                            streams.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        streams.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    remaining = spec['output_limit'] - sum(map(len, buffers.values()))
                    buffers[key.data].extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        receipt['output_truncated'] = True
                        reason = 'output_limit'
                        break
                if reason == 'output_limit':
                    break
    except Exception:
        reason = 'supervisor_error' if process else 'launch_failed'
    finally:
        if process is not None:
            try:
                # Also stop background children after the foreground command exits.
                if not group_stopped:
                    _kill_group(process.pid)
                process.wait(timeout=2)
                receipt['returncode'] = process.returncode
            except Exception:
                reason = 'cleanup_unconfirmed'
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        streams.close()
        receipt.update(reason=reason, finished_at=time.time(), elapsed=time.monotonic() - started,
                       stdout=buffers['stdout'].decode('utf-8', 'replace'),
                       stderr=buffers['stderr'].decode('utf-8', 'replace'),
                       captured_bytes=sum(map(len, buffers.values())))
        try:
            _persist_receipt(spec, receipt)
            receipt['terminal_record_saved'] = True
        except Exception:
            receipt['terminal_record_saved'] = False
        try:
            output.send_bytes(json.dumps({'event': 'finished', 'receipt': receipt}).encode())
        except (OSError, EOFError):
            pass


def _supervise(control, output, spec):
    try:
        with execution_slot(spec['coordination_dir']):
            _supervise_locked(control, output, spec)
    except Exception:
        try:
            receipt = {
                'reason': 'coordination_unavailable', 'pid': None, 'returncode': None,
                'stdout': '', 'stderr': '', 'captured_bytes': 0, 'output_truncated': False,
                'started_at': None, 'finished_at': time.time(),
                'containment': 'posix_process_group_not_sandbox'}
            try:
                _persist_receipt(spec, receipt)
                receipt['terminal_record_saved'] = True
            except Exception:
                receipt['terminal_record_saved'] = False
            output.send_bytes(json.dumps({'event': 'finished', 'receipt': receipt}).encode())
        except (OSError, EOFError):
            pass
    finally:
        control.close()
        output.close()


def run_job(spec, *, allowed=lambda: True, on_ready=lambda: None, on_started=lambda _info: None):
    """Execute one fully specified job; callers durably record launch intent first."""
    if current_identity() != spec['identity'] or not allowed():
        raise PermissionError('Job identity or authorization changed')
    context = multiprocessing.get_context('spawn')
    child_control, parent_control = context.Pipe(duplex=False)
    parent_output, child_output = context.Pipe(duplex=False)
    supervisor = context.Process(target=_supervise, args=(child_control, child_output, spec),
                                 name='NetCare job supervisor', daemon=True)
    deadline = time.monotonic() + spec['timeout'] + 70
    pid = None
    cancelled = False
    try:
        supervisor.start()
        child_control.close()
        child_output.close()
        while True:
            if not cancelled and not allowed():
                parent_control.close()
                cancelled = True
                deadline = min(deadline, time.monotonic() + 3)
            if time.monotonic() >= deadline:
                raise TimeoutError('Job supervisor did not finish')
            if parent_output.poll(0.025):
                message = json.loads(parent_output.recv_bytes(maxlength=1024 * 1024))
                if message.get('event') == 'locked':
                    on_ready()
                    if not allowed():
                        raise PermissionError('Launch authorization was revoked')
                    parent_control.send_bytes(b'go')
                    deadline = time.monotonic() + spec['timeout'] + 5
                elif message.get('event') == 'started':
                    pid = message['pid']
                    on_started({'pid': pid, 'supervisor_pid': supervisor.pid})
                elif message.get('event') == 'finished':
                    return message['receipt']
                else:
                    raise RuntimeError('Invalid supervisor response')
            elif not supervisor.is_alive():
                raise RuntimeError('Job supervisor stopped without a receipt')
    finally:
        parent_control.close()
        child_control.close()
        child_output.close()
        if supervisor.pid is not None:
            supervisor.join(3)
            if supervisor.is_alive():
                if pid is not None:
                    _kill_group(pid)
                supervisor.terminate()
                supervisor.join(2)
                if supervisor.is_alive():
                    supervisor.kill()
                    supervisor.join()
            supervisor.close()
        parent_output.close()
