"""Calendar and entitlement invariants, separate from HTTP/payment transport."""

import calendar
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from .errors import APIError
from .models import Account, AuditEvent, EntitlementGrant, Order, Refund

PLANS = {
    "monthly": {"id": "monthly", "name": "月度", "months": 1, "amount": 1200, "currency": "CNY"},
    "yearly": {"id": "yearly", "name": "年度", "months": 12, "amount": 9800, "currency": "CNY"},
}
FREE_FEATURES = ("basic_diagnostics", "menubar_monitoring", "basic_report", "safe_repairs")
PRO_FEATURES = ("history", "compare", "export_bundle")
TRIAL_DAYS = 14
OFFLINE_LICENSE_SECONDS = 7 * 86400
SHANGHAI = ZoneInfo("Asia/Shanghai")


def stamp(value):
    return int(value.timestamp()) if value else None


def add_months(anchor, months):
    local = anchor.astimezone(SHANGHAI)
    year, month0 = divmod(local.year * 12 + local.month - 1 + months, 12)
    month = month0 + 1
    return local.replace(
        year=year, month=month, day=min(local.day, calendar.monthrange(year, month)[1])
    )


def entitlement(account, now=None):
    now = now or timezone.now()
    grants = list(
        EntitlementGrant.objects.filter(account=account, revoked_at=None).order_by(
            "starts_at", "pk"
        )
    )
    paid_until = max((g.ends_at for g in grants), default=None)
    active_paid = any(g.starts_at <= now < g.ends_at for g in grants)
    trial_active = bool(account.trial_ends_at and account.trial_ends_at > now)
    if active_paid or trial_active:
        status = "paid" if active_paid else "trial"
        end = max([v for v in [account.trial_ends_at, paid_until] if v])
    else:
        status = (
            "expired"
            if account.trial_started_at
            or Order.objects.filter(account=account, granted_at__isnull=False).exists()
            else "eligible"
        )
        end = None
    return {
        "status": status,
        "trialEndsAt": stamp(account.trial_ends_at),
        "paidUntil": stamp(paid_until),
        "validUntil": stamp(end),
        "tier": "pro" if status in {"paid", "trial"} else "free",
        "features": list(PRO_FEATURES) if status in {"paid", "trial"} else [],
    }


@transaction.atomic
def start_trial(account):
    account = Account.objects.select_for_update().get(pk=account.pk)
    if (
        account.trial_started_at
        or Order.objects.filter(account=account, granted_at__isnull=False).exists()
    ):
        raise APIError("trial_unavailable", "此账号已试用或已购买，不能重新开启试用。", 409)
    account.trial_started_at = timezone.now()
    account.trial_ends_at = account.trial_started_at + timedelta(days=TRIAL_DAYS)
    account.save(update_fields=["trial_started_at", "trial_ends_at"])
    AuditEvent.objects.create(actor=account, action="trial_started", target=str(account.pk))
    return account


def renewal_terms(account, months, now=None):
    """One computation for previews and grants; grants call this under the account lock."""
    now = now or timezone.now()
    previous = (
        EntitlementGrant.objects.filter(account=account, revoked_at=None, ends_at__gt=now)
        .order_by("-ends_at")
        .first()
    )
    if previous:
        start, anchor, cumulative = (
            previous.ends_at,
            previous.anchor,
            previous.total_months + months,
        )
    else:
        start = max(now, account.trial_ends_at) if account.trial_ends_at else now
        anchor, cumulative = start, months
    return start, add_months(anchor, cumulative), anchor, cumulative


@transaction.atomic
def grant_payment(order_id, provider_transaction, paid_at, amount, currency="CNY"):
    # Every balance mutation locks account first, then order, avoiding cross-order lost updates.
    source = Order.objects.get(pk=order_id)
    account = Account.objects.select_for_update().get(pk=source.account_id)
    order = Order.objects.select_for_update().get(pk=order_id)
    if amount != order.amount or currency != order.currency or not provider_transaction:
        raise APIError("payment_mismatch", "支付核验信息不一致，订单尚未开通。", 409)
    if order.status in {"paid", "refunded"}:
        if order.provider_transaction != provider_transaction:
            raise APIError("payment_mismatch", "支付流水不一致。", 409)
        return order
    if order.status != "pending":
        raise APIError("order_closed", "订单已经关闭，请联系支持核查付款。", 409)
    now = timezone.now()
    start, end, anchor, cumulative = renewal_terms(account, order.months, now)
    EntitlementGrant.objects.create(
        order=order,
        account=account,
        starts_at=start,
        ends_at=end,
        anchor=anchor,
        total_months=cumulative,
    )
    order.status, order.provider_transaction, order.paid_at, order.granted_at = (
        "paid",
        provider_transaction,
        paid_at,
        now,
    )
    order.save(update_fields=["status", "provider_transaction", "paid_at", "granted_at"])
    AuditEvent.objects.create(
        action="payment_granted",
        target=str(order.pk),
        detail={"channel": order.channel, "amount": amount},
    )
    return order


@transaction.atomic
def confirm_refund(refund_id):
    source = Refund.objects.select_related("order").get(pk=refund_id)
    account = Account.objects.select_for_update().get(pk=source.order.account_id)
    refund = Refund.objects.select_for_update().select_related("order").get(pk=refund_id)
    if refund.status == "refunded":
        return refund
    now = timezone.now()
    grant = EntitlementGrant.objects.select_for_update().get(order=refund.order)
    if grant.revoked_at:
        refund.status = "refunded"
        refund.save(update_fields=["status", "updated_at"])
        return refund
    grant.revoked_at = now
    grant.save(update_fields=["revoked_at"])
    # Do not claw back consumed service. Compact only future grants, retaining other purchases.
    cursor = max(now, grant.starts_at)
    if grant.ends_at > now:
        future = list(
            EntitlementGrant.objects.filter(
                account=account, revoked_at=None, starts_at__gte=grant.ends_at
            )
            .select_related("order")
            .order_by("starts_at", "pk")
        )
        preceding = (
            EntitlementGrant.objects.filter(account=account, revoked_at=None, ends_at=cursor)
            .order_by("-pk")
            .first()
        )
        anchor, months = (preceding.anchor, preceding.total_months) if preceding else (cursor, 0)
        for following in future:
            months += following.order.months
            following.starts_at, following.anchor, following.total_months = cursor, anchor, months
            following.ends_at = add_months(anchor, months)
            following.save(update_fields=["starts_at", "ends_at", "anchor", "total_months"])
            cursor = following.ends_at
    refund.status = "refunded"
    refund.save(update_fields=["status", "updated_at"])
    refund.order.status = "refunded"
    refund.order.save(update_fields=["status"])
    AuditEvent.objects.create(
        action="refund_confirmed", target=str(refund.pk), detail={"order": str(refund.order_id)}
    )
    return refund


@transaction.atomic
def reject_refund(refund_id, actor, explanation):
    source = Refund.objects.select_related("order").get(pk=refund_id)
    Account.objects.select_for_update().get(pk=source.order.account_id)
    changed = Refund.objects.filter(pk=refund_id, status="requested").update(
        status="rejected", review_note=explanation[:2000], updated_at=timezone.now()
    )
    if changed:
        AuditEvent.objects.create(actor=actor, action="refund_rejected", target=str(refund_id))
    return bool(changed)
