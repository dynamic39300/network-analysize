import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.db import close_old_connections
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from commerce import auth
from commerce.domain import add_months, grant_payment, start_trial
from commerce.errors import APIError
from commerce.models import Account, DesktopSession, EntitlementGrant, Order


@skipUnlessDBFeature("has_select_for_update")
class PostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.account = Account.objects.create_user(
            username="concurrent", email="concurrent@example.test"
        )

    def order(self, key):
        return Order.objects.create(
            account=self.account,
            idempotency_key=key,
            plan_id="monthly",
            months=1,
            amount=1200,
            channel="simulated",
        )

    def parallel(self, operations):
        barrier = Barrier(len(operations))

        def run(operation):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return operation()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            return list(pool.map(run, operations))

    def test_two_simultaneous_orders_append_without_lost_month(self):
        one, two = self.order("one"), self.order("two")
        self.parallel(
            [
                lambda: grant_payment(one.pk, "txn-one", timezone.now(), 1200),
                lambda: grant_payment(two.pk, "txn-two", timezone.now(), 1200),
            ]
        )
        grants = list(EntitlementGrant.objects.order_by("starts_at"))
        self.assertEqual(len(grants), 2)
        self.assertEqual(grants[1].starts_at, grants[0].ends_at)
        self.assertEqual(grants[1].ends_at, add_months(grants[0].anchor, 2))

    def test_simultaneous_duplicate_notification_grants_once(self):
        order = self.order("same")
        self.parallel(
            [
                lambda: grant_payment(order.pk, "same-txn", timezone.now(), 1200),
                lambda: grant_payment(order.pk, "same-txn", timezone.now(), 1200),
            ]
        )
        self.assertEqual(EntitlementGrant.objects.filter(order=order).count(), 1)

    def test_simultaneous_trial_claim_has_one_winner(self):
        def attempt():
            try:
                start_trial(self.account)
                return "granted"
            except APIError as error:
                return error.code

        results = self.parallel([attempt, attempt])
        self.assertCountEqual(results, ["granted", "trial_unavailable"])
        self.account.refresh_from_db()
        self.assertIsNotNone(self.account.trial_started_at)

    def test_refund_rejection_racing_channel_success_keeps_success(self):
        from commerce.domain import confirm_refund, reject_refund
        from commerce.models import Refund

        order = self.order("refund-race")
        grant_payment(order.pk, "refund-race-payment", timezone.now(), 1200)
        refund = Refund.objects.create(order=order, reason="synthetic race")
        self.parallel(
            [
                lambda: confirm_refund(refund.pk),
                lambda: reject_refund(refund.pk, self.account, "review rejection"),
            ]
        )
        refund.refresh_from_db()
        self.assertEqual(refund.status, "refunded")
        self.assertIsNotNone(EntitlementGrant.objects.get(order=order).revoked_at)

    def test_simultaneous_device_poll_consumes_once(self):
        verifier, state = "v" * 64, "s" * 43
        started = auth.desktop_start(
            auth.b64url(hashlib.sha256(verifier.encode()).digest()), state, "Race Mac"
        )
        auth.approve_request(started["requestId"], self.account)

        def attempt():
            try:
                auth.poll(started["requestId"], verifier, state)
                return "granted"
            except APIError as error:
                return error.code

        self.assertCountEqual(
            self.parallel([attempt, attempt]), ["granted", "invalid_authorization"]
        )
        self.assertEqual(DesktopSession.objects.count(), 1)
