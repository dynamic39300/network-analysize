import base64
import json
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from commerce import auth
from commerce.domain import FREE_FEATURES, PRO_FEATURES, entitlement, start_trial
from commerce.errors import APIError
from commerce.models import Account, EmailChallenge, Order


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RelayContractTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create_user(username="relay", email="relay@example.test")
        self.client.force_login(self.account)

    def test_public_configuration_and_prices_are_relay_specific(self):
        config = self.client.get("/api/v1/config").json()
        self.assertEqual(config["productName"], "NetCare")
        self.assertEqual(
            [(p["id"], p["amount"], p["months"]) for p in config["plans"]],
            [("monthly", 1200, 1), ("yearly", 9800, 12)],
        )
        self.assertEqual(config["termsVersion"], "2026-09-26")
        self.assertEqual(config["privacyVersion"], "2026-09-26")
        self.assertEqual(config["download"]["minimumOS"], "12.0")
        self.assertEqual(config["freeFeatures"], list(FREE_FEATURES))
        self.assertEqual(config["proFeatures"], list(PRO_FEATURES))
        self.assertFalse(config["download"]["available"])
        self.assertNotIn("publicKey", config)
        self.assertNotIn("code", config)
        self.assertEqual(self.client.get("/api/v1/orders/quote?planId=quarterly").status_code, 400)

    def test_public_shell_and_login_email_use_netcare(self):
        from django.core import mail
        for route in ('/', '/features/', '/download/', '/pricing/', '/account/', '/privacy/', '/terms/', '/support/'):
            response = self.client.get(route)
            self.assertContains(response, 'NetCare')
            self.assertNotContains(response, 'Relay')
        auth.send_code('branding@example.test', '127.0.0.1')
        self.assertEqual(mail.outbox[-1].subject, 'NetCare 登录验证码')

    def test_free_before_trial_and_after_expiration_with_no_signed_license(self):
        before = self.client.get("/api/v1/me").json()
        self.assertEqual(before["entitlement"]["tier"], "free")
        self.assertEqual(before["entitlement"]["features"], [])
        self.assertIsNone(before["license"])
        start_trial(self.account)
        self.account.refresh_from_db()
        active = entitlement(self.account)
        self.assertEqual(active["tier"], "pro")
        self.assertEqual(active["features"], list(PRO_FEATURES))
        after = entitlement(self.account, self.account.trial_ends_at)
        self.assertEqual(after["status"], "expired")
        self.assertEqual(after["tier"], "free")
        self.assertEqual(after["features"], [])
        self.assertIsNone(after["validUntil"])
        self.assertFalse(Order.objects.exists())

    def test_mail_failure_invalidates_challenge_and_does_not_login(self):
        for email, result in [("failure@example.test", RuntimeError()), ("zero@example.test", 0)]:
            with patch("commerce.auth.send_mail") as send:
                if isinstance(result, Exception):
                    send.side_effect = result
                else:
                    send.return_value = result
                with self.assertRaises(APIError) as error:
                    auth.send_code(email, "127.0.0.1")
                self.assertEqual(error.exception.status, 503)
                self.assertTrue(EmailChallenge.objects.get(email=email).consumed)

    def test_admin_account_cannot_log_in_by_public_email_code(self):
        self.account.is_staff = True
        self.account.save()
        with patch("commerce.auth.secrets.randbelow", return_value=112233):
            auth.send_code(self.account.email, "127.0.0.1")
        client = Client()
        response = client.post(
            "/api/v1/auth/verify-code",
            json.dumps(
                {
                    "email": self.account.email,
                    "code": "112233",
                    "acceptedTerms": True,
                    "termsVersion": settings.TERMS_VERSION,
                    "privacyVersion": settings.PRIVACY_VERSION,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(client.get("/api/v1/me").status_code, 401)

    def test_expired_email_code_cannot_authenticate(self):
        with patch("commerce.auth.secrets.randbelow", return_value=112233):
            auth.send_code(self.account.email, "127.0.0.1")
        EmailChallenge.objects.filter(email=self.account.email).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        client = Client()
        response = client.post(
            "/api/v1/auth/verify-code",
            json.dumps(
                {
                    "email": self.account.email,
                    "code": "112233",
                    "acceptedTerms": True,
                    "termsVersion": settings.TERMS_VERSION,
                    "privacyVersion": settings.PRIVACY_VERSION,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_development_public_signature_fixture_matches_contract(self):
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from django.core.management import call_command

        with TemporaryDirectory() as directory:
            target = Path(directory) / "public.json"
            call_command("license_fixture", str(target), stdout=StringIO())
            fixture = json.loads(target.read_text())
        encoded_header, payload, _ = fixture["license"].split(".")
        header = json.loads(base64.urlsafe_b64decode(encoded_header + "=="))
        claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
        self.assertEqual(header, {"alg": "EdDSA", "typ": "JWT", "kid": "license-v1"})
        self.assertEqual(claims["aud"], "relay")
        self.assertEqual(claims["features"], list(PRO_FEATURES))
        self.assertEqual(claims["exp"] - claims["iat"], 7 * 86400)
        self.assertEqual(claims["entitlementUntil"] - claims["iat"], 14 * 86400)
        self.assertNotIn("private", json.dumps(fixture).lower())
