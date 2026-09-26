#!/usr/bin/env python3
"""Real loopback HTTP + the desktop client, isolated DB/mail/keys, no real charges or emails.

Run: uv run --directory backend python ../scripts/verify_commercial_flow.py
"""

import base64
import http.cookiejar
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.request import HTTPCookieProcessor, Request, build_opener

from cryptography.hazmat.primitives import serialization

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
from relay.commercial.client import AccountClient, CommercialConfig  # noqa: E402


class EphemeralStore:
    def __init__(self):
        self.data = {}

    def load(self):
        return self.data.copy()

    def save(self, data):
        self.data = data.copy()

    def clear(self):
        self.data.clear()


def main():
    with tempfile.TemporaryDirectory(prefix="relay-commerce-check-") as temp:
        runtime = Path(temp)
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        for name in ("DATABASE_URL", "TRUST_PROXY_HTTPS", "TRUSTED_PROXY_IPS"):
            env.pop(name, None)
        env.update(
            RELAY_ENV="development",
            RELAY_RUNTIME_DIR=temp,
            PUBLIC_URL=origin,
            SIMULATED_PAYMENTS="1",
            WECHAT_ENABLED="0",
            ALIPAY_ENABLED="0",
            EMAIL_BACKEND="django.core.mail.backends.filebased.EmailBackend",
            LICENSE_PRIVATE_KEY_FILE=str(runtime / "license-ed25519.pem"),
            DJANGO_SECRET_KEY=secrets.token_urlsafe(64),
        )
        command = [str(ROOT / "backend/.venv/bin/python"), "manage.py"]
        for args in (["migrate", "--noinput"], ["init_development"]):
            result = subprocess.run(
                command + args, cwd=ROOT / "backend", env=env, capture_output=True, text=True
            )
            if result.returncode:
                raise RuntimeError("Isolated backend setup failed: " + str(args[0]))
        server = subprocess.Popen(
            command + ["runserver", f"127.0.0.1:{port}", "--noreload"],
            cwd=ROOT / "backend",
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cookies = http.cookiejar.CookieJar()
        browser = build_opener(HTTPCookieProcessor(cookies))

        def request(path, data=None):
            headers = {"Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
                headers["X-CSRFToken"] = next(c.value for c in cookies if c.name == "csrftoken")
            req = Request(
                origin + path,
                headers=headers,
                data=json.dumps(data).encode() if data is not None else None,
            )
            with browser.open(req, timeout=10) as response:
                return json.load(response)

        try:
            for _ in range(50):
                try:
                    request("/healthz")
                    break
                except Exception:
                    if server.poll() is not None:
                        raise RuntimeError("Isolated backend stopped unexpectedly")
                    time.sleep(0.1)
            else:
                raise RuntimeError("Isolated backend did not become ready")
            with browser.open(origin + "/", timeout=10) as response:
                assert response.status == 200
            config = request("/api/v1/config")
            assert config["payments"]["simulated"] and not config["payments"]["wechat"]
            email = "local-smoke-" + secrets.token_hex(5) + "@example.test"
            request("/api/v1/auth/request-code", {"email": email})
            message = BytesParser(policy=policy.default).parsebytes(
                next((runtime / "mail").glob("*")).read_bytes()
            )
            body = message.get_content()
            otp = re.search(r"\b\d{6}\b", body).group()
            request(
                "/api/v1/auth/verify-code",
                {
                    "email": email,
                    "code": otp,
                    "acceptedTerms": True,
                    "termsVersion": config["termsVersion"],
                    "privacyVersion": config["privacyVersion"],
                },
            )
            assert request("/api/v1/me")["entitlement"]["tier"] == "free"
            trial = request("/api/v1/trial/start", {})
            assert trial["entitlement"]["tier"] == "pro"
            key = serialization.load_pem_private_key(
                (runtime / "license-ed25519.pem").read_bytes(), password=None
            )
            public = base64.b64encode(
                key.public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )
            ).decode()
            client = AccountClient(CommercialConfig(origin, public, True), EphemeralStore())
            attempt = client.begin_login("Synthetic Relay API validation")
            assert not client.poll_login(attempt)
            request("/api/v1/desktop/approve", {"request": attempt["requestId"]})
            time.sleep(3.1)  # Respect the production-equivalent polling interval.
            assert client.poll_login(attempt)
            assert all(client.allows(f) for f in ("history", "compare", "export_bundle"))
            quote = request("/api/v1/orders/quote?planId=yearly")
            assert quote["amount"] == 9800 and quote["currency"] == "CNY"
            payload = {
                "planId": "yearly",
                "channel": "simulated",
                "idempotencyKey": secrets.token_hex(16),
                "acceptedTerms": True,
                "termsVersion": config["termsVersion"],
                "expectedAmount": 9800,
                "expectedCurrency": "CNY",
            }
            order = request("/api/v1/orders", payload)
            assert request("/api/v1/orders", payload)["order"]["id"] == order["order"]["id"]
            order_id = order["order"]["id"]
            paid = request(f"/api/v1/orders/{order_id}/simulate", {})
            duplicate = request(f"/api/v1/orders/{order_id}/simulate", {})
            assert paid["order"]["endsAt"] == duplicate["order"]["endsAt"]
            assert paid["order"]["status"] == "paid"
            client.refresh_entitlement()
            assert client.allows("export_bundle")
            ticket = request(
                "/api/v1/support",
                {"subject": "本地验证工单", "body": "合成测试，无用户数据。", "orderId": order_id},
            )
            assert ticket["ticket"]["id"]
            refund = request(f"/api/v1/orders/{order_id}/refund", {"reason": "本地合成验证"})
            assert refund["refund"]["status"] == "requested"
            client.logout()
            assert not client.allows("history") and client.allows("basic_diagnostics")
            print(
                json.dumps(
                    {
                        "result": "PASS",
                        "transport": "real loopback HTTP",
                        "checks": [
                            "file-only email OTP and CSRF login",
                            "single-account trial",
                            "PKCE browser approval and poll",
                            "backend Ed25519 license verified by desktop",
                            "server-priced quote",
                            "order idempotency",
                            "simulated confirmation does not duplicate grant",
                            "support/refund request",
                            "logout retains Free",
                        ],
                        "realEmailSent": False,
                        "realPayment": False,
                        "persistentTestAccount": False,
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == "__main__":
    main()
