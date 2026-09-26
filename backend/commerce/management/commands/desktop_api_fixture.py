import base64
import hashlib
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from commerce import auth
from commerce.models import Account


class Command(BaseCommand):
    help = "Create a 5-minute, single-use loopback desktop API fixture for a synthetic account."

    def add_arguments(self, parser):
        parser.add_argument("output")

    def handle(self, *args, **options):
        if settings.ENVIRONMENT != "development" or urlsplit(settings.PUBLIC_URL).hostname not in {
            "127.0.0.1",
            "localhost",
        }:
            raise CommandError("Fixture requires local development origin")
        name = "api-fixture-" + secrets.token_hex(8)
        account = Account.objects.create_user(username=name, email=name + "@example.test")
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(32)
        start = auth.desktop_start(
            auth.b64url(hashlib.sha256(verifier.encode()).digest()),
            state,
            "Synthetic API verification",
        )
        request_id = start["requestId"]
        auth.approve_request(request_id, account)
        key = (
            auth.signing_key()
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
        data = {
            "serviceURL": settings.PUBLIC_URL,
            "publicKey": base64.b64encode(key).decode(),
            "requestId": request_id,
            "state": state,
            "codeVerifier": verifier,
        }
        path = Path(options["output"])
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
        self.stdout.write("Single-use synthetic API fixture written; expires in five minutes.")
