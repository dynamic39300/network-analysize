import json
from functools import wraps

from django.conf import settings
from django.contrib.auth import logout
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

from . import auth, payments
from .domain import (
    FREE_FEATURES,
    PLANS,
    PRO_FEATURES,
    grant_payment,
    renewal_terms,
    stamp,
    start_trial,
)
from .errors import APIError
from .models import (
    Account,
    AuditEvent,
    DesktopAuthorization,
    DesktopSession,
    EntitlementGrant,
    Order,
    Refund,
    Release,
    SupportMessage,
    SupportTicket,
)


def api(methods, authenticated=True, web_only=False):
    def decorator(func):
        @wraps(func)
        def wrapped(request, *args, **kwargs):
            try:
                if request.method not in methods:
                    raise APIError("method_not_allowed", "不支持此请求方法。", 405)
                session = getattr(request, "desktop_session", None)
                if web_only and session:
                    raise APIError("web_session_required", "请在浏览器中登录并确认。", 401)
                request.account = session.account if session else request.user
                if authenticated and (
                    not request.account.is_authenticated or not request.account.is_active
                ):
                    raise APIError("login_required", "请先登录。", 401)
                if request.method == "POST":
                    if len(request.body) > 65536:
                        raise APIError("body_too_large", "请求内容过长。", 413)
                    if request.content_type != "application/json":
                        raise APIError("invalid_content_type", "请使用 JSON 请求。", 415)
                    request.data = json.loads(request.body or b"{}")
                    if not isinstance(request.data, dict):
                        raise APIError("invalid_input", "请求格式无效。")
                result = func(request, *args, **kwargs)
                return result if isinstance(result, HttpResponse) else JsonResponse(result)
            except APIError as error:
                return JsonResponse(
                    {"error": {"code": error.code, "message": error.message}}, status=error.status
                )
            except (ValidationError, json.JSONDecodeError, ValueError, TypeError):
                return JsonResponse(
                    {"error": {"code": "invalid_input", "message": "请求内容格式无效。"}},
                    status=400,
                )

        return wrapped

    return decorator


def text(data, key, maximum=256, minimum=1):
    value = data.get(key)
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise APIError("invalid_input", "请检查填写内容。")
    return value.strip()


def email(data):
    value = text(data, "email", 254).lower()
    validate_email(value)
    return value


def require_agreement(data, include_privacy=False):
    if data.get("acceptedTerms") is not True:
        raise APIError("agreement_required", "请阅读并同意当前用户协议与隐私政策。")
    if data.get("termsVersion") != settings.TERMS_VERSION or (
        include_privacy and data.get("privacyVersion") != settings.PRIVACY_VERSION
    ):
        raise APIError("terms_updated", "协议版本已更新，请刷新页面阅读并重新同意。", 409)


def csrf_failure(request, reason=""):
    return JsonResponse(
        {"error": {"code": "csrf_failed", "message": "页面验证已过期，请刷新后重试。"}}, status=403
    )


@ensure_csrf_cookie
def page(request):
    return render(
        request,
        "index.html",
        {
            "environment": settings.ENVIRONMENT,
            "legal_entity_name": settings.LEGAL_ENTITY_NAME,
            "support_email": settings.SUPPORT_EMAIL,
        },
    )


def latest_release():
    return (
        Release.objects.filter(
            published=True, verified_signed_notarized=True, url__startswith="https://"
        )
        .order_by("-created_at")
        .first()
    )


@api(["GET"], authenticated=False)
def config(request):
    release = latest_release()
    return {
        "productName": "Relay",
        "termsVersion": settings.TERMS_VERSION,
        "privacyVersion": settings.PRIVACY_VERSION,
        "environment": settings.ENVIRONMENT,
        "plans": list(PLANS.values()),
        "freeFeatures": list(FREE_FEATURES),
        "proFeatures": list(PRO_FEATURES),
        "payments": {
            key: payments.channel_available(key) for key in ["wechat", "alipay", "simulated"]
        },
        "download": {
            "available": bool(release),
            "url": release.url if release else None,
            "version": release.version if release else None,
            "minimumOS": release.minimum_os if release else "12.0",
            "architecture": release.architecture if release else "arm64",
        },
        "supportEmail": settings.SUPPORT_EMAIL or None,
        "legalEntityName": settings.LEGAL_ENTITY_NAME or None,
    }


