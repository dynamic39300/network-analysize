import base64
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.test import TestCase, override_settings
from django.utils import timezone

from commerce.domain import grant_payment
from commerce.models import Account, EntitlementGrant, Order, Refund
from commerce.payments import (
    Alipay,
    PaymentError,
    WeChat,
    money_cents,
    raw_response_value,
    rsa_sign,
)


class PaymentProtocolTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.merchant_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.provider_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private = Path(self.temp.name) / "merchant.pem"
        public = Path(self.temp.name) / "provider.pem"
        private.write_bytes(
            self.merchant_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        public.write_bytes(
            self.provider_key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
        self.wechat = {
            "APP_ID": "wx-synthetic",
            "MCH_ID": "merchant-synthetic",
            "CERT_SERIAL": "merchant-cert",
            "PRIVATE_KEY_FILE": str(private),
            "PUBLIC_KEY_ID": "PUB_KEY_ID_synthetic",
            "PUBLIC_KEY_FILE": str(public),
            "API_V3_KEY": "0" * 32,
        }
        self.alipay = {
            "APP_ID": "alipay-synthetic",
            "SELLER_ID": "seller-synthetic",
            "PRIVATE_KEY_FILE": str(private),
            "PUBLIC_KEY_FILE": str(public),
        }
        self.settings_override = override_settings(
            WECHAT_ENABLED=True, ALIPAY_ENABLED=True, WECHAT=self.wechat, ALIPAY=self.alipay
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.account = Account.objects.create_user(username="buyer", email="buyer@example.test")

    def order(self, channel):
        return Order.objects.create(
            account=self.account,
            idempotency_key="key" + str(Order.objects.count()),
            plan_id="monthly",
            months=1,
            amount=1200,
            channel=channel,
        )

    def wechat_headers(self, body, timestamp=None):
        timestamp = str(timestamp or int(time.time()))
        nonce = "synthetic-nonce"
        signature = rsa_sign(
            self.provider_key, timestamp.encode() + b"\n" + nonce.encode() + b"\n" + body + b"\n"
        )
        return {
            "Wechatpay-Timestamp": timestamp,
            "Wechatpay-Nonce": nonce,
            "Wechatpay-Serial": self.wechat["PUBLIC_KEY_ID"],
            "Wechatpay-Signature": signature,
        }

    def wechat_transaction(self, order):
        return {
            "appid": self.wechat["APP_ID"],
            "mchid": self.wechat["MCH_ID"],
            "out_trade_no": order.pk.hex,
            "trade_state": "SUCCESS",
            "transaction_id": "wx_" + order.pk.hex,
            "success_time": timezone.now().isoformat(),
            "amount": {"total": 1200, "currency": "CNY"},
        }

    def webhook_body(self, payload, event_type="TRANSACTION.SUCCESS"):
        nonce, aad = "123456789012", "transaction"
        encrypted = AESGCM(self.wechat["API_V3_KEY"].encode()).encrypt(
            nonce.encode(), json.dumps(payload).encode(), aad.encode()
        )
        return json.dumps(
            {
                "id": "event",
                "event_type": event_type,
                "resource": {
                    "algorithm": "AEAD_AES_256_GCM",
                    "nonce": nonce,
                    "associated_data": aad,
                    "ciphertext": base64.b64encode(encrypted).decode(),
                },
            }
        ).encode()

    def notify_wechat(self, body, headers=None):
        headers = headers or self.wechat_headers(body)
        return self.client.post(
            "/api/v1/payments/wechat/notify", body, content_type="application/json", headers=headers
        )

    def test_wechat_valid_encrypted_callback_grants_once(self):
        order = self.order("wechat")
        body = self.webhook_body(self.wechat_transaction(order))
        self.assertEqual(self.notify_wechat(body).status_code, 204)
        self.assertEqual(self.notify_wechat(body).status_code, 204)
        self.assertEqual(EntitlementGrant.objects.count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, "paid")

    def test_wechat_tamper_and_expired_signature_never_grant(self):
        order = self.order("wechat")
        body = self.webhook_body(self.wechat_transaction(order))
        headers = self.wechat_headers(body)
        self.assertEqual(self.notify_wechat(body + b" ", headers).status_code, 400)
        self.assertEqual(
            self.notify_wechat(
                body, self.wechat_headers(body, int(time.time()) - 1000)
            ).status_code,
            400,
        )
        headers["Wechatpay-Serial"] = "attacker-key"
        self.assertEqual(self.notify_wechat(body, headers).status_code, 400)
        self.assertFalse(EntitlementGrant.objects.exists())

    def test_wechat_valid_signature_wrong_amount_merchant_or_currency(self):
        order = self.order("wechat")
        for change in [
            {"mchid": "other"},
            {"appid": "other"},
            {"amount": {"total": 1, "currency": "CNY"}},
            {"amount": {"total": 1200, "currency": "USD"}},
        ]:
            payload = self.wechat_transaction(order) | change
            self.assertEqual(self.notify_wechat(self.webhook_body(payload)).status_code, 400)
        self.assertFalse(EntitlementGrant.objects.exists())

    def test_wechat_query_requires_response_signature_and_matches_order(self):
        order = self.order("wechat")
        body = json.dumps(self.wechat_transaction(order)).encode()
        response = httpx.Response(200, content=body, headers=self.wechat_headers(body))
        with patch("commerce.payments.httpx.request", return_value=response):
            self.assertEqual(WeChat().query(order).status, "paid")
        response = httpx.Response(200, content=body, headers={})
        with (
            patch("commerce.payments.httpx.request", return_value=response),
            self.assertRaises(PaymentError),
        ):
            WeChat().query(order)

    def test_wechat_native_request_signing_and_local_qr_url(self):
        order = self.order("wechat")
        body = b'{"code_url":"weixin://wxpay/bizpayurl?pr=synthetic"}'
        response = httpx.Response(200, content=body, headers=self.wechat_headers(body))
        with patch("commerce.payments.httpx.request", return_value=response) as request:
            url = WeChat().create(order)
        self.assertTrue(url.startswith("weixin://wxpay/"))
        submitted = json.loads(request.call_args.kwargs["content"])
        self.assertEqual(submitted["amount"], {"total": 1200, "currency": "CNY"})
        self.assertEqual(submitted["out_trade_no"], order.pk.hex)
        self.assertTrue(
            request.call_args.kwargs["headers"]["Authorization"].startswith(
                "WECHATPAY2-SHA256-RSA2048 "
            )
        )

    def test_wechat_refund_only_after_signed_success(self):
        order = self.order("wechat")
        grant_payment(order.pk, "payment", timezone.now(), 1200)
        refund = Refund.objects.create(order=order, reason="test")
        payload = {
            "mchid": self.wechat["MCH_ID"],
            "out_trade_no": order.pk.hex,
            "out_refund_no": refund.pk.hex,
            "refund_status": "SUCCESS",
            "amount": {"refund": 1200, "total": 1200},
        }
        body = self.webhook_body(payload, "REFUND.SUCCESS")
        self.assertEqual(self.notify_wechat(body).status_code, 204)
        order.refresh_from_db()
        self.assertEqual(order.status, "refunded")
        self.assertIsNotNone(order.grant.revoked_at)

    def alipay_payload(self, order):
        return {
            "app_id": self.alipay["APP_ID"],
            "seller_id": self.alipay["SELLER_ID"],
            "out_trade_no": order.pk.hex,
            "trade_no": "ali_" + order.pk.hex,
            "trade_status": "TRADE_SUCCESS",
            "total_amount": "12.00",
            "sign_type": "RSA2",
            "gmt_payment": "2026-09-25 10:00:00",
        }

    def signed_alipay(self, payload):
        return payload | {
            "sign": rsa_sign(self.provider_key, Alipay.canonical(payload, notification=True))
        }

    def test_alipay_callback_signature_and_replay(self):
        order = self.order("alipay")
        payload = self.signed_alipay(self.alipay_payload(order))
        for _ in range(2):
            response = self.client.post("/api/v1/payments/alipay/notify", payload)
            self.assertEqual(response.content, b"success")
        self.assertEqual(EntitlementGrant.objects.count(), 1)

    def test_alipay_signed_wrong_app_seller_amount_rejected(self):
        order = self.order("alipay")
        for change in [
            {"app_id": "other"},
            {"seller_id": "other"},
            {"total_amount": "0.01"},
            {"sign_type": "RSA"},
        ]:
            payload = self.signed_alipay(self.alipay_payload(order) | change)
            self.assertEqual(
                self.client.post("/api/v1/payments/alipay/notify", payload).status_code, 400
            )
        self.assertFalse(EntitlementGrant.objects.exists())

    def test_alipay_modified_callback_rejected(self):
        order = self.order("alipay")
        payload = self.signed_alipay(self.alipay_payload(order))
        payload["total_amount"] = "100.00"
        self.assertEqual(
            self.client.post("/api/v1/payments/alipay/notify", payload).status_code, 400
        )

    def alipay_response(self, method, payload):
        signed = json.dumps(payload, ensure_ascii=False, indent=1).encode()
        signature = rsa_sign(self.provider_key, signed)
        raw = (
            b'{"'
            + method.replace(".", "_").encode()
            + b'_response":'
            + signed
            + b',"sign":'
            + json.dumps(signature).encode()
            + b"}"
        )
        return httpx.Response(200, content=raw)

    def test_alipay_query_preserves_exact_signed_json(self):
        order = self.order("alipay")
        payload = self.alipay_payload(order) | {"code": "10000"}
        response = self.alipay_response("alipay.trade.query", payload)
        with patch("commerce.payments.httpx.post", return_value=response):
            self.assertEqual(Alipay().query(order).status, "paid")
        response = httpx.Response(200, content=response.content.replace(b"12.00", b"8.00"))
        with (
            patch("commerce.payments.httpx.post", return_value=response),
            self.assertRaises(PaymentError),
        ):
            Alipay().query(order)

    def test_alipay_refund_validates_amount_then_revokes_only_order(self):
        order = self.order("alipay")
        grant_payment(order.pk, "payment", timezone.now(), 1200)
        refund = Refund.objects.create(order=order, reason="test")
        payload = {
            "code": "10000",
            "out_trade_no": order.pk.hex,
            "refund_fee": "12.00",
            "fund_change": "Y",
        }
        with patch(
            "commerce.payments.httpx.post",
            return_value=self.alipay_response("alipay.trade.refund", payload),
        ):
            self.assertEqual(Alipay().refund(refund).status, "refunded")

    def test_raw_json_duplicate_response_is_rejected(self):
        with self.assertRaises(PaymentError):
            raw_response_value(b'{"x":{},"x":{},"sign":"x"}', "x")

    def test_cents_do_not_round_or_accept_nan(self):
        self.assertEqual(money_cents("12.00"), 1200)
        for value in ["NaN", "Infinity", "0.001", "-9.00", None]:
            with self.assertRaises(PaymentError):
                money_cents(value)
