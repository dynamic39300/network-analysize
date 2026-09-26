from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from commerce.domain import add_months, confirm_refund, grant_payment, reject_refund
from commerce.models import Account, AuditEvent, EntitlementGrant, Order, Refund
from commerce.payments import PaymentError


class ReconciliationTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create_user(username="queue", email="queue@example.test")

    def orders(self, count=101, status="pending"):
        # Also prove old payments remain eligible after a long service outage.
        old = timezone.now() - timedelta(days=30)
        return [
            Order.objects.create(
                account=self.account,
                idempotency_key=str(i),
                plan_id="monthly",
                months=1,
                amount=1200,
                channel="wechat",
                status=status,
                created_at=old + timedelta(seconds=i),
            )
            for i in range(count)
        ]

    def test_failing_first_hundred_orders_do_not_starve_paid_tail(self):
        orders = self.orders()
        tail = orders[-1]

        def query(order):
            if order.pk == tail.pk:
                return grant_payment(order.pk, "tail-paid", timezone.now(), 1200)
            raise PaymentError()

        with patch(
            "commerce.management.commands.reconcile_payments.synchronize_order", side_effect=query
        ):
            call_command("reconcile_payments", stdout=StringIO())
            self.assertFalse(EntitlementGrant.objects.filter(order=tail).exists())
            self.assertEqual(Order.objects.filter(last_checked_at__isnull=False).count(), 100)
            call_command("reconcile_payments", stdout=StringIO())
        self.assertTrue(EntitlementGrant.objects.filter(order=tail).exists())
        self.assertEqual(Order.objects.filter(last_checked_at__isnull=False).count(), 101)

    def test_failing_first_hundred_refunds_do_not_starve_successful_tail(self):
        orders = self.orders(status="paid")
        refunds = [
            Refund.objects.create(order=order, reason="synthetic queue", status="processing")
            for order in orders
        ]
        tail = refunds[-1]
        now = timezone.now()
        EntitlementGrant.objects.create(
            order=tail.order,
            account=self.account,
            starts_at=now,
            ends_at=add_months(now, 1),
            anchor=now,
            total_months=1,
        )

        def query(refund):
            if refund.pk == tail.pk:
                return confirm_refund(refund.pk)
            raise PaymentError()

        with patch("commerce.management.commands.reconcile_payments.provider") as provider:
            provider.return_value.query_refund.side_effect = query
            call_command("reconcile_payments", stdout=StringIO())
            self.assertEqual(Refund.objects.filter(last_checked_at__isnull=False).count(), 100)
            call_command("reconcile_payments", stdout=StringIO())
        tail.refresh_from_db()
        self.assertEqual(tail.status, "refunded")
        self.assertEqual(Refund.objects.filter(last_checked_at__isnull=False).count(), 101)

    def test_stale_rejection_never_overwrites_processing_or_refunded(self):
        order = self.orders(1, status="paid")[0]
        stale = Refund.objects.create(order=order, reason="request")
        for current in ["processing", "refunded"]:
            Refund.objects.filter(pk=stale.pk).update(status=current)
            self.assertFalse(reject_refund(stale.pk, self.account, "stale rejection"))
            self.assertEqual(Refund.objects.get(pk=stale.pk).status, current)
        self.assertFalse(AuditEvent.objects.filter(action="refund_rejected").exists())

    def test_valid_rejection_changes_only_requested_and_records_once(self):
        order = self.orders(1, status="paid")[0]
        refund = Refund.objects.create(order=order, reason="request")
        self.assertTrue(reject_refund(refund.pk, self.account, "Explained through support"))
        self.assertFalse(reject_refund(refund.pk, self.account, "Again"))
        self.assertEqual(AuditEvent.objects.filter(action="refund_rejected").count(), 1)
