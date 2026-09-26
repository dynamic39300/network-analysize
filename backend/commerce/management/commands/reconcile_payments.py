from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from commerce.errors import APIError
from commerce.models import Order, Refund
from commerce.payments import provider, synchronize_order


def claim_batch(queryset, limit=100):
    """Persist a claim before external I/O so failing/slow head entries cannot starve the queue."""
    now = timezone.now()
    with transaction.atomic():
        batch = list(
            queryset.filter(next_check_at__lte=now)
            .order_by("next_check_at", "created_at")
            .select_for_update(skip_locked=connection.features.has_select_for_update_skip_locked)[
                :limit
            ]
        )
        for item in batch:
            item.sync_attempts += 1
            item.next_check_at = now + timedelta(minutes=5)
            item.save(update_fields=["sync_attempts", "next_check_at"])
    return batch


def finish_check(item):
    # A crash leaves the five-minute claim; handled failures still advance a bounded backoff.
    now = timezone.now()
    delay = min(21600, 60 * (2 ** min(item.sync_attempts - 1, 9)))
    type(item).objects.filter(pk=item.pk).update(
        last_checked_at=now, next_check_at=now + timedelta(seconds=delay)
    )


class Command(BaseCommand):
    help = "Fairly reconcile up to 100 due payments and refunds. Persistent claims and backoff."

    def handle(self, *args, **options):
        checked, failed = 0, 0
        for order in claim_batch(
            Order.objects.filter(status="pending").exclude(channel="simulated")
        ):
            try:
                synchronize_order(order)
                checked += 1
            except APIError:
                failed += 1
            finally:
                finish_check(order)
        for refund in claim_batch(
            Refund.objects.filter(status="processing").exclude(order__channel="simulated")
        ):
            try:
                provider(refund.order.channel).query_refund(refund)
                checked += 1
            except APIError:
                failed += 1
            finally:
                finish_check(refund)
        self.stdout.write(f"Reconciled={checked} retry_required={failed}")
