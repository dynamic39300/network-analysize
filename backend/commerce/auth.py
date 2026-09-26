import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.conf import settings
from django.contrib.auth import login
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .domain import OFFLINE_LICENSE_SECONDS, PRO_FEATURES, entitlement
from .errors import APIError
from .models import (
    Account,
    AuditEvent,
    ConsentRecord,
    DesktopAuthorization,
    DesktopSession,
    EmailChallenge,
    RateBucket,
    UsedRefreshToken,
)


def digest(value):
    return hmac.new(settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def b64url(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def rate_limit(key, limit, seconds):
    now = timezone.now()
    exceeded = False
    with transaction.atomic():
        bucket, _ = RateBucket.objects.get_or_create(key=digest(key), defaults={"starts_at": now})
        bucket = RateBucket.objects.select_for_update().get(pk=bucket.pk)
        if bucket.starts_at + timedelta(seconds=seconds) <= now:
            bucket.starts_at, bucket.count = now, 0
        bucket.count += 1
        exceeded = bucket.count > limit
        bucket.save()
    if exceeded:
        raise APIError("rate_limited", "操作过于频繁，请稍后再试。", 429)


def send_code(email, ip):
    rate_limit("email-ip:" + ip, 30, 3600)
    rate_limit("email-minute:" + email, 1, 60)
    rate_limit("email-hour:" + email, 10, 3600)
    code = f"{secrets.randbelow(1000000):06}"
    now = timezone.now()
    EmailChallenge.objects.update_or_create(
        email=email,
        defaults={
            "digest": digest(email + ":" + code),
            "expires_at": now + timedelta(minutes=10),
            "attempts": 0,
            "consumed": False,
            "created_at": now,
        },
    )
    try:
        sent = send_mail(
            "Relay 登录验证码",
            f"你的登录验证码是 {code}，10 分钟内有效。请勿将验证码交给任何人。若非本人操作，请忽略本邮件。",
            settings.DEFAULT_FROM_EMAIL,
            [email],
            fail_silently=False,
        )
        if sent != 1:
            raise RuntimeError("Mail delivery was not accepted")
    except Exception:
        EmailChallenge.objects.filter(email=email).update(consumed=True)
        raise APIError("email_unavailable", "邮件暂时无法发送，请稍后重试。", 503) from None


def verify_code(request, email, code, consent_versions=None):
    rate_limit(
        "verify-ip:" + getattr(request, "client_ip", request.META.get("REMOTE_ADDR", "")), 60, 3600
    )
    account = None
    with transaction.atomic():
        challenge = EmailChallenge.objects.select_for_update().filter(email=email).first()
        if (
            challenge
            and not challenge.consumed
            and challenge.expires_at > timezone.now()
            and challenge.attempts < 5
        ):
            challenge.attempts += 1
            if hmac.compare_digest(challenge.digest, digest(email + ":" + code)):
                challenge.consumed = True
                account = Account.objects.filter(email=email).first()
                if not account:
                    account = Account.objects.create_user(
                        username=secrets.token_hex(16), email=email, password=None
                    )
                if (
                    account.is_active
                    and not account.is_staff
                    and not account.is_superuser
                    and consent_versions
                ):
                    for document, version in consent_versions.items():
                        ConsentRecord.objects.get_or_create(
                            account=account, document=document, version=version
                        )
            challenge.save(update_fields=["attempts", "consumed"])
    if not account or not account.is_active or account.is_staff or account.is_superuser:
        raise APIError("invalid_code", "验证码无效或已过期，请重新获取。", 401)
    login(request, account, backend="commerce.backends.EmailCodeBackend")
    return account


def desktop_start(challenge, state, label):
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge) or not re.fullmatch(
        r"[A-Za-z0-9_-]{32,256}", state
    ):
        raise APIError("invalid_pkce", "登录请求格式无效。")
    obj = DesktopAuthorization.objects.create(
        challenge=challenge,
        state=state,
        label=label,
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    return {
        "authorizeUrl": settings.PUBLIC_URL
        + "/desktop/authorize/?"
        + urlencode({"request": str(obj.pk)}),
        "requestId": str(obj.pk),
        "expiresIn": 300,
        "pollInterval": 3,
    }


@transaction.atomic
def approve_request(request_id, account):
    """Bind once to the authenticated browser account; never expose desktop credentials."""
    obj = DesktopAuthorization.objects.select_for_update().filter(pk=request_id).first()
    if (
        not obj
        or obj.expires_at <= timezone.now()
        or obj.account_id
        or obj.consumed
        or obj.attempts >= 5
        or not account.is_active
        or account.is_staff
        or account.is_superuser
    ):
        raise APIError("invalid_authorization", "授权请求已过期或已使用，请回 App 重试。", 409)
    obj.account = account
    obj.save(update_fields=["account"])
    AuditEvent.objects.create(actor=account, action="desktop_approved", target=str(obj.pk))


def tokens_for(session):
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    now = timezone.now()
    session.access_hash, session.refresh_hash = digest(access), digest(refresh)
    session.access_expires_at, session.refresh_expires_at = (
        now + timedelta(hours=1),
        now + timedelta(days=90),
    )
    session.last_seen_at = now
    session.save()
    return {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresIn": 3600,
        "sessionId": str(session.pk),
        "account": {"id": str(session.account_id), "email": session.account.email},
    }


def poll(request_id, verifier, state):
    """Proof-bound polling. Failed attempts commit; successful authorization is consumed once.

    A request ID alone is never a credential. Valid pending polls do not consume the
    five-error budget and must be at least three seconds apart. Row locking gives
    concurrent successful pollers exactly one session.
    """
    if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) or not re.fullmatch(
        r"[A-Za-z0-9_-]{32,256}", state
    ):
        raise APIError("invalid_pkce", "登录验证失败，请重新登录。", 401)
    response, error = None, None
    with transaction.atomic():
        obj = (
            DesktopAuthorization.objects.select_for_update(of=("self",))
            .select_related("account")
            .filter(pk=request_id)
            .first()
        )
        now = timezone.now()
        if (
            not obj
            or obj.consumed
            or obj.expires_at <= now
            or obj.attempts >= 5
            or (
                obj.account
                and (not obj.account.is_active or obj.account.is_staff or obj.account.is_superuser)
            )
        ):
            error = APIError("invalid_authorization", "登录请求无效、已过期或已使用。", 401)
        else:
            valid_challenge = hmac.compare_digest(
                obj.challenge, b64url(hashlib.sha256(verifier.encode()).digest())
            )
            valid_state = hmac.compare_digest(obj.state, state)
            if not (valid_challenge and valid_state):
                obj.attempts += 1
                obj.save(update_fields=["attempts"])
                error = APIError("invalid_authorization", "登录验证失败，请重新登录。", 401)
            elif obj.last_polled_at and now < obj.last_polled_at + timedelta(seconds=3):
                error = APIError("rate_limited", "请等待 3 秒后再次检查授权。", 429)
            else:
                obj.last_polled_at = now
                if obj.account:
                    obj.consumed = True
                    session = DesktopSession(account=obj.account, label=obj.label)
                    response = tokens_for(session)
                    AuditEvent.objects.create(
                        actor=obj.account, action="desktop_authorized", target=str(session.pk)
                    )
                obj.save(update_fields=["last_polled_at", "consumed"])
    if error:
        raise error
    return response


def refresh(token):
    response = None
    with transaction.atomic():
        hashed = digest(token)
        session = (
            DesktopSession.objects.select_for_update()
            .select_related("account")
            .filter(refresh_hash=hashed)
            .first()
        )
        if (
            session
            and not session.revoked
            and session.refresh_expires_at > timezone.now()
            and session.account.is_active
            and not session.account.is_staff
            and not session.account.is_superuser
        ):
            UsedRefreshToken.objects.create(
                digest=hashed, session=session, expires_at=session.refresh_expires_at
            )
            response = tokens_for(session)
        else:
            old = UsedRefreshToken.objects.filter(digest=hashed).first()
            if old:
                DesktopSession.objects.filter(pk=old.session_id).update(revoked=True)
    if not response:
        raise APIError("invalid_session", "登录已失效，请重新登录。", 401)
    return response


def authenticate_bearer(request):
    raw = request.headers.get("Authorization", "")
    if not raw.startswith("Bearer ") or len(raw) > 256:
        raise APIError("invalid_session", "登录已失效，请重新登录。", 401)
    session = (
        DesktopSession.objects.select_related("account")
        .filter(
            access_hash=digest(raw[7:]),
            revoked=False,
            access_expires_at__gt=timezone.now(),
            account__is_active=True,
            account__is_staff=False,
            account__is_superuser=False,
        )
        .first()
    )
    if not session:
        raise APIError("invalid_session", "登录已失效，请重新登录。", 401)
    if session.last_seen_at < timezone.now() - timedelta(minutes=5):
        DesktopSession.objects.filter(pk=session.pk).update(last_seen_at=timezone.now())
    return session


def signing_key():
    path = Path(settings.LICENSE_PRIVATE_KEY_FILE)
    if not path.is_file():
        raise APIError("license_unavailable", "授权签发暂未配置，请联系支持。", 503)
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        raise APIError("license_unavailable", "授权签发配置无效。", 503) from None
    if not isinstance(key, Ed25519PrivateKey):
        raise APIError("license_unavailable", "授权签发配置无效。", 503)
    return key


def issue_license(account, session, ent):
    if not session or not ent["validUntil"] or ent["status"] not in {"trial", "paid"}:
        return None
    now = int(timezone.now().timestamp())
    payload = {
        "iss": settings.PUBLIC_URL,
        "aud": "relay",
        "sub": str(account.pk),
        "sid": str(session.pk),
        "iat": now,
        "nbf": now,
        "exp": min(ent["validUntil"], now + OFFLINE_LICENSE_SECONDS),
        "entitlementUntil": ent["validUntil"],
        "status": ent["status"],
        "features": list(PRO_FEATURES),
    }
    header = {"alg": "EdDSA", "typ": "JWT", "kid": "license-v1"}
    message = ".".join(
        b64url(json.dumps(obj, separators=(",", ":")).encode()) for obj in [header, payload]
    )
    return message + "." + b64url(signing_key().sign(message.encode()))


def me(account, session=None):
    account.refresh_from_db()
    ent = entitlement(account)
    return {
        "account": {"id": str(account.pk), "email": account.email},
        "entitlement": ent,
        "license": issue_license(account, session, ent),
    }