@api(["POST"], authenticated=False, web_only=True)
def request_code(request):
    auth.send_code(
        email(request.data), getattr(request, "client_ip", request.META.get("REMOTE_ADDR", ""))
    )
    return {"ok": True}


@api(["POST"], authenticated=False, web_only=True)
def verify_code(request):
    require_agreement(request.data, include_privacy=True)
    account = auth.verify_code(
        request,
        email(request.data),
        text(request.data, "code", 6, 6),
        {"terms": settings.TERMS_VERSION, "privacy": settings.PRIVACY_VERSION},
    )
    return auth.me(account)


@api(["POST"], web_only=True)
def web_logout(request):
    logout(request)
    return {"ok": True}


@api(["GET"])
def me(request):
    return auth.me(request.account, request.desktop_session)


@api(["POST"])
def trial_start(request):
    account = start_trial(request.account)
    return auth.me(account, request.desktop_session)


@api(["GET"])
def sessions(request):
    return {
        "sessions": [
            {
                "id": str(s.pk),
                "label": s.label,
                "createdAt": stamp(s.created_at),
                "lastSeenAt": stamp(s.last_seen_at),
                "current": bool(request.desktop_session and request.desktop_session.pk == s.pk),
            }
            for s in DesktopSession.objects.filter(
                account=request.account, revoked=False, refresh_expires_at__gt=timezone.now()
            ).order_by("-last_seen_at")[:200]
        ]
    }


@api(["POST"])
def revoke_session(request, session_id):
    count = DesktopSession.objects.filter(pk=session_id, account=request.account).update(
        revoked=True
    )
    if not count:
        raise APIError("not_found", "没有找到此登录会话。", 404)
    AuditEvent.objects.create(
        actor=request.account, action="session_revoked", target=str(session_id)
    )
    return {"ok": True}


@csrf_exempt
@api(["POST"], authenticated=False)
def desktop_start(request):
    auth.rate_limit(
        "desktop-start:" + getattr(request, "client_ip", request.META.get("REMOTE_ADDR", "")),
        60,
        3600,
    )
    return auth.desktop_start(
        text(request.data, "codeChallenge", 43, 43),
        text(request.data, "state", 256, 32),
        text(request.data, "deviceName", 80),
    )


@api(["GET"], web_only=True)
def desktop_request(request):
    obj = DesktopAuthorization.objects.filter(
        pk=request.GET.get("request"), expires_at__gt=timezone.now(), consumed=False, account=None
    ).first()
    if not obj:
        raise APIError("invalid_authorization", "授权请求已过期或已使用，请回 App 重试。", 404)
    return {"deviceName": obj.label, "expiresAt": stamp(obj.expires_at)}


@api(["POST"], web_only=True)
def desktop_approve(request):
    auth.approve_request(text(request.data, "request", 36, 36), request.account)
    return {"approved": True}


@csrf_exempt
@api(["POST"], authenticated=False)
def desktop_poll(request):
    auth.rate_limit(
        "desktop-poll:" + getattr(request, "client_ip", request.META.get("REMOTE_ADDR", "")),
        600,
        3600,
    )
    result = auth.poll(
        text(request.data, "requestId", 36, 36),
        text(request.data, "codeVerifier", 128, 43),
        text(request.data, "state", 256, 32),
    )
    if result is None:
        return JsonResponse({"status": "pending"}, status=202)
    return result


@csrf_exempt
@api(["POST"], authenticated=False)
def desktop_refresh(request):
    auth.rate_limit(
        "desktop-refresh:" + getattr(request, "client_ip", request.META.get("REMOTE_ADDR", "")),
        240,
        3600,
    )
    return auth.refresh(text(request.data, "refreshToken", 128))


