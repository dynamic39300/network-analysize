import base64
import hashlib
import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from commerce import auth
from commerce.domain import (
    SHANGHAI,
    add_months,
    confirm_refund,
    entitlement,
    grant_payment,
    start_trial,
)
from commerce.errors import APIError
from commerce.models import (
    Account,
    DesktopSession,
    EmailChallenge,
    EntitlementGrant,
    Order,
    Refund,
    Release,
)


def date(year, month, day, hour=10):
    return datetime(year, month, day, hour, tzinfo=SHANGHAI)


@override_settings(
    ENVIRONMENT="development",
    SIMULATED_PAYMENTS=True,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class CommerceTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create_user(username="alice", email="alice@example.test")
        self.other = Account.objects.create_user(username="bob", email="bob@example.test")
        self.client.force_login(self.account)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = Ed25519PrivateKey.generate()
        path = Path(self.temp.name) / "license.pem"
        path.write_bytes(
            self.key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        self.settings_override = override_settings(LICENSE_PRIVATE_KEY_FILE=str(path))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def post(self, path, data=None, client=None, **extra):
        if path in {"auth/verify-code", "orders"}:
            data = {
                "acceptedTerms": True,
                "termsVersion": "2026-09-26",
                "privacyVersion": "2026-09-26",
            } | (data or {})
        if path == "orders":
            plan = {"monthly": 1200, "yearly": 9800}.get((data or {}).get("planId"), 1200)
            data = {"expectedAmount": plan, "expectedCurrency": "CNY"} | (data or {})
        return (client or self.client).post(
            "/api/v1/" + path, data=json.dumps(data or {}), content_type="application/json", **extra
        )

    def order(self, months=1, account=None, channel="simulated"):
        return Order.objects.create(
            account=account or self.account,
            idempotency_key=secrets.token_hex(16),
            plan_id="monthly",
            months=months,
            amount=1200,
            channel=channel,
        )

    def pay(self, order, when):
        with patch("commerce.domain.timezone.now", return_value=when):
            return grant_payment(order.pk, "txn_" + order.pk.hex, when, order.amount)

    def desktop_tokens(self):
        session = DesktopSession(account=self.account, label="Synthetic Mac")
        return auth.tokens_for(session)

    def test_month_end_anchor_recovers_and_leap_year(self):
        self.assertEqual(add_months(date(2027, 1, 31), 1), date(2027, 2, 28))
        self.assertEqual(add_months(date(2027, 1, 31), 2), date(2027, 3, 31))
        self.assertEqual(add_months(date(2028, 2, 29), 12), date(2029, 2, 28))
        self.assertEqual(add_months(date(2028, 2, 29), 48), date(2032, 2, 29))

    def test_early_renewal_keeps_anchor(self):
        first, second = self.order(), self.order()
        self.pay(first, date(2027, 1, 31))
        self.pay(second, date(2027, 2, 10))
        second.refresh_from_db()
        self.assertEqual(second.grant.starts_at, date(2027, 2, 28))
        self.assertEqual(second.grant.ends_at, date(2027, 3, 31))

    def test_month_to_year_appends_without_conversion(self):
        first, year = self.order(), self.order(months=12)
        self.pay(first, date(2026, 9, 25))
        self.pay(year, date(2026, 10, 10))
        self.assertEqual(year.grant.ends_at, date(2027, 10, 25))

    def test_expired_purchase_uses_grant_time_not_old_callback(self):
        first, second = self.order(), self.order()
        self.pay(first, date(2026, 9, 25))
        with patch("commerce.domain.timezone.now", return_value=date(2026, 11, 3)):
            grant_payment(second.pk, "late-payment", date(2026, 11, 1), second.amount)
        self.assertEqual(second.grant.starts_at, date(2026, 11, 3))
        self.assertEqual(second.grant.ends_at, date(2026, 12, 3))

    def test_trial_explicit_single_global_period(self):
        self.assertEqual(entitlement(self.account)["status"], "eligible")
        before = timezone.now()
        response = self.post("trial/start")
        self.assertEqual(response.status_code, 200)
        self.account.refresh_from_db()
        self.assertEqual(
            self.account.trial_ends_at - self.account.trial_started_at, timedelta(days=14)
        )
        self.assertGreaterEqual(self.account.trial_started_at, before)
        self.assertEqual(self.post("trial/start").status_code, 409)
        another = Client()
        another.force_login(self.account)
        self.assertEqual(
            another.get("/api/v1/me").json()["entitlement"], response.json()["entitlement"]
        )

    def test_trial_purchase_preserves_remaining_trial(self):
        with patch("commerce.domain.timezone.now", return_value=date(2026, 9, 1)):
            start_trial(self.account)
        order = self.order()
        self.pay(order, date(2026, 9, 3))
        self.assertEqual(order.grant.starts_at, date(2026, 9, 15))
        self.assertEqual(order.grant.ends_at, date(2026, 10, 15))
        self.account.refresh_from_db()
        self.assertEqual(entitlement(self.account, date(2026, 10, 15))["status"], "expired")

    def test_direct_purchase_prevents_later_trial(self):
        self.pay(self.order(), timezone.now())
        self.assertEqual(self.post("trial/start").status_code, 409)

    def test_duplicate_payment_is_idempotent(self):
        order = self.order()
        self.pay(order, timezone.now())
        self.pay(order, timezone.now() + timedelta(days=2))
        self.assertEqual(EntitlementGrant.objects.filter(order=order).count(), 1)

    def test_amount_currency_and_transaction_mismatch_never_grant(self):
        order = self.order()
        for amount, currency in [(1, "CNY"), (1200, "USD")]:
            with self.assertRaises(APIError):
                grant_payment(order.pk, "bad", timezone.now(), amount, currency)
        self.assertEqual(EntitlementGrant.objects.count(), 0)
        self.pay(order, timezone.now())
        with self.assertRaises(APIError):
            grant_payment(order.pk, "different", timezone.now(), 1200)

    def test_refund_future_order_retains_other_purchase(self):
        one, two, three = self.order(), self.order(), self.order()
        self.pay(one, date(2027, 1, 31))
        self.pay(two, date(2027, 2, 1))
        self.pay(three, date(2027, 2, 2))
        refund = Refund.objects.create(order=two, reason="unused renewal")
        with patch("commerce.domain.timezone.now", return_value=date(2027, 2, 3)):
            confirm_refund(refund.pk)
            confirm_refund(refund.pk)
        self.assertEqual(three.grant.starts_at, date(2027, 2, 28))
        self.assertEqual(three.grant.ends_at, date(2027, 3, 31))
        self.assertEqual(one.grant.ends_at, date(2027, 2, 28))
        self.assertIsNotNone(two.grant.revoked_at)
        self.assertEqual(Order.objects.filter(status="paid").count(), 2)

    def test_refund_used_order_does_not_debt_other_orders(self):
        first, second = self.order(), self.order()
        self.pay(first, date(2027, 1, 1))
        self.pay(second, date(2027, 1, 3))
        refund = Refund.objects.create(order=first, reason="first purchase refund")
        with patch("commerce.domain.timezone.now", return_value=date(2027, 1, 5)):
            confirm_refund(refund.pk)
        self.assertEqual(second.grant.starts_at, date(2027, 1, 5))
        self.assertEqual(second.grant.ends_at, date(2027, 2, 5))

    def test_order_idempotency_and_quote(self):
        data = {
            "planId": "monthly",
            "channel": "simulated",
            "idempotencyKey": "unique-test-order-1",
        }
        first, second = self.post("orders", data), self.post("orders", data)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["order"]["id"], second.json()["order"]["id"])
        data["planId"] = "yearly"
        self.assertEqual(self.post("orders", data).status_code, 409)
        self.assertEqual(
            self.client.get("/api/v1/orders/quote?planId=monthly").json()["amount"], 1200
        )

    def test_complete_local_purchase_refund_request(self):
        response = self.post(
            "orders",
            {"planId": "yearly", "channel": "simulated", "idempotencyKey": "complete-local-flow"},
        )
        number = response.json()["order"]["id"]
        self.assertEqual(self.post(f"orders/{number}/simulate").json()["order"]["status"], "paid")
        response = self.post(f"orders/{number}/refund", {"reason": "首次购买不适合我的工作流"})
        self.assertEqual(response.json()["refund"]["status"], "requested")
        self.assertEqual(self.client.get("/api/v1/me").json()["entitlement"]["status"], "paid")

    def test_cross_account_orders_refunds_support_and_sessions(self):
        foreign = self.order(account=self.other)
        session = DesktopSession(account=self.other, label="Other")
        auth.tokens_for(session)
        self.assertEqual(self.client.get(f"/api/v1/orders/{foreign.pk}").status_code, 404)
        self.assertEqual(self.post(f"orders/{foreign.pk}/simulate").status_code, 404)
        self.assertEqual(
            self.post(f"orders/{foreign.pk}/refund", {"reason": "foreign refund"}).status_code, 404
        )
        self.assertEqual(self.post(f"sessions/{session.pk}/revoke").status_code, 404)
        self.assertEqual(
            self.post(
                "support",
                {"subject": "Order question", "body": "Details", "orderId": str(foreign.pk)},
            ).status_code,
            404,
        )

    def test_support_html_is_data_and_isolated(self):
        response = self.post("support", {"subject": "Bug", "body": "<script>alert(1)</script>"})
        ticket = response.json()["ticket"]["id"]
        self.assertEqual(
            self.post(f"support/{ticket}/messages", {"body": "follow up"}).status_code, 200
        )
        self.client.force_login(self.other)
        self.assertEqual(self.client.get("/api/v1/support").json(), {"tickets": []})
        self.assertEqual(
            self.post(f"support/{ticket}/messages", {"body": "foreign"}).status_code, 404
        )

    def test_unconfigured_channel_and_production_simulation_reject(self):
        with override_settings(WECHAT_ENABLED=False):
            self.assertEqual(
                self.post(
                    "orders",
                    {
                        "planId": "monthly",
                        "channel": "wechat",
                        "idempotencyKey": "wechat-disabled-123",
                    },
                ).status_code,
                503,
            )
        order = self.order()
        with override_settings(ENVIRONMENT="production"):
            self.assertEqual(self.post(f"orders/{order.pk}/simulate").status_code, 404)
            self.assertFalse(self.client.get("/api/v1/config").json()["payments"]["simulated"])

    def test_missing_release_is_honest(self):
        self.assertFalse(self.client.get("/api/v1/releases/latest").json()["available"])
        Release.objects.create(
            version="0.4.0",
            url="https://example.test/test.dmg",
            sha256="a" * 64,
            notes="test",
            published=True,
        )
        self.assertFalse(self.client.get("/api/v1/config").json()["download"]["available"])

    def test_email_code_not_in_response_one_use(self):
        self.client.logout()
        with patch("commerce.auth.secrets.randbelow", return_value=112233):
            response = self.post("auth/request-code", {"email": "new@example.test"})
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            self.post(
                "auth/verify-code", {"email": "new@example.test", "code": "112233"}
            ).status_code,
            200,
        )
        self.client.logout()
        self.assertEqual(
            self.post(
                "auth/verify-code", {"email": "new@example.test", "code": "112233"}
            ).status_code,
            401,
        )

    def test_email_five_failures_and_expiry(self):
        self.client.logout()
        auth.send_code("new@example.test", "test-ip")
        for _ in range(5):
            self.assertEqual(
                self.post(
                    "auth/verify-code", {"email": "new@example.test", "code": "wrong!"}
                ).status_code,
                401,
            )
        self.assertEqual(EmailChallenge.objects.get(pk="new@example.test").attempts, 5)
        self.assertEqual(
            self.post("auth/request-code", {"email": "new@example.test"}).status_code, 429
        )

    def test_browser_csrf_required_even_with_fake_bearer(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.account)
        self.assertEqual(self.post("trial/start", client=browser).status_code, 403)
        self.assertEqual(
            self.post(
                "trial/start", client=browser, HTTP_AUTHORIZATION="Bearer fabricated"
            ).status_code,
            401,
        )
        self.assertEqual(
            self.post(
                "auth/request-code", {"email": "test@example.test"}, client=browser
            ).status_code,
            403,
        )

    def test_valid_bearer_csrf_exempt_but_not_web_approval(self):
        tokens = self.desktop_tokens()
        desktop = Client(enforce_csrf_checks=True)
        headers = {"HTTP_AUTHORIZATION": "Bearer " + tokens["accessToken"]}
        response = self.post("trial/start", client=desktop, **headers)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.json()["license"])
        self.assertEqual(
            self.post("desktop/approve", {"request": "bad"}, client=desktop, **headers).status_code,
            401,
        )

    def test_pkce_approval_poll_and_replay(self):
        verifier, state = "x" * 43, "s" * 43
        challenge = auth.b64url(hashlib.sha256(verifier.encode()).digest())
        response = self.post(
            "desktop/start", {"codeChallenge": challenge, "state": state, "deviceName": "My Mac"}
        )
        request_id = response.json()["requestId"]
        approved = self.post("desktop/approve", {"request": request_id})
        self.assertEqual(approved.json(), {"approved": True})
        self.assertEqual(self.post("desktop/approve", {"request": request_id}).status_code, 409)
        data = {"requestId": request_id, "codeVerifier": "wrong" * 10, "state": state}
        self.assertEqual(self.post("desktop/poll", data).status_code, 401)
        data["codeVerifier"] = verifier
        self.assertEqual(self.post("desktop/poll", data).status_code, 200)
        self.assertEqual(self.post("desktop/poll", data).status_code, 401)
        self.assertEqual(DesktopSession.objects.count(), 1)

    def test_refresh_rotation_and_replay_revokes_session(self):
        original = self.desktop_tokens()
        rotated = self.post("desktop/refresh", {"refreshToken": original["refreshToken"]}).json()
        self.assertNotEqual(original["refreshToken"], rotated["refreshToken"])
        self.assertEqual(
            self.post("desktop/refresh", {"refreshToken": original["refreshToken"]}).status_code,
            401,
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/me", HTTP_AUTHORIZATION="Bearer " + rotated["accessToken"]
            ).status_code,
            401,
        )

    def test_no_device_or_concurrency_limit(self):
        for _ in range(12):
            self.desktop_tokens()
        self.assertEqual(len(self.client.get("/api/v1/sessions").json()["sessions"]), 12)

    def test_jws_signature_bound_to_account_session_and_seven_days(self):
        start_trial(self.account)
        tokens = self.desktop_tokens()
        response = self.client.get(
            "/api/v1/me", HTTP_AUTHORIZATION="Bearer " + tokens["accessToken"]
        ).json()
        header, payload, signature = response["license"].split(".")
        self.key.public_key().verify(
            base64.urlsafe_b64decode(signature + "=="), (header + "." + payload).encode()
        )
        claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
        self.assertEqual(claims["aud"], "relay")
        self.assertEqual(claims["features"], ["history", "compare", "export_bundle"])
        self.assertEqual(claims["sub"], str(self.account.pk))
        self.assertEqual(claims["sid"], tokens["sessionId"])
        self.assertEqual(claims["exp"] - claims["iat"], 604800)
        self.assertEqual(claims["entitlementUntil"], response["entitlement"]["validUntil"])

    def test_jws_never_outlives_entitlement(self):
        self.account.trial_started_at = timezone.now() - timedelta(days=13)
        self.account.trial_ends_at = timezone.now() + timedelta(hours=1)
        self.account.save()
        tokens = self.desktop_tokens()
        response = self.client.get(
            "/api/v1/me", HTTP_AUTHORIZATION="Bearer " + tokens["accessToken"]
        ).json()
        payload = response["license"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
        self.assertEqual(claims["exp"], claims["entitlementUntil"])

    def test_consent_required_versioned_and_recorded_once(self):
        from commerce.models import ConsentRecord

        self.client.logout()
        with patch("commerce.auth.secrets.randbelow", return_value=112233):
            self.post("auth/request-code", {"email": "consent@example.test"})
        payload = {"email": "consent@example.test", "code": "112233"}
        self.assertEqual(
            self.post("auth/verify-code", payload | {"acceptedTerms": False}).status_code, 400
        )
        self.assertEqual(
            self.post("auth/verify-code", payload | {"termsVersion": "old"}).status_code, 409
        )
        self.assertEqual(ConsentRecord.objects.count(), 0)
        self.assertEqual(self.post("auth/verify-code", payload).status_code, 200)
        self.assertEqual(ConsentRecord.objects.count(), 2)
        self.assertSetEqual(
            set(ConsentRecord.objects.values_list("document", flat=True)), {"terms", "privacy"}
        )

    def test_order_requires_current_terms_and_keeps_snapshot(self):
        payload = {
            "planId": "monthly",
            "channel": "simulated",
            "idempotencyKey": "terms-test-order-key",
        }
        self.assertEqual(self.post("orders", payload | {"acceptedTerms": False}).status_code, 400)
        self.assertEqual(self.post("orders", payload | {"termsVersion": "old"}).status_code, 409)
        self.assertEqual(Order.objects.count(), 0)
        response = self.post("orders", payload)
        self.assertEqual(
            Order.objects.get(pk=response.json()["order"]["id"]).terms_version, "2026-09-26"
        )

    def test_price_change_requires_new_confirmation(self):
        payload = {
            "planId": "monthly",
            "channel": "simulated",
            "idempotencyKey": "price-change-order-key",
            "expectedAmount": 800,
            "expectedCurrency": "CNY",
        }
        response = self.post("orders", payload)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "price_changed")
        self.assertFalse(Order.objects.exists())
