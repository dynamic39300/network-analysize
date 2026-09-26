import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.conf import settings
from django.http import JsonResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from commerce.middleware import TrustedProxyMiddleware


class ProxyTests(SimpleTestCase):
    def check_ip(self, source, header):
        request = RequestFactory().get("/", REMOTE_ADDR=source, HTTP_X_REAL_IP=header)
        response = TrustedProxyMiddleware(lambda r: JsonResponse({"ip": r.client_ip}))(request)
        return response

    @override_settings(TRUSTED_PROXY_IPS=("127.0.0.1",))
    def test_only_configured_proxy_can_override_address(self):
        self.assertContains(self.check_ip("127.0.0.1", "203.0.113.20"), "203.0.113.20")
        self.assertContains(self.check_ip("203.0.113.5", "203.0.113.20"), "203.0.113.5")
        self.assertEqual(self.check_ip("127.0.0.1", "203.0.113.20, attacker").status_code, 400)
        self.assertEqual(self.check_ip("127.0.0.1", "").status_code, 400)

    @override_settings(TRUSTED_PROXY_IPS=())
    def test_proxy_headers_ignored_by_default(self):
        self.assertContains(self.check_ip("127.0.0.1", "203.0.113.20"), "127.0.0.1")


class ProductionConfigurationTests(SimpleTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "test-only-license.pem"
        key = Ed25519PrivateKey.generate()
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "RELAY_ENV": "production",
            "RELAY_RUNTIME_DIR": str(Path(self.temp.name) / "runtime"),
            "DJANGO_SECRET_KEY": "test-only-configuration-secret" * 3,
            "PUBLIC_URL": "https://relay.example.test",
            "DATABASE_URL": "postgresql://test:test@localhost/test",
            "DATABASE_SSLMODE": "verify-full",
            "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "EMAIL_HOST": "smtp.example.test",
            "EMAIL_HOST_USER": "test",
            "EMAIL_HOST_PASSWORD": "synthetic-not-real",
            "EMAIL_USE_TLS": "1",
            "DEFAULT_FROM_EMAIL": "Relay <mail@example.test>",
            "SUPPORT_EMAIL": "support@example.test",
            "LEGAL_ENTITY_NAME": "Synthetic Test Entity",
            "LICENSE_PRIVATE_KEY_FILE": str(path),
        }

    def config(self, updates):
        return subprocess.run(
            [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "WARNING"],
            cwd=settings.BASE_DIR,
            env=self.env | updates,
            text=True,
            capture_output=True,
            timeout=15,
        )

    def test_complete_synthetic_production_config_passes_deploy_checks(self):
        result = self.config({})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_rejects_development_mail_simulation_bad_keys_and_http(self):
        cases = [
            {"SIMULATED_PAYMENTS": "1"},
            {"RELAY_RUNTIME_DIR": ""},
            {"RELAY_RUNTIME_DIR": str(settings.BASE_DIR / "runtime")},
            {"EMAIL_BACKEND": "django.core.mail.backends.filebased.EmailBackend"},
            {"LICENSE_PRIVATE_KEY_FILE": "/nonexistent/key.pem"},
            {"PUBLIC_URL": "http://relay.example.test"},
            {"DATABASE_SSLMODE": "disable"},
            {"EMAIL_USE_TLS": "0"},
            {"WECHAT_ENABLED": "1"},
            {"ALIPAY_ENABLED": "1"},
        ]
        for case in cases:
            with self.subTest(setting=list(case)[0]):
                self.assertNotEqual(self.config(case).returncode, 0)

    def test_production_rejects_wrong_license_key_type(self):
        from cryptography.hazmat.primitives.asymmetric import rsa

        path = Path(self.temp.name) / "wrong-type.pem"
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        self.assertNotEqual(self.config({"LICENSE_PRIVATE_KEY_FILE": str(path)}).returncode, 0)

    def test_enabled_channel_rejects_unreadable_or_wrong_key_material(self):
        settings_patch = {
            "WECHAT_ENABLED": "1",
            "WECHAT_APP_ID": "synthetic",
            "WECHAT_MCH_ID": "synthetic",
            "WECHAT_CERT_SERIAL": "synthetic",
            "WECHAT_PRIVATE_KEY_FILE": self.env["LICENSE_PRIVATE_KEY_FILE"],
            "WECHAT_PUBLIC_KEY_ID": "synthetic",
            "WECHAT_PUBLIC_KEY_FILE": "/nonexistent/public.pem",
            "WECHAT_API_V3_KEY": "short",
        }
        self.assertNotEqual(self.config(settings_patch).returncode, 0)
