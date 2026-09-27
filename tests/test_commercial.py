"""Real signed licenses, isolated storage, adversarial inputs and account failure boundaries."""

import base64
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from relay.commercial.client import AccountClient, AccountError, CommercialConfig
from relay.commercial.history import HistoryStore, redact
from relay.commercial.license import InvalidLicense, verify_license


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class MemoryStore:
    def __init__(self):
        self.data = {}

    def load(self):
        return dict(self.data)

    def save(self, data):
        self.data = dict(data)

    def clear(self):
        self.data = {}


class LicenseTests(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.public = b64(
            self.key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )
        self.now = 1790380800
        self.origin = "https://relay.example.test"
        self.claims = {
            "iss": self.origin,
            "aud": "relay",
            "sub": "account-1",
            "sid": "session-1",
            "iat": self.now,
            "nbf": self.now,
            "exp": self.now + 604800,
            "entitlementUntil": self.now + 864000,
            "status": "paid",
            "features": ["history", "compare", "export_bundle"],
        }

    def token(self, claims=None, header=None):
        header = header or {"alg": "EdDSA", "typ": "JWT", "kid": "license-v1"}
        payload = (
            b64(json.dumps(header).encode()) + "." + b64(json.dumps(claims or self.claims).encode())
        )
        return payload + "." + b64(self.key.sign(payload.encode()))

    def verify(self, token, **kwargs):
        return verify_license(
            token,
            self.public,
            self.origin,
            "account-1",
            "session-1",
            now=kwargs.pop("now", self.now),
            **kwargs,
        )

    def test_signature_scope_and_deadlines(self):
        self.assertEqual(self.verify(self.token())["status"], "paid")
        for field, value in [
            ("aud", "appswitcher"),
            ("iss", "https://evil.test"),
            ("sub", "other"),
            ("sid", "other"),
            ("exp", self.now + 604801),
            ("entitlementUntil", self.now),
            ("iat", True),
            ("status", "expired"),
            ("features", ["shell_exec"]),
        ]:
            with self.subTest(field=field), self.assertRaises(InvalidLicense):
                self.verify(self.token({**self.claims, field: value}))
        with self.assertRaises(InvalidLicense):
            self.verify(self.token(), now=self.claims["exp"])
        with self.assertRaises(InvalidLicense):
            self.verify(self.token(), last_seen=self.now + 300)
        with self.assertRaises(InvalidLicense):
            self.verify(self.token(header={"alg": "none", "typ": "JWT", "kid": "license-v1"}))
        parts = self.token().split(".")
        parts[1] = b64(json.dumps({**self.claims, "sub": "tampered"}).encode())
        with self.assertRaises(InvalidLicense):
            self.verify(".".join(parts))

    def client(self):
        store = MemoryStore()
        client = AccountClient(CommercialConfig(self.origin, self.public), store, lambda: self.now)
        client.session = {
            "accessToken": "synthetic-access",
            "refreshToken": "synthetic-refresh",
            "sessionId": "session-1",
            "account": {"id": "account-1", "email": "local@example.test"},
            "license": self.token(),
            "lastSeen": self.now,
        }
        store.save(client.session)
        return client

    def test_offline_does_not_block_free_or_cached_pro(self):
        client = self.client()
        with patch.object(client, "_request", side_effect=AccountError("offline")):
            with self.assertRaises(AccountError):
                client.refresh_entitlement()
        self.assertTrue(client.allows("history"))
        self.now += 604800
        self.assertFalse(client.allows("history"))
        for feature in ["basic_diagnostics", "menubar_monitoring", "basic_report", "safe_repairs"]:
            self.assertTrue(client.allows(feature))
        # An expiry observed offline remains a high-water mark across restart.
        self.now -= 60
        restored = AccountClient(client.config, client.store, lambda: self.now)
        restored.restore()
        self.assertFalse(restored.allows("history"))

    def test_explicit_rejection_revokes_cached_pro(self):
        client = self.client()
        with patch.object(client, "_request", side_effect=AccountError("revoked", 401)):
            with self.assertRaises(AccountError):
                client.refresh_entitlement()
        self.assertFalse(client.allows("history"))
        self.assertEqual(client.store.data, {})
        self.assertTrue(client.allows("basic_diagnostics"))

    def test_poll_pending_never_saves_credentials(self):
        client = self.client()
        client.session = {}
        client.store.clear()
        attempt = {"requestId": "request", "codeVerifier": "v" * 64, "state": "s" * 43}
        with patch.object(client, "_request", return_value=(202, {"status": "pending"})):
            self.assertFalse(client.poll_login(attempt))
        self.assertEqual(client.store.data, {})

    def test_damaged_keychain_records_degrade_to_free(self):
        for field, value in [
            ("account", []),
            ("lastSeen", "yesterday"),
            ("lastSeen", True),
            ("accessToken", "invalid\r\nheader"),
            ("sessionId", None),
        ]:
            with self.subTest(field=field):
                client = self.client()
                client.store.data[field] = value
                self.assertEqual(client.restore()["tier"], "Free")
                self.assertTrue(client.allows("basic_diagnostics"))
                self.assertIn("缓存无效", client.message)

    def test_invalid_poll_session_is_never_persisted(self):
        client = self.client()
        client.store.clear()
        attempt = {"requestId": "request", "codeVerifier": "v" * 64, "state": "s" * 43}
        with patch.object(client, "_request", return_value=(200, {"account": []})):
            with self.assertRaises(AccountError):
                client.poll_login(attempt)
        self.assertEqual(client.store.data, {})

    def test_unsafe_origin_and_authorize_url_rejected(self):
        for origin in [
            "http://example.test",
            "https://user:secret@example.test",
            "https://example.test/path",
            "file:///tmp/test",
        ]:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                CommercialConfig(origin, self.public)
        self.assertTrue(CommercialConfig("http://127.0.0.1:8016", self.public, True).configured)
        client = self.client()
        with patch.object(
            client,
            "_request",
            return_value=(200, {"authorizeUrl": "https://evil.test/desktop/authorize/"}),
        ):
            with self.assertRaises(AccountError):
                client.begin_login()

    def test_logout_clears_local_state_even_offline(self):
        client = self.client()
        with patch.object(client, "_request", side_effect=AccountError("offline")):
            self.assertIn("本机已退出", client.logout())
        self.assertEqual(client.store.data, {})
        self.assertTrue(client.allows("safe_repairs"))

    def test_logout_refreshes_expired_access_to_revoke_session(self):
        client = self.client()
        with patch.object(
            client,
            "_request",
            side_effect=[
                AccountError("expired", 401),
                (200, {"accessToken": "rotated", "refreshToken": "rotated-refresh"}),
                (200, {"ok": True}),
            ],
        ) as request:
            self.assertIn("已退出", client.logout())
            self.assertEqual(
                [x.args[0] for x in request.call_args_list],
                ["/api/v1/desktop/logout", "/api/v1/desktop/refresh", "/api/v1/desktop/logout"],
            )
        self.assertEqual(client.store.data, {})

    def test_keychain_delete_failure_is_explicit(self):
        client = self.client()
        with (
            patch.object(client, "_request", return_value=(200, {"ok": True})),
            patch.object(
                client.store, "clear", side_effect=AccountError("keychain deletion failed")
            ),
        ):
            with self.assertRaises(AccountError):
                client.logout()
        self.assertEqual(client.session, {})


class HistoryTests(unittest.TestCase):
    def test_redaction_dedup_comparison_retention_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [1790380800]
            history = HistoryStore(
                Path(tmp) / "history.sqlite3", max_records=3, clock=lambda: now[0]
            )
            original = {
                "status": {
                    "vpn": "ok",
                    "wifi_ip": "192.168.88.10",
                    "wifi_network": "PrivateSSID",
                    "endpoint": "https://user:password@example.test/secret-path?token=not-shared",
                    "api_key": "super-private",
                    "message": "Bearer veryprivate sk-neverpublish me@example.test",
                    "ipv6": "2001:db8::1",
                    "note": "<script>alert(1)</script>",
                },
                "issues": [],
                "accessToken": "outer-secret-must-not-be-copied",
            }
            first = history.record(original)
            self.assertEqual(first, history.record(original))
            now[0] += 10
            history.record(
                {"status": {"vpn": "error"}, "issues": [["high", "vpn", "VPN unavailable"]]}
            )
            self.assertTrue(history.compare_latest()["available"])
            self.assertTrue(any(x["field"] == "vpn" for x in history.compare_latest()["changes"]))
            paths = history.export(Path(tmp) / "exports")
            self.assertEqual(json.loads(paths[0].read_text())["product"], "NetCare")
            self.assertTrue(all(path.name.startswith("netcare-report-") for path in paths))
            self.assertIn("NetCare 脱敏诊断报告", paths[1].read_text())
            texts = "\n".join(p.read_text() for p in paths)
            for secret in [
                "192.168.88.10",
                "PrivateSSID",
                "secret-path",
                "not-shared",
                "super-private",
                "veryprivate",
                "sk-neverpublish",
                "me@example.test",
                "2001:db8::1",
                "outer-secret",
            ]:
                self.assertNotIn(secret, texts)
            self.assertNotIn("<script>", paths[1].read_text())
            self.assertIn("&lt;script&gt;", paths[1].read_text())
            self.assertEqual(stat.S_IMODE(paths[0].stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(history.path.stat().st_mode), 0o600)
            self.assertNotEqual(paths, history.export(Path(tmp) / "exports"))
            for i in range(5):
                history.record({"status": {"counter": i}, "issues": []})
            self.assertEqual(len(history.list()), 3)
            now[0] += 31 * 86400
            self.assertEqual(history.list(), [])
            history.record({"status": {"fresh": True}})
            self.assertEqual(len(history.list()), 1)

    def test_empty_comparison_and_nested_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(
                HistoryStore(Path(tmp) / "history.sqlite3").compare_latest()["available"]
            )
        self.assertEqual(
            redact({"nested": {"refreshToken": "secret"}})["nested"]["refreshToken"], "[已隐藏]"
        )

    def test_real_vpn_routes_and_auth_headers_are_redacted(self):
        payload = {
            "vpn_evidence": {"routes": {"10.23.4.5": "utun8", "10.23.4.6": "utun8"}},
            "error": "Authorization: Bearer synthetic-secret-abc\nCookie: a=first-secret; b=second-secret",
        }
        safe = redact(payload)
        encoded = json.dumps(safe)
        for secret in [
            "10.23.4.5",
            "10.23.4.6",
            "synthetic-secret-abc",
            "first-secret",
            "second-secret",
        ]:
            self.assertNotIn(secret, encoded)
        self.assertEqual(len(safe["vpn_evidence"]["routes"]), 2)


if __name__ == "__main__":
    unittest.main()
