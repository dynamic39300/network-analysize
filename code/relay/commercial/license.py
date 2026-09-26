"""Verify a pinned Ed25519 license, never trust claims decoded without a signature."""

import base64
import json
import time

FEATURES = frozenset({"history", "compare", "export_bundle"})
OFFLINE_SECONDS = 7 * 86400


class InvalidLicense(ValueError):
    pass


def _decode(value):
    if not isinstance(value, str) or len(value) > 16384:
        raise InvalidLicense("invalid encoding")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise InvalidLicense("invalid encoding") from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidLicense("duplicate claim")
        result[key] = value
    return result


def verify_license(token, public_key, issuer, account_id, session_id, now=None, last_seen=0):
    """Return verified claims, or raise InvalidLicense. No online dependency."""
    if type(last_seen) is not int or last_seen < 0:
        raise InvalidLicense("invalid local timestamp")
    now = int(time.time() if now is None else now)
    if now + 120 < last_seen:
        raise InvalidLicense("clock moved backwards")
    # A tolerated clock correction must never revive an already-observed expiry.
    now = max(now, last_seen)
    try:
        if not isinstance(token, str) or len(token) > 16384:
            raise InvalidLicense("missing license")
        header_part, payload_part, signature_part = token.split(".")
        header = json.loads(_decode(header_part), object_pairs_hook=_unique_object)
        if header != {"alg": "EdDSA", "typ": "JWT", "kid": "license-v1"}:
            raise InvalidLicense("unexpected algorithm or key")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        Ed25519PublicKey.from_public_bytes(_decode(public_key)).verify(
            _decode(signature_part), (header_part + "." + payload_part).encode("ascii")
        )
        claims = json.loads(_decode(payload_part), object_pairs_hook=_unique_object)
        if not isinstance(claims, dict):
            raise InvalidLicense("invalid claims")
        expected = {"iss": issuer, "aud": "relay", "sub": account_id, "sid": session_id}
        if any(not value or claims.get(key) != value for key, value in expected.items()):
            raise InvalidLicense("license belongs to another account or application")
        for key in ("iat", "nbf", "exp", "entitlementUntil"):
            if type(claims.get(key)) is not int:
                raise InvalidLicense("invalid timestamp")
        if not (
            claims["nbf"] <= now < claims["exp"]
            and claims["nbf"] <= claims["iat"] <= now + 60
            and claims["iat"] < claims["exp"] <= claims["entitlementUntil"]
            and claims["exp"] <= claims["iat"] + OFFLINE_SECONDS
        ):
            raise InvalidLicense("license expired or exceeds its offline window")
        if claims.get("status") not in {"trial", "paid"}:
            raise InvalidLicense("inactive license")
        features = claims.get("features")
        if not isinstance(features, list) or any(not isinstance(f, str) for f in features):
            raise InvalidLicense("invalid features")
        if not set(features).issubset(FEATURES):
            raise InvalidLicense("unknown features")
        return claims
    except InvalidLicense:
        raise
    except Exception as exc:
        raise InvalidLicense("license verification failed") from exc
