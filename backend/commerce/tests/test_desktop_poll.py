"""Public device-flow contract and regression tests; no network or real mail."""

import hashlib
import json
from datetime import timedelta
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from commerce import auth
from commerce.models import Account, DesktopAuthorization, DesktopSession, RateBucket


@override_settings(
    ENVIRONMENT="test", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"
)
class DesktopPollingTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create_user(username="alice", email="alice@example.test")
        self.other = Account.objects.create_user(username="bob", email="bob@example.test")
        self.browser = Client()
        self.browser.force_login(self.account)
        self.desktop = Client(enforce_csrf_checks=True)
        self.verifier, self.state = "v" * 64, "s" * 43
        self.started = self.post(
            "start",
            {
                "codeChallenge": auth.b64url(hashlib.sha256(self.verifier.encode()).digest()),
                "state": self.state,
                "deviceName": "Synthetic Mac",
            },
        ).json()
        self.request_id = self.started["requestId"]
        self.proof = {
            "requestId": self.request_id,
            "codeVerifier": self.verifier,
            "state": self.state,
        }

    def post(self, route, data, client=None):
        return (client or self.desktop).post(
            "/api/v1/desktop/" + route, json.dumps(data), content_type="application/json"
        )

    def approve(self, client=None):
        return self.post("approve", {"request": self.request_id}, client or self.browser)

    def test_start_contract_has_request_id_and_no_callback_secret(self):
        self.assertEqual(self.started["expiresIn"], 300)
        self.assertEqual(self.started["pollInterval"], 3)
        self.assertIn(
            "/desktop/authorize/?request=" + self.request_id, self.started["authorizeUrl"]
        )
        self.assertEqual(
            set(self.started), {"requestId", "authorizeUrl", "expiresIn", "pollInterval"}
        )
        info = self.browser.get("/api/v1/desktop/request", {"request": self.request_id})
        self.assertEqual(set(info.json()), {"deviceName", "expiresAt"})
        self.assertEqual(info.json()["deviceName"], "Synthetic Mac")
        self.assertEqual(
            self.desktop.get("/api/v1/desktop/request", {"request": self.request_id}).status_code,
            401,
        )

    def test_pending_poll_202_then_approved_consumed_once(self):
        now = timezone.now()
        with patch("commerce.auth.timezone.now", return_value=now):
            result = self.post("poll", self.proof)
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.json(), {"status": "pending"})
        self.assertFalse(DesktopSession.objects.exists())
        self.assertEqual(self.approve().json(), {"approved": True})
        with patch("commerce.auth.timezone.now", return_value=now + timedelta(seconds=3)):
            tokens = self.post("poll", self.proof)
            self.assertEqual(tokens.status_code, 200)
            self.assertEqual(tokens.json()["account"]["id"], str(self.account.pk))
            self.assertEqual(self.post("poll", self.proof).status_code, 401)
        self.assertEqual(DesktopSession.objects.count(), 1)
        self.account.refresh_from_db()
        self.assertIsNone(self.account.trial_started_at)
        self.assertTrue(DesktopAuthorization.objects.get(pk=self.request_id).consumed)

    def test_pending_poll_requires_proof_even_before_approval(self):
        for data in [
            {"requestId": self.request_id},
            self.proof | {"codeVerifier": "x" * 64},
            self.proof | {"state": "x" * 43},
        ]:
            self.assertIn(self.post("poll", data).status_code, {400, 401})
        self.assertFalse(DesktopSession.objects.exists())
        self.assertEqual(self.post("poll", self.proof).status_code, 202)

    def test_wrong_state_and_verifier_errors_persist_and_lock_at_five(self):
        self.approve()
        for attempt in range(5):
            changes = {"state": "x" * 43} if attempt % 2 else {"codeVerifier": "x" * 64}
            self.assertEqual(self.post("poll", self.proof | changes).status_code, 401)
        obj = DesktopAuthorization.objects.get(pk=self.request_id)
        self.assertEqual(obj.attempts, 5)
        self.assertEqual(self.post("poll", self.proof).status_code, 401)
        self.assertFalse(DesktopSession.objects.exists())

    def test_poll_frequency_limits_without_consuming_failure_budget(self):
        now = timezone.now()
        with patch("commerce.auth.timezone.now", return_value=now):
            self.assertEqual(self.post("poll", self.proof).status_code, 202)
            limited = self.post("poll", self.proof)
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.json()["error"]["code"], "rate_limited")
        with patch("commerce.auth.timezone.now", return_value=now + timedelta(seconds=3)):
            self.assertEqual(self.post("poll", self.proof).status_code, 202)
        self.assertEqual(DesktopAuthorization.objects.get(pk=self.request_id).attempts, 0)

    def test_poll_ip_limit_applies_to_unknown_requests(self):
        RateBucket.objects.create(
            key=auth.digest("desktop-poll:127.0.0.1"), starts_at=timezone.now(), count=600
        )
        result = self.post("poll", self.proof)
        self.assertEqual(result.status_code, 429)
        self.assertFalse(DesktopSession.objects.exists())

    def test_expired_authorization_cannot_approve_or_poll(self):
        DesktopAuthorization.objects.filter(pk=self.request_id).update(expires_at=timezone.now())
        self.assertEqual(self.approve().status_code, 409)
        self.assertEqual(self.post("poll", self.proof).status_code, 401)

    def test_cross_account_approval_cannot_rebind_authorization(self):
        self.assertEqual(self.approve().status_code, 200)
        other_browser = Client()
        other_browser.force_login(self.other)
        self.assertEqual(self.approve(other_browser).status_code, 409)
        # Extra account IDs from an untrusted client never override the browser approval.
        result = self.post("poll", self.proof | {"accountId": str(self.other.pk)})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["account"]["id"], str(self.account.pk))
        self.assertFalse(DesktopSession.objects.filter(account=self.other).exists())

    def test_proof_from_other_request_does_not_grant_tokens(self):
        other_verifier, other_state = "z" * 64, "z" * 43
        other_start = auth.desktop_start(
            auth.b64url(hashlib.sha256(other_verifier.encode()).digest()), other_state, "Other Mac"
        )
        auth.approve_request(other_start["requestId"], self.other)
        foreign_proof = self.proof | {"requestId": other_start["requestId"]}
        self.assertEqual(self.post("poll", foreign_proof).status_code, 401)
        self.assertFalse(DesktopSession.objects.exists())

    def test_disabled_or_staff_account_cannot_get_desktop_credentials(self):
        self.approve()
        Account.objects.filter(pk=self.account.pk).update(is_active=False)
        self.assertEqual(self.post("poll", self.proof).status_code, 401)
        Account.objects.filter(pk=self.account.pk).update(is_active=True, is_staff=True)
        self.assertEqual(self.post("poll", self.proof).status_code, 401)
        self.assertFalse(DesktopSession.objects.exists())

    def test_browser_approval_requires_session_and_csrf(self):
        self.assertIn(self.approve(self.desktop).status_code, {401, 403})
        strict_browser = Client(enforce_csrf_checks=True)
        strict_browser.force_login(self.account)
        self.assertEqual(self.approve(strict_browser).status_code, 403)
        self.assertIsNone(DesktopAuthorization.objects.get(pk=self.request_id).account_id)

    def test_old_callback_exchange_endpoint_no_longer_exists(self):
        self.assertEqual(self.post("exchange", {"code": "legacy"}).status_code, 404)
