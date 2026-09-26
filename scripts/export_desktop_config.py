#!/usr/bin/env python3
"""Export only a local preview origin and PUBLIC key; never export credentials."""

import base64
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def main():
    from django.conf import settings

    origin = urlsplit(settings.PUBLIC_URL)
    if settings.PRODUCTION or origin.hostname not in {"localhost", "127.0.0.1"}:
        raise SystemExit("This exporter is for loopback development only.")
    public_path = Path(settings.LICENSE_PRIVATE_KEY_FILE).with_suffix(".public.pem")
    public = serialization.load_pem_public_key(public_path.read_bytes())
    encoded = base64.b64encode(
        public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    destination = settings.RUNTIME_DIR / "desktop-public.json"
    destination.write_text(
        json.dumps(
            {"origin": settings.PUBLIC_URL, "publicKey": encoded, "development": True}, indent=2
        )
        + "\n"
    )
    print(f"Public-only desktop preview config: {destination}")


if __name__ == "__main__":
    main()
