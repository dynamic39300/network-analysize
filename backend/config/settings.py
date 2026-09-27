"""Fail-closed production configuration. Secrets are file/env inputs, never API output."""

import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
ENVIRONMENT = os.getenv("RELAY_ENV", "development")
if ENVIRONMENT not in {"development", "test", "production"}:
    raise ImproperlyConfigured("Invalid RELAY_ENV")
PRODUCTION = ENVIRONMENT == "production"
runtime_override = os.getenv("RELAY_RUNTIME_DIR", "")
if PRODUCTION and not runtime_override:
    raise ImproperlyConfigured("Production requires RELAY_RUNTIME_DIR outside the code directory")
RUNTIME_DIR = Path(runtime_override).expanduser() if runtime_override else BASE_DIR / ".runtime"
if not RUNTIME_DIR.is_absolute() or (
    PRODUCTION and RUNTIME_DIR.resolve().is_relative_to(BASE_DIR.resolve())
):
    raise ImproperlyConfigured("RELAY_RUNTIME_DIR must be an absolute writable data directory")
RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
DEBUG = not PRODUCTION
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if PRODUCTION:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY required")
    from django.core.management.utils import get_random_secret_key

    secret_file = RUNTIME_DIR / "django-secret"
    if not secret_file.exists():
        secret_file.write_text(get_random_secret_key())
        secret_file.chmod(0o600)
    SECRET_KEY = secret_file.read_text().strip()
if PRODUCTION and len(SECRET_KEY) < 50:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must contain at least 50 characters")
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://127.0.0.1:8016").rstrip("/")
parsed_public = urlsplit(PUBLIC_URL)
if (
    parsed_public.scheme not in {"http", "https"}
    or not parsed_public.hostname
    or parsed_public.path
    or parsed_public.query
    or parsed_public.fragment
    or parsed_public.username
    or parsed_public.password
):
    raise ImproperlyConfigured("PUBLIC_URL must be an origin")
if PRODUCTION and parsed_public.scheme != "https":
    raise ImproperlyConfigured("Production PUBLIC_URL requires HTTPS")
if (
    not PRODUCTION
    and parsed_public.scheme != "https"
    and parsed_public.hostname not in {"localhost", "127.0.0.1", "::1"}
):
    raise ImproperlyConfigured("Development HTTP is localhost-only")
ALLOWED_HOSTS = (
    [parsed_public.hostname, "localhost", "127.0.0.1", "testserver"]
    if not PRODUCTION
    else [parsed_public.hostname]
)
CSRF_TRUSTED_ORIGINS = [PUBLIC_URL]
SESSION_COOKIE_NAME = "relay_sessionid"
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "commerce",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "commerce.middleware.TrustedProxyMiddleware",
    "commerce.middleware.BearerCSRFMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "commerce.middleware.SecurityHeadersMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
AUTH_USER_MODEL = "commerce.Account"
AUTHENTICATION_BACKENDS = [
    "commerce.backends.AdminPasswordBackend",
    "commerce.backends.EmailCodeBackend",
]
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 14},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": RUNTIME_DIR / "db.sqlite3",
        "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
    }
}
if os.getenv("DATABASE_URL"):
    db = urlsplit(os.environ["DATABASE_URL"])
    if db.scheme not in {"postgres", "postgresql"}:
        raise ImproperlyConfigured("DATABASE_URL must use PostgreSQL")
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": db.path.lstrip("/"),
        "USER": unquote(db.username or ""),
        "PASSWORD": unquote(db.password or ""),
        "HOST": db.hostname,
        "PORT": db.port or 5432,
        "CONN_MAX_AGE": 60,
        "OPTIONS": {"sslmode": os.getenv("DATABASE_SSLMODE", "verify-full")},
    }
if PRODUCTION and DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
    raise ImproperlyConfigured("Production requires PostgreSQL")
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = RUNTIME_DIR / "staticfiles"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = PRODUCTION
CSRF_COOKIE_SECURE = PRODUCTION
SECURE_SSL_REDIRECT = PRODUCTION
SECURE_HSTS_SECONDS = 31536000 if PRODUCTION else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = PRODUCTION
SECURE_HSTS_PRELOAD = PRODUCTION
# Set only when reverse proxy overwrites this header and upstream is not publicly reachable.
if os.getenv("TRUST_PROXY_HTTPS") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Exact proxy addresses only. X-Real-IP is ignored unless REMOTE_ADDR is trusted.
TRUSTED_PROXY_IPS = tuple(
    value.strip() for value in os.getenv("TRUSTED_PROXY_IPS", "").split(",") if value.strip()
)
if TRUSTED_PROXY_IPS:
    from ipaddress import ip_address

    try:
        TRUSTED_PROXY_IPS = tuple(str(ip_address(value)) for value in TRUSTED_PROXY_IPS)
    except ValueError:
        raise ImproperlyConfigured("TRUSTED_PROXY_IPS requires literal IP addresses") from None
