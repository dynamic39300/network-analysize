"""Official WeChat API v3 and Alipay RSA2 adapters. No unverified response grants rights.

Protocol references and deployment limitations are in backend/README.md.
"""

import base64
import json
import secrets
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

import httpx
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .domain import SHANGHAI, confirm_refund, grant_payment
from .errors import APIError
from .models import AuditEvent, Order, Refund


class PaymentError(APIError):
    def __init__(self, message="支付渠道暂时不可用，请稍后同步订单。"):
        super().__init__("payment_unavailable", message, 502)


def private_key(path):
    return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)


def public_key(path):
    return serialization.load_pem_public_key(Path(path).read_bytes())


def rsa_sign(key, message):
    return base64.b64encode(key.sign(message, padding.PKCS1v15(), hashes.SHA256())).decode()


def rsa_verify(key, message, signature):
    try:
        key.verify(
            base64.b64decode(signature, validate=True), message, padding.PKCS1v15(), hashes.SHA256()
        )
    except (InvalidSignature, ValueError, TypeError):
        raise PaymentError("支付签名校验失败，未修改订单。") from None


def money_cents(value):
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount != amount.to_integral_value() or amount <= 0:
            raise ValueError
        return int(amount)
    except (InvalidOperation, ValueError, TypeError):
        raise PaymentError("支付金额格式无效。") from None


def channel_available(channel):
    return {
        "wechat": settings.WECHAT_ENABLED,
        "alipay": settings.ALIPAY_ENABLED,
        "simulated": settings.SIMULATED_PAYMENTS and settings.ENVIRONMENT == "development",
    }.get(channel, False)


def provider(channel):
    if not channel_available(channel):
        raise APIError("channel_unavailable", "此支付方式暂未开通，请选择可用方式。", 503)
    if channel == "wechat":
        return WeChat()
    if channel == "alipay":
        return Alipay()
    raise PaymentError("模拟支付没有外部支付渠道。")


