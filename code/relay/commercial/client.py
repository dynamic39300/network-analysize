"""Bounded account requests, browser authorization and macOS-only credential storage."""

import base64
import hashlib
import json
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .license import InvalidLicense, verify_license


class AccountError(Exception):
    def __init__(self, message, status=0, code="unavailable"):
        super().__init__(message)
        self.status, self.code = status, code


@dataclass(frozen=True)
class CommercialConfig:
    origin: str = ""
    public_key: str = ""
    development: bool = False

    def __post_init__(self):
        if not self.origin:
            return
        p = urlsplit(self.origin)
        if p.path or p.query or p.fragment or p.username or p.password or not p.hostname:
            raise ValueError("Account service must be a fixed origin")
        if p.scheme != "https" and not (
            self.development and p.scheme == "http" and p.hostname in {"127.0.0.1", "localhost"}
        ):
            raise ValueError("Account service requires HTTPS")
        if not self.public_key:
            raise ValueError("Pinned public key is required")

    @classmethod
    def load(cls, filename):
        path = Path(filename)
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        return cls(
            data.get("origin", "").rstrip("/"),
            data.get("publicKey", ""),
            data.get("development") is True,
        )

    @property
    def configured(self):
        return bool(self.origin and self.public_key)


class KeychainStore:
    """Explicit native backend: no environment-selected plaintext fallback."""

    def __init__(self, origin):
        self.service = "com.relay.account." + hashlib.sha256(origin.encode()).hexdigest()[:16]

    def _backend(self):
        if sys.platform != "darwin":
            raise AccountError("账号凭证需要 macOS 系统钥匙串。")
        from keyring.backends.macOS import Keyring

        return Keyring()

    def load(self):
        try:
            value = self._backend().get_password(self.service, "session")
            return json.loads(value) if value else {}
        except AccountError:
            raise
        except Exception as exc:
            raise AccountError("无法读取系统钥匙串，基础诊断仍可使用。") from exc

    def save(self, data):
        try:
            self._backend().set_password(self.service, "session", json.dumps(data))
        except Exception as exc:
            raise AccountError("无法保存到系统钥匙串，请重新登录。") from exc

    def clear(self):
        from keyring.errors import PasswordDeleteError

        try:
            self._backend().delete_password(self.service, "session")
        except PasswordDeleteError:
            pass
        except Exception as exc:
            raise AccountError("无法清除钥匙串，请在系统钥匙串中移除 NetCare 账号。") from exc


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _valid_session(data):
    """Reject damaged Keychain records before they can reach the menu or HTTP headers."""
    if not isinstance(data, dict):
        return False
    account = data.get("account")
    if not isinstance(account, dict):
        return False
    for value in (
        data.get("accessToken"),
        data.get("refreshToken"),
        data.get("sessionId"),
        account.get("id"),
    ):
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 4096
            or any(ord(c) < 33 for c in value)
        ):
            return False
    return (
        isinstance(account.get("email"), str)
        and len(account["email"]) <= 320
        and type(data.get("lastSeen", 0)) is int
        and 0 <= data.get("lastSeen", 0) < 2**53
        and (data.get("license") is None or isinstance(data["license"], str))
    )


