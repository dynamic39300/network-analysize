import json
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY
from django.test import Client, TestCase, override_settings

from commerce import auth
from commerce.models import Account, DesktopSession


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PrivilegedSessionTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create_user(
            username="promoted", email="promoted@example.test", password="synthetic-test-password"
        )

    def email_login(self):
        with patch("commerce.auth.secrets.randbelow", return_value=112233):
            auth.send_code(self.account.email, "127.0.0.1")
        response = self.client.post(
            "/api/v1/auth/verify-code",
            json.dumps(
                {
                    "email": self.account.email,
                    "code": "112233",
                    "acceptedTerms": True,
                    "termsVersion": settings.TERMS_VERSION,
                    "privacyVersion": settings.PRIVACY_VERSION,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

    def promote(self):
        Account.objects.filter(pk=self.account.pk).update(is_staff=True, is_superuser=True)

    def test_email_session_cannot_become_admin_after_account_promotion(self):
        self.email_login()
        self.promote()
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/admin/login/"))
        self.assertEqual(self.client.get("/api/v1/me").status_code, 401)

    def test_legacy_mixed_origin_session_cannot_become_admin(self):
        self.client.force_login(self.account, backend="django.contrib.auth.backends.ModelBackend")
        self.promote()
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.assertEqual(self.client.get("/api/v1/me").status_code, 401)

    def test_promoted_account_can_still_use_separate_password_admin_login(self):
        self.email_login()
        self.promote()
        admin = Client()
        response = admin.post(
            "/admin/login/?next=/admin/",
            {"username": "promoted", "password": "synthetic-test-password", "next": "/admin/"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            admin.session[BACKEND_SESSION_KEY], "commerce.backends.AdminPasswordBackend"
        )
        self.assertEqual(admin.get("/admin/").status_code, 200)

    def test_existing_desktop_credentials_cannot_survive_staff_promotion(self):
        session = DesktopSession(account=self.account, label="Before promotion")
        tokens = auth.tokens_for(session)
        self.promote()
        self.assertEqual(
            self.client.get(
                "/api/v1/me", HTTP_AUTHORIZATION="Bearer " + tokens["accessToken"]
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/desktop/refresh",
                json.dumps({"refreshToken": tokens["refreshToken"]}),
                content_type="application/json",
            ).status_code,
            401,
        )