@api(["POST"])
def desktop_logout(request):
    if not request.desktop_session:
        raise APIError("desktop_session_required", "此操作需要 App 登录。", 401)
    DesktopSession.objects.filter(pk=request.desktop_session.pk).update(revoked=True)
    return {"ok": True}


def order_data(order):
    grant = EntitlementGrant.objects.filter(order=order).first()
    refund = Refund.objects.filter(order=order).first()
    return {
        "id": str(order.pk),
        "planId": order.plan_id,
        "amount": order.amount,
        "currency": order.currency,
        "channel": order.channel,
        "status": order.status,
        "createdAt": stamp(order.created_at),
        "paidAt": stamp(order.paid_at),
        "startsAt": stamp(grant.starts_at) if grant else None,
        "endsAt": stamp(grant.ends_at) if grant else None,
        "refundStatus": refund.status if refund else None,
    }


def payment_data(order):
    return {
        "kind": {"wechat": "qr", "alipay": "redirect", "simulated": "simulated"}[order.channel],
        "url": order.payment_url or None,
    }


def owned_order(request, order_id):
    order = Order.objects.filter(pk=order_id, account=request.account).first()
    if not order:
        raise APIError("not_found", "没有找到此订单。", 404)
    return order


@api(["GET"])
def quote(request):
    plan = PLANS.get(request.GET.get("planId"))
    if not plan:
        raise APIError("invalid_plan", "请选择有效套餐。")
    start, end, _, _ = renewal_terms(request.account, plan["months"])
    return {
        "planId": plan["id"],
        "amount": plan["amount"],
        "currency": "CNY",
        "startsAt": stamp(start),
        "endsAt": stamp(end),
    }


@api(["GET", "POST"])
def orders(request):
    if request.method == "GET":
        return {
            "orders": [
                order_data(o)
                for o in Order.objects.filter(account=request.account).order_by("-created_at")[:100]
            ]
        }
    require_agreement(request.data)
    plan = PLANS.get(text(request.data, "planId", 20))
    channel = text(request.data, "channel", 20)
    key = text(request.data, "idempotencyKey", 80, 16)
    if not plan:
        raise APIError("invalid_plan", "请选择有效套餐。")
    expected = request.data.get("expectedAmount")
    if (
        type(expected) is not int
        or expected != plan["amount"]
        or request.data.get("expectedCurrency") != plan["currency"]
    ):
        raise APIError("price_changed", "套餐价格已更新，请刷新并重新确认后购买。", 409)
    if not payments.channel_available(channel):
        raise APIError("channel_unavailable", "此支付方式暂未开通。", 503)
    auth.rate_limit("orders:" + str(request.account.pk), 30, 3600)
    with transaction.atomic():
        Account.objects.select_for_update().get(pk=request.account.pk)
        order, _ = Order.objects.get_or_create(
            account=request.account,
            idempotency_key=key,
            defaults={
                "plan_id": plan["id"],
                "terms_version": settings.TERMS_VERSION,
                "months": plan["months"],
                "amount": plan["amount"],
                "channel": channel,
            },
        )
        if order.plan_id != plan["id"] or order.channel != channel:
            raise APIError("idempotency_conflict", "此请求已用于另一个订单，请重新发起购买。", 409)
    if order.status == "pending" and not order.payment_url and channel != "simulated":
        url = payments.provider(channel).create(order)
        Order.objects.filter(pk=order.pk).update(payment_url=url)
        order.payment_url = url
    return {"order": order_data(order), "payment": payment_data(order)}


@api(["GET"])
def order_detail(request, order_id):
    order = owned_order(request, order_id)
    return {"order": order_data(order), "payment": payment_data(order)}


@api(["POST"])
def order_sync(request, order_id):
    order = owned_order(request, order_id)
    auth.rate_limit("sync:" + str(order.pk), 12, 60)
    order = payments.synchronize_order(order)
    return {"order": order_data(order), "payment": payment_data(order)}


@api(["POST"])
def order_simulate(request, order_id):
    if not payments.channel_available("simulated"):
        raise APIError("not_found", "没有找到此功能。", 404)
    order = owned_order(request, order_id)
    if order.channel != "simulated":
        raise APIError("invalid_channel", "真实支付订单不能模拟付款。", 409)
    order = grant_payment(order.pk, "sim_" + order.pk.hex, timezone.now(), order.amount)
    return {"order": order_data(order), "payment": payment_data(order)}


