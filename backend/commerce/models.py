import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class Account(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    trial_started_at = models.DateTimeField(null=True, blank=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)


class ConsentRecord(models.Model):
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    document = models.CharField(max_length=20)
    version = models.CharField(max_length=32)
    accepted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "document", "version"], name="unique_account_document_consent"
            )
        ]


class EmailChallenge(models.Model):
    email = models.EmailField(primary_key=True)
    digest = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)


class RateBucket(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    starts_at = models.DateTimeField()
    count = models.PositiveIntegerField(default=0)


class DesktopAuthorization(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    challenge = models.CharField(max_length=43)
    state = models.CharField(max_length=256)
    label = models.CharField(max_length=80)
    expires_at = models.DateTimeField()
    account = models.ForeignKey(Account, null=True, on_delete=models.CASCADE)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    consumed = models.BooleanField(default=False)
    attempts = models.PositiveSmallIntegerField(default=0)


class DesktopSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE)
    label = models.CharField(max_length=80)
    access_hash = models.CharField(max_length=64, unique=True)
    refresh_hash = models.CharField(max_length=64, unique=True)
    access_expires_at = models.DateTimeField()
    refresh_expires_at = models.DateTimeField()
    revoked = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)


class UsedRefreshToken(models.Model):
    digest = models.CharField(max_length=64, unique=True)
    session = models.ForeignKey(DesktopSession, on_delete=models.CASCADE)
    expires_at = models.DateTimeField()


class Order(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=80)
    terms_version = models.CharField(max_length=32, default="", blank=True)
    plan_id = models.CharField(max_length=20)
    months = models.PositiveSmallIntegerField()
    amount = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="CNY")
    channel = models.CharField(max_length=20)
    status = models.CharField(max_length=20, default="pending")
    last_checked_at = models.DateTimeField(null=True, blank=True)
    next_check_at = models.DateTimeField(default=timezone.now, db_index=True)
    sync_attempts = models.PositiveIntegerField(default=0)
    payment_url = models.TextField(blank=True)
    provider_transaction = models.CharField(max_length=100, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    paid_at = models.DateTimeField(null=True, blank=True)
    granted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "idempotency_key"], name="order_account_idempotency"
            ),
            models.UniqueConstraint(
                fields=["channel", "provider_transaction"], name="unique_provider_transaction"
            ),
        ]


class EntitlementGrant(models.Model):
    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="grant")
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    anchor = models.DateTimeField()
    total_months = models.PositiveIntegerField()
    revoked_at = models.DateTimeField(null=True, blank=True)


class Refund(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="refund")
    reason = models.TextField(max_length=2000)
    status = models.CharField(max_length=20, default="requested")
    last_checked_at = models.DateTimeField(null=True, blank=True)
    next_check_at = models.DateTimeField(default=timezone.now, db_index=True)
    sync_attempts = models.PositiveIntegerField(default=0)
    review_note = models.TextField(blank=True, max_length=2000)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [("process_refund", "Approve and submit channel refunds")]


class SupportTicket(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    order = models.ForeignKey(Order, null=True, blank=True, on_delete=models.PROTECT)
    subject = models.CharField(max_length=160)
    status = models.CharField(max_length=20, default="open")
    created_at = models.DateTimeField(default=timezone.now)


class SupportMessage(models.Model):
    ticket = models.ForeignKey(SupportTicket, related_name="messages", on_delete=models.CASCADE)
    body = models.TextField(max_length=5000)
    from_support = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)


class Release(models.Model):
    version = models.CharField(max_length=40, unique=True)
    url = models.URLField()
    sha256 = models.CharField(max_length=64)
    notes = models.TextField(max_length=5000)
    minimum_os = models.CharField(max_length=20, default="12.0")
    architecture = models.CharField(max_length=20, default="arm64")
    verified_signed_notarized = models.BooleanField(default=False)
    published = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)


class AuditEvent(models.Model):
    actor = models.ForeignKey(Account, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=80)
    target = models.CharField(max_length=100)
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)
