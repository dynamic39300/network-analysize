"""Bounded JSON POST in a disposable process; no credentials in argv or disk files."""
import json
import ipaddress
import multiprocessing
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class ModelError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post_worker(pipe, endpoint, headers, body, timeout, maximum):
    try:
        request = Request(endpoint, data=body, headers=headers, method='POST')
        try:
            local = ipaddress.ip_address(urlsplit(endpoint).hostname).is_loopback
        except ValueError:
            local = False
        # Loopback stays local; remote HTTPS honors standard user/environment proxy settings.
        proxy = ProxyHandler({}) if local else ProxyHandler()
        with build_opener(proxy, NoRedirect()).open(request, timeout=timeout) as response:
            content = response.read(maximum + 1)
            if len(content) > maximum:
                pipe.send_bytes(b'Eresponse_limit')
            elif response.headers.get_content_type() != 'application/json':
                pipe.send_bytes(b'Einvalid_content_type')
            else:
                pipe.send_bytes(b'J' + content)
    except HTTPError as exc:
        reason = ('authentication' if exc.code in (401, 403) else 'rate_limited' if exc.code == 429 else
                  'redirect_rejected' if 300 <= exc.code < 400 else 'provider_error')
        pipe.send_bytes(b'E' + reason.encode('ascii'))
    except Exception:
        try:
            pipe.send_bytes(b'Econnection_unavailable')
        except (OSError, EOFError):
            pass
    finally:
        pipe.close()


def post_json(endpoint, headers, payload, *, timeout=30, allowed=lambda: True,
              maximum_request=65536, maximum_response=131072):
    body = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode('utf-8')
    if len(body) > maximum_request:
        raise ModelError('context_limit')
    if timeout <= 0 or not allowed():
        raise ModelError('cancelled')
    context = multiprocessing.get_context('spawn')
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_post_worker,
        args=(send, endpoint, headers, body, timeout, maximum_response), name='NetCare model request', daemon=True)
    deadline = time.monotonic() + timeout
    try:
        process.start()
        send.close()
        while True:
            if not allowed():
                raise ModelError('cancelled')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModelError('timeout')
            if receive.poll(min(0.05, remaining)):
                try:
                    result = receive.recv_bytes(maxlength=maximum_response + 1)
                except (OSError, EOFError) as exc:
                    raise ModelError('connection_unavailable') from exc
                if not allowed():
                    raise ModelError('cancelled')
                if time.monotonic() >= deadline:
                    raise ModelError('timeout')
                if result[:1] != b'J':
                    raise ModelError(result[1:].decode('ascii') if result[:1] == b'E' else 'invalid_response')
                return result[1:]
            if not process.is_alive():
                raise ModelError('connection_unavailable')
    finally:
        receive.close()
        send.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(2)
            if process.is_alive():
                process.kill()
                process.join()
            process.close()