class WeChat:
    host = "https://api.mch.weixin.qq.com"

    def __init__(self):
        self.cfg = settings.WECHAT
        self.private = private_key(self.cfg["PRIVATE_KEY_FILE"])
        self.public = public_key(self.cfg["PUBLIC_KEY_FILE"])

    def verify(self, body, headers):
        timestamp = headers.get("Wechatpay-Timestamp", "")
        nonce = headers.get("Wechatpay-Nonce", "")
        if headers.get("Wechatpay-Serial") != self.cfg["PUBLIC_KEY_ID"]:
            raise PaymentError("微信支付签名公钥不匹配。")
        try:
            if abs(time.time() - int(timestamp)) > 300 or not nonce or len(nonce) > 256:
                raise ValueError
        except (ValueError, TypeError):
            raise PaymentError("微信支付通知已过期或格式无效。") from None
        rsa_verify(
            self.public,
            timestamp.encode() + b"\n" + nonce.encode() + b"\n" + body + b"\n",
            headers.get("Wechatpay-Signature", ""),
        )

    def call(self, method, path, data=None):
        body = (
            json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
            if data is not None
            else b""
        )
        timestamp, nonce = str(int(time.time())), secrets.token_hex(16)
        message = f"{method}\n{path}\n{timestamp}\n{nonce}\n".encode() + body + b"\n"
        signature = rsa_sign(self.private, message)
        auth = f'WECHATPAY2-SHA256-RSA2048 mchid="{self.cfg["MCH_ID"]}",nonce_str="{nonce}",timestamp="{timestamp}",serial_no="{self.cfg["CERT_SERIAL"]}",signature="{signature}"'
        try:
            response = httpx.request(
                method,
                self.host + path,
                content=body,
                headers={
                    "Authorization": auth,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Wechatpay-Serial": self.cfg["PUBLIC_KEY_ID"],
                },
                timeout=10,
                follow_redirects=False,
            )
            if len(response.content) > 131072:
                raise PaymentError()
            self.verify(response.content, response.headers)
            if not response.is_success:
                raise PaymentError()
            return response.json() if response.content else {}
        except (httpx.HTTPError, json.JSONDecodeError):
            raise PaymentError() from None

    def create(self, order):
        data = self.call(
            "POST",
            "/v3/pay/transactions/native",
            {
                "appid": self.cfg["APP_ID"],
                "mchid": self.cfg["MCH_ID"],
                "description": f"NetCare {order.months}个月使用权",
                "out_trade_no": order.pk.hex,
                "notify_url": settings.PUBLIC_URL + "/api/v1/payments/wechat/notify",
                "amount": {"total": order.amount, "currency": "CNY"},
            },
        )
        url = data.get("code_url", "")
        if not url.startswith("weixin://wxpay/") or len(url) > 4096:
            raise PaymentError()
        return url

    def query(self, order):
        data = self.call(
            "GET",
            f"/v3/pay/transactions/out-trade-no/{order.pk.hex}?"
            + urlencode({"mchid": self.cfg["MCH_ID"]}),
        )
        return self.accept_transaction(data, order)

    def accept_transaction(self, data, order=None):
        if data.get("appid") != self.cfg["APP_ID"] or data.get("mchid") != self.cfg["MCH_ID"]:
            raise PaymentError("支付商户或应用不匹配。")
        number = data.get("out_trade_no", "")
        if order is None:
            order = Order.objects.filter(pk=number, channel="wechat").first()
        if not order or order.pk.hex != number or order.channel != "wechat":
            raise PaymentError("支付订单不匹配。")
        if data.get("trade_state") == "SUCCESS":
            paid_at = parse_datetime(data.get("success_time", ""))
            if not paid_at or timezone.is_naive(paid_at):
                raise PaymentError("支付时间无效。")
            amount = data.get("amount", {})
            return grant_payment(
                order.pk,
                data.get("transaction_id"),
                paid_at,
                amount.get("total"),
                amount.get("currency"),
            )
        if data.get("trade_state") in {"CLOSED", "REVOKED", "PAYERROR"}:
            Order.objects.filter(pk=order.pk, status="pending").update(status="closed")
        order.refresh_from_db()
        return order

    def notification(self, body, headers):
        self.verify(body, headers)
        try:
            event = json.loads(body)
            resource = event["resource"]
            if resource["algorithm"] != "AEAD_AES_256_GCM":
                raise ValueError
            payload = AESGCM(self.cfg["API_V3_KEY"].encode()).decrypt(
                resource["nonce"].encode(),
                base64.b64decode(resource["ciphertext"], validate=True),
                resource.get("associated_data", "").encode(),
            )
            data = json.loads(payload)
            if event["event_type"] == "TRANSACTION.SUCCESS":
                self.accept_transaction(data)
            elif event["event_type"] == "REFUND.SUCCESS":
                refund = (
                    Refund.objects.select_related("order")
                    .filter(pk=data.get("out_refund_no"), order__channel="wechat")
                    .first()
                )
                if not refund or data.get("mchid") != self.cfg["MCH_ID"]:
                    raise ValueError
                self.accept_refund(data, refund)
            else:
                raise ValueError
        except (KeyError, ValueError, TypeError, InvalidTag):
            raise PaymentError("支付通知格式无效。") from None

    def refund(self, refund):
        order = refund.order
        data = self.call(
            "POST",
            "/v3/refund/domestic/refunds",
            {
                "out_trade_no": order.pk.hex,
                "out_refund_no": refund.pk.hex,
                "reason": "用户申请退款",
                "notify_url": settings.PUBLIC_URL + "/api/v1/payments/wechat/notify",
                "amount": {"refund": order.amount, "total": order.amount, "currency": "CNY"},
            },
        )
        return self.accept_refund(data, refund)

    def query_refund(self, refund):
        return self.accept_refund(
            self.call("GET", "/v3/refund/domestic/refunds/" + refund.pk.hex), refund
        )

    def accept_refund(self, data, refund):
        order = refund.order
        amount = data.get("amount", {})
        if (
            data.get("out_trade_no") != order.pk.hex
            or data.get("out_refund_no") != refund.pk.hex
            or amount.get("refund") != order.amount
            or amount.get("total") != order.amount
        ):
            raise PaymentError("退款核验信息不一致。")
        status = data.get("status", data.get("refund_status"))
        if status == "SUCCESS":
            return confirm_refund(refund.pk)
        if status in {"CLOSED", "ABNORMAL"}:
            Refund.objects.filter(pk=refund.pk).exclude(status="refunded").update(status="failed")
        return refund