@api(["POST"])
def request_refund(request, order_id):
    order = owned_order(request, order_id)
    reason = text(request.data, "reason", 2000, 5)
    if order.status not in {"paid", "refunded"}:
        raise APIError("refund_unavailable", "订单尚未付款，无法申请退款。", 409)
    auth.rate_limit("refund:" + str(request.account.pk), 20, 3600)
    refund, created = Refund.objects.get_or_create(order=order, defaults={"reason": reason})
    if created:
        AuditEvent.objects.create(
            actor=request.account, action="refund_requested", target=str(refund.pk)
        )
    return {"refund": {"id": str(refund.pk), "status": refund.status}}


def ticket_data(ticket):
    return {
        "id": str(ticket.pk),
        "subject": ticket.subject,
        "status": ticket.status,
        "createdAt": stamp(ticket.created_at),
        "messages": [
            {"body": m.body, "fromSupport": m.from_support, "createdAt": stamp(m.created_at)}
            for m in ticket.messages.order_by("created_at")[:200]
        ],
    }


@api(["GET", "POST"])
def support(request):
    if request.method == "GET":
        return {
            "tickets": [
                ticket_data(t)
                for t in SupportTicket.objects.filter(account=request.account).order_by(
                    "-created_at"
                )[:50]
            ]
        }
    auth.rate_limit("support:" + str(request.account.pk), 10, 3600)
    subject, body = text(request.data, "subject", 160), text(request.data, "body", 5000)
    order = owned_order(request, request.data["orderId"]) if request.data.get("orderId") else None
    with transaction.atomic():
        ticket = SupportTicket.objects.create(account=request.account, subject=subject, order=order)
        SupportMessage.objects.create(ticket=ticket, body=body)
    return {"ticket": ticket_data(ticket)}


@api(["POST"])
def support_message(request, ticket_id):
    ticket = SupportTicket.objects.filter(pk=ticket_id, account=request.account).first()
    if not ticket:
        raise APIError("not_found", "没有找到此工单。", 404)
    auth.rate_limit("support-messages:" + str(request.account.pk), 30, 3600)
    SupportMessage.objects.create(ticket=ticket, body=text(request.data, "body", 5000))
    SupportTicket.objects.filter(pk=ticket.pk).update(status="open")
    return {"ok": True}


@api(["GET"], authenticated=False)
def latest(request):
    release = latest_release()
    return {
        "available": bool(release),
        "version": release.version if release else None,
        "url": release.url if release else None,
        "notes": release.notes if release else "正式签名与公证安装包尚未发布。",
        "minimumOS": release.minimum_os if release else "12.0",
        "sha256": release.sha256 if release else None,
        "architecture": release.architecture if release else "arm64",
    }


@csrf_exempt
def wechat_notify(request):
    if request.method != "POST" or not payments.channel_available("wechat"):
        return JsonResponse({"code": "FAIL", "message": "unavailable"}, status=404)
    try:
        payments.WeChat().notification(request.body, request.headers)
        return HttpResponse(status=204)
    except (APIError, ValidationError, ValueError, IntegrityError):
        return JsonResponse({"code": "FAIL", "message": "verification failed"}, status=400)


@csrf_exempt
def alipay_notify(request):
    if request.method != "POST" or not payments.channel_available("alipay"):
        return HttpResponse("failure", status=404)
    try:
        if any(len(values) != 1 for _, values in request.POST.lists()):
            raise ValueError
        payments.Alipay().notification(request.POST.dict())
        return HttpResponse("success", content_type="text/plain")
    except (APIError, ValidationError, ValueError, IntegrityError):
        return HttpResponse("failure", status=400, content_type="text/plain")


def health(request):
    return JsonResponse({"status": "ok"})


def ready(request):
    from django.db import connection

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        if settings.PRODUCTION:
            auth.signing_key()
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ready", "environment": settings.ENVIRONMENT})