class AccountClient:
    def __init__(self, config, store=None, clock=time.time):
        self.config, self.clock = config, clock
        self.store = store or KeychainStore(config.origin)
        self.session = {}
        self.message = "基础诊断无需登录"
        self._lock = threading.RLock()
        self._opener = build_opener(_NoRedirect())

    def restore(self):
        if self.config.configured:
            data = self.store.load()
            with self._lock:
                self.session = data if _valid_session(data) else {}
                if data and not self.session:
                    self.message = "本机账号缓存无效，请重新登录；基础诊断仍可使用。"
        return self.summary()

    def _request(self, path, data=None, bearer=False):
        if not self.config.configured:
            raise AccountError("账号服务尚未开放。基础检测和修复可以继续使用。")
        headers = {"Accept": "application/json"}
        if bearer:
            token = self.session.get("accessToken")
            if not token:
                raise AccountError("请先登录 NetCare 账号。", 401)
            headers["Authorization"] = "Bearer " + token
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = Request(self.config.origin + path, data=body, headers=headers)
        try:
            with self._opener.open(req, timeout=10) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise AccountError("账号服务返回内容过大。")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise AccountError("账号服务返回格式无效，请稍后重试。")
                return response.status, result
        except HTTPError as exc:
            try:
                error = json.loads(exc.read(8192)).get("error", {})
                message = str(error.get("message", "账号请求失败，请稍后重试。"))[:240]
                code = str(error.get("code", "request_failed"))[:80]
            except Exception:
                message, code = "账号请求失败，请稍后重试。", "request_failed"
            raise AccountError(message, exc.code, code) from None
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise AccountError("账号服务暂时不可达，已缓存权益按有效期继续使用。") from exc

    def begin_login(self, device_name="我的 Mac"):
        verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        _, result = self._request(
            "/api/v1/desktop/start",
            {"codeChallenge": challenge, "state": state, "deviceName": device_name[:80]},
        )
        url = result["authorizeUrl"]
        p = urlsplit(url)
        if p.scheme + "://" + p.netloc != self.config.origin or p.path != "/desktop/authorize/":
            raise AccountError("登录地址验证失败。")
        return {
            "requestId": result["requestId"],
            "codeVerifier": verifier,
            "state": state,
            "authorizeUrl": url,
            "expiresIn": min(result["expiresIn"], 300),
        }

    def poll_login(self, attempt):
        with self._lock:
            status, data = self._request(
                "/api/v1/desktop/poll",
                {key: attempt[key] for key in ("requestId", "codeVerifier", "state")},
            )
            if status == 202:
                return False
            data["lastSeen"] = int(self.clock())
            if not _valid_session(data):
                raise AccountError("账号服务返回会话无效，请重新登录。")
            self.store.save(data)
            self.session = data
            self.refresh_entitlement()
            return True

    def refresh_entitlement(self):
        with self._lock:
            if not self.session.get("accessToken"):
                return self.summary()
            try:
                try:
                    _, data = self._request("/api/v1/me", bearer=True)
                except AccountError as exc:
                    if exc.status != 401:
                        raise
                    _, tokens = self._request(
                        "/api/v1/desktop/refresh",
                        {"refreshToken": self.session.get("refreshToken", "")},
                    )
                    self.session.update(tokens)
                    self.store.save(self.session)
                    _, data = self._request("/api/v1/me", bearer=True)
                token = data.get("license")
                if token:
                    verify_license(
                        token,
                        self.config.public_key,
                        self.config.origin,
                        self.session["account"]["id"],
                        self.session["sessionId"],
                        now=self.clock(),
                        last_seen=self.session.get("lastSeen", 0),
                    )
                self.session.update(
                    license=token,
                    entitlement=data.get("entitlement", {}),
                    lastSeen=int(self.clock()),
                )
                self.store.save(self.session)
                self.message = "权益已同步"
            except AccountError as exc:
                if exc.status in {401, 403}:
                    self.session = {}
                    self.store.clear()
                    self.message = "登录已失效，请重新登录；基础诊断不受影响。"
                else:
                    self.message = str(exc)
                raise
            except InvalidLicense as exc:
                self.session["license"] = None
                self.store.save(self.session)
                raise AccountError("授权签名或时间校验失败，请重新登录同步。") from exc
            return self.summary()

    def allows(self, feature):
        if feature in {"basic_diagnostics", "menubar_monitoring", "basic_report", "safe_repairs"}:
            return True
        with self._lock:
            try:
                now = int(self.clock())
                previous = self.session.get("lastSeen", 0)
                if self.session and now >= previous + 60:
                    # Persist a high-water mark in Keychain, including after expiry.
                    self.session["lastSeen"] = now
                    self.store.save(self.session)
                claims = verify_license(
                    self.session.get("license"),
                    self.config.public_key,
                    self.config.origin,
                    self.session.get("account", {}).get("id"),
                    self.session.get("sessionId"),
                    now=self.clock(),
                    last_seen=self.session.get("lastSeen", 0),
                )
                return feature in claims["features"]
            except (InvalidLicense, AccountError):
                return False

    def summary(self):
        with self._lock:
            return {
                "email": self.session.get("account", {}).get("email"),
                "tier": "Pro" if self.allows("history") else "Free",
                "message": self.message,
            }

    def logout(self):
        with self._lock:
            error = None
            try:
                if self.session.get("accessToken"):
                    try:
                        self._request("/api/v1/desktop/logout", {}, bearer=True)
                    except AccountError as exc:
                        if exc.status != 401:
                            raise
                        _, tokens = self._request(
                            "/api/v1/desktop/refresh",
                            {"refreshToken": self.session.get("refreshToken", "")},
                        )
                        self.session.update(tokens)
                        self._request("/api/v1/desktop/logout", {}, bearer=True)
            except AccountError:
                error = "本机已退出，未能确认服务器会话已撤销；可在账户页再次撤销这台设备。"
            finally:
                self.session = {}
                if self.config.configured:
                    self.store.clear()
                self.message = error or "已退出，基础诊断仍可使用。"
            return self.message