SECURE_REFERRER_POLICY = "no-referrer"
SESSION_COOKIE_AGE = 14 * 86400
DATA_UPLOAD_MAX_MEMORY_SIZE = 65536
DATA_UPLOAD_MAX_NUMBER_FIELDS = 100
CSRF_FAILURE_VIEW = "commerce.views.csrf_failure"
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.filebased.EmailBackend")
EMAIL_FILE_PATH = str(RUNTIME_DIR / "mail")
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "1") == "1"
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "NetCare <development@localhost>")
TERMS_VERSION = "2026-09-26"
PRIVACY_VERSION = "2026-09-26"
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "")
LEGAL_ENTITY_NAME = os.getenv("LEGAL_ENTITY_NAME", "")
LICENSE_PRIVATE_KEY_FILE = os.getenv(
    "LICENSE_PRIVATE_KEY_FILE", str(RUNTIME_DIR / "license-ed25519.pem") if not PRODUCTION else ""
)
SIMULATED_PAYMENTS = os.getenv("SIMULATED_PAYMENTS", "0") == "1"
WECHAT_ENABLED = os.getenv("WECHAT_ENABLED", "0") == "1"
ALIPAY_ENABLED = os.getenv("ALIPAY_ENABLED", "0") == "1"
WECHAT = {
    key: os.getenv("WECHAT_" + key, "")
    for key in [
        "APP_ID",
        "MCH_ID",
        "CERT_SERIAL",
        "PRIVATE_KEY_FILE",
        "PUBLIC_KEY_ID",
        "PUBLIC_KEY_FILE",
        "API_V3_KEY",
    ]
}
ALIPAY = {
    key: os.getenv("ALIPAY_" + key, "")
    for key in ["APP_ID", "SELLER_ID", "PRIVATE_KEY_FILE", "PUBLIC_KEY_FILE"]
}
for enabled, cfg, channel in [
    (WECHAT_ENABLED, WECHAT, "WECHAT"),
    (ALIPAY_ENABLED, ALIPAY, "ALIPAY"),
]:
    if enabled and not all(cfg.values()):
        raise ImproperlyConfigured(channel + " enabled with missing credentials")
if PRODUCTION:
    if SIMULATED_PAYMENTS or EMAIL_BACKEND != "django.core.mail.backends.smtp.EmailBackend":
        raise ImproperlyConfigured("Production forbids simulated payment and development email")
    if (
        not all(
            [
                EMAIL_HOST,
                EMAIL_HOST_USER,
                EMAIL_HOST_PASSWORD,
                SUPPORT_EMAIL,
                LEGAL_ENTITY_NAME,
                LICENSE_PRIVATE_KEY_FILE,
            ]
        )
        or "localhost" in DEFAULT_FROM_EMAIL
    ):
        raise ImproperlyConfigured(
            "Production requires SMTP, support identity and license signing key"
        )
if PRODUCTION:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        license_key = serialization.load_pem_private_key(
            Path(LICENSE_PRIVATE_KEY_FILE).read_bytes(), password=None
        )
        if not isinstance(license_key, Ed25519PrivateKey):
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise ImproperlyConfigured("Production license key is missing or invalid") from None
    if not EMAIL_USE_TLS:
        raise ImproperlyConfigured("Production SMTP requires TLS")
    if DATABASES["default"]["OPTIONS"]["sslmode"] != "verify-full":
        raise ImproperlyConfigured("Production PostgreSQL requires verify-full TLS")

for enabled, cfg, channel in [
    (WECHAT_ENABLED, WECHAT, "WECHAT"),
    (ALIPAY_ENABLED, ALIPAY, "ALIPAY"),
]:
    if enabled:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

        try:
            private = serialization.load_pem_private_key(
                Path(cfg["PRIVATE_KEY_FILE"]).read_bytes(), password=None
            )
            public = serialization.load_pem_public_key(Path(cfg["PUBLIC_KEY_FILE"]).read_bytes())
            if (
                not isinstance(private, RSAPrivateKey)
                or not isinstance(public, RSAPublicKey)
                or private.key_size < 2048
                or public.key_size < 2048
            ):
                raise ValueError
            if channel == "WECHAT" and len(cfg["API_V3_KEY"].encode()) != 32:
                raise ValueError
        except (OSError, ValueError, TypeError):
            raise ImproperlyConfigured(channel + " key configuration is invalid") from None

# Never log request bodies, query strings, codes or provider responses.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "loggers": {"django.server": {"handlers": ["null"], "propagate": False}},
}
