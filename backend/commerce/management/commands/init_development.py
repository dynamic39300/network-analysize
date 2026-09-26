import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create a local-only signing key (does not print secrets)."

    def handle(self, *args, **options):
        if settings.PRODUCTION:
            raise CommandError("Refusing development initialization in production")
        path = Path(settings.LICENSE_PRIVATE_KEY_FILE)
        if not path.exists():
            key = Ed25519PrivateKey.generate()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(
                    key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    )
                )
            path.with_suffix(".public.pem").write_bytes(
                key.public_key().public_bytes(
                    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
                )
            )
        Path(settings.EMAIL_FILE_PATH).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.stdout.write(
            "Local development signing key and mail directory ready; no secrets printed."
        )
