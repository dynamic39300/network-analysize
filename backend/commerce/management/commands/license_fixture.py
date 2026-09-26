import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test import override_settings
from django.utils import timezone

from commerce.auth import issue_license
from commerce.models import Account, DesktopSession


class Command(BaseCommand):
    help = "Write a newly generated test-only public license fixture; never a private key."

    def add_arguments(self, parser):
        parser.add_argument("output")

    def handle(self, *args, **options):
        if settings.PRODUCTION:
            raise CommandError("Test fixture generation forbidden in production")
        key = Ed25519PrivateKey.generate()
        account = Account(id=uuid4(), email="fixture@example.test")
        session = DesktopSession(id=uuid4(), account=account)
        now = int(timezone.now().timestamp())
        with TemporaryDirectory() as temp:
            path = Path(temp) / "test-key.pem"
            path.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            with override_settings(LICENSE_PRIVATE_KEY_FILE=str(path)):
                license_value = issue_license(
                    account, session, {"validUntil": now + 14 * 86400, "status": "trial"}
                )
        data = {
            "publicKey": base64.b64encode(
                key.public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )
            ).decode(),
            "issuer": settings.PUBLIC_URL,
            "accountId": str(account.id),
            "sessionId": str(session.id),
            "license": license_value,
            "now": now,
        }
        Path(options["output"]).write_text(json.dumps(data))
        self.stdout.write("Test-only public signature fixture written.")