def raw_response_value(raw, name):
    """Locate exact response JSON bytes for RSA2; reserialization changes signed bytes."""
    decoder, index, values = json.JSONDecoder(), 0, {}
    text = raw.decode("utf-8")
    index = len(text) - len(text.lstrip())
    if text[index : index + 1] != "{":
        raise PaymentError()
    index += 1
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if text[index : index + 1] == "}":
            break
        key, index = decoder.raw_decode(text, index)
        if key in values or not isinstance(key, str):
            raise PaymentError()
        while index < len(text) and text[index].isspace():
            index += 1
        if text[index : index + 1] != ":":
            raise PaymentError()
        index += 1
        while index < len(text) and text[index].isspace():
            index += 1
        start = index
        value, index = decoder.raw_decode(text, index)
        values[key] = (value, text[start:index].encode())
        while index < len(text) and text[index].isspace():
            index += 1
        if text[index : index + 1] == "}":
            break
        if text[index : index + 1] != ",":
            raise PaymentError()
        index += 1
    if name not in values or "sign" not in values:
        raise PaymentError()
    return values[name][0], values[name][1], values["sign"][0]


class Alipay:
    gateway = "https://openapi.alipay.com/gateway.do"

    def __init__(self):
        self.cfg = settings.ALIPAY
        self.private = private_key(self.cfg["PRIVATE_KEY_FILE"])
        self.public = public_key(self.cfg["PUBLIC_KEY_FILE"])

    @staticmethod
    def canonical(params, notification=False):
        excluded = {"sign", "sign_type"} if notification else {"sign"}
        return "&".join(
            f"{key}={value}"
            for key, value in sorted(params.items())
            if key not in excluded and value not in {"", None}
        ).encode()

    def params(self, method, biz):
        params = {
            "app_id": self.cfg["APP_ID"],
            "method": method,
            "format": "JSON",
            "charset": "utf-8",
            "sign_type": "RSA2",
            "timestamp": timezone.now().astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
            "version": "1.0",
            "biz_content": json.dumps(biz, ensure_ascii=False, separators=(",", ":")),
        }
        return params

    def create(self, order):
        params = self.params(
            "alipay.trade.page.pay",
            {
                "out_trade_no": order.pk.hex,
                "product_code": "FAST_INSTANT_TRADE_PAY",
                "total_amount": f"{order.amount / 100:.2f}",
                "subject": f"NetCare {order.months}个月使用权",
                "timeout_express": "30m",
            },
        )
        params.update(
            {
                "notify_url": settings.PUBLIC_URL + "/api/v1/payments/alipay/notify",
                "return_url": settings.PUBLIC_URL + "/orders/",
            }
        )
        params["sign"] = rsa_sign(self.private, self.canonical(params))
        return self.gateway + "?" + urlencode(params)

    def call(self, method, biz):
        params = self.params(method, biz)
        params["sign"] = rsa_sign(self.private, self.canonical(params))
        try:
            response = httpx.post(self.gateway, data=params, timeout=10, follow_redirects=False)
            if not response.is_success or len(response.content) > 131072:
                raise PaymentError()
            data, signed, signature = raw_response_value(
                response.content, method.replace(".", "_") + "_response"
            )
            rsa_verify(self.public, signed, signature)
            if data.get("code") != "10000":
                raise PaymentError()
            return data
        except (httpx.HTTPError, ValueError, IndexError, KeyError):
            raise PaymentError() from None

    def query(self, order):
        return self.accept_transaction(
            self.call("alipay.trade.query", {"out_trade_no": order.pk.hex}), order
        )

    def accept_transaction(self, data, order):
        if data.get("out_trade_no") != order.pk.hex or order.channel != "alipay":
            raise PaymentError("支付订单不匹配。")
        if data.get("seller_id") and data["seller_id"] != self.cfg["SELLER_ID"]:
            raise PaymentError("支付商户不匹配。")
        if data.get("trade_status") in {"TRADE_SUCCESS", "TRADE_FINISHED"}:
            raw_time = data.get("gmt_payment", data.get("send_pay_date"))
            paid_at = timezone.now()
            if raw_time:
                paid_at = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
            return grant_payment(
                order.pk, data.get("trade_no"), paid_at, money_cents(data.get("total_amount"))
            )
        if data.get("trade_status") == "TRADE_CLOSED":
            Order.objects.filter(pk=order.pk, status="pending").update(status="closed")
        order.refresh_from_db()
        return order

    def notification(self, params):
        if params.get("sign_type") != "RSA2":
            raise PaymentError("支付宝签名算法无效。")
        rsa_verify(self.public, self.canonical(params, notification=True), params.get("sign", ""))
        if (
            params.get("app_id") != self.cfg["APP_ID"]
            or params.get("seller_id") != self.cfg["SELLER_ID"]
        ):
            raise PaymentError("支付商户或应用不匹配。")
        order = Order.objects.filter(pk=params.get("out_trade_no"), channel="alipay").first()
        if not order:
            raise PaymentError("支付订单不匹配。")
        return self.accept_transaction(params, order)

    def refund(self, refund):
        data = self.call(
            "alipay.trade.refund",
            {
                "out_trade_no": refund.order.pk.hex,
                "out_request_no": refund.pk.hex,
                "refund_amount": f"{refund.order.amount / 100:.2f}",
                "refund_reason": "用户申请退款",
            },
        )
        if (
            data.get("out_trade_no") != refund.order.pk.hex
            or money_cents(data.get("refund_fee")) != refund.order.amount
        ):
            raise PaymentError("退款核验信息不一致。")
        return confirm_refund(refund.pk)

    def query_refund(self, refund):
        data = self.call(
            "alipay.trade.fastpay.refund.query",
            {
                "out_trade_no": refund.order.pk.hex,
                "out_request_no": refund.pk.hex,
                "query_options": ["refund_detail_item_list"],
            },
        )
        if (
            data.get("out_trade_no") != refund.order.pk.hex
            or data.get("out_request_no") != refund.pk.hex
            or money_cents(data.get("refund_amount")) != refund.order.amount
        ):
            raise PaymentError("退款核验信息不一致。")
        if data.get("refund_status") == "REFUND_SUCCESS":
            return confirm_refund(refund.pk)
        return refund


def synchronize_order(order):
    if order.status != "pending" or order.channel == "simulated":
        return order
    return provider(order.channel).query(order)


def process_refund(refund, actor):
    with transaction.atomic():
        refund = Refund.objects.select_for_update().select_related("order").get(pk=refund.pk)
        if refund.status not in {"requested", "processing", "failed"}:
            raise APIError("refund_state", "退款申请当前状态不可处理。", 409)
        was_requested = refund.status == "requested"
        refund.status = "processing"
        refund.save(update_fields=["status", "updated_at"])
    AuditEvent.objects.create(
        actor=actor,
        action="refund_submitted",
        target=str(refund.pk),
        detail={"channel": refund.order.channel},
    )
    if refund.order.channel == "simulated" and channel_available("simulated"):
        return confirm_refund(refund.pk)
    adapter = provider(refund.order.channel)
    # Retry always uses the same merchant refund ID; never creates a second refund.
    if not was_requested:
        try:
            adapter.query_refund(refund)
            refund.refresh_from_db()
            if refund.status == "refunded":
                return refund
        except PaymentError:
            pass
    return adapter.refund(refund)
