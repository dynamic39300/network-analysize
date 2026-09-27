#!/usr/bin/env python3
"""Run commerce tests on a disposable, Unix-socket-only PostgreSQL cluster.

From the repository root:
  uv run --directory backend --with pgserver python ../scripts/verify_postgres.py

The extra package is supplied by uv for this command only; it is not a production
or lockfile dependency. No existing database, secret, daemon, or TCP port is used.
"""

import importlib.metadata
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
CONCURRENCY_PREFIX = "commerce.tests.test_concurrency.PostgreSQLConcurrencyTests."
REQUIRED_CONCURRENCY = {
    "test_two_simultaneous_orders_append_without_lost_month",
    "test_simultaneous_duplicate_notification_grants_once",
    "test_simultaneous_trial_claim_has_one_winner",
    "test_refund_rejection_racing_channel_success_keeps_success",
    "test_simultaneous_device_poll_consumes_once",
}


def isolated_environment(directory, socket_directory, port):
    # Remove inherited credentials and libpq service selectors before importing
    # Django. In particular, no ambient production DATABASE_URL or mail backend
    # may select a real service for this verification run.
    prefixes = (
        "PG",
        "RELAY_",
        "DATABASE_",
        "DJANGO_",
        "EMAIL_",
        "WECHAT_",
        "ALIPAY_",
        "LICENSE_",
        "SIMULATED_",
        "SUPPORT_",
        "LEGAL_",
        "TRUSTED_PROXY_",
    )
    for key in tuple(os.environ):
        if key.startswith(prefixes) or key in {
            "PUBLIC_URL",
            "DEFAULT_FROM_EMAIL",
            "TRUST_PROXY_HTTPS",
        }:
            os.environ.pop(key)
    os.environ.update(
        {
            "RELAY_ENV": "test",
            "RELAY_RUNTIME_DIR": str(directory / "runtime"),
            "PUBLIC_URL": "http://127.0.0.1:8016",
            "DATABASE_URL": "postgresql://postgres@/relay_verify",
            "DATABASE_SSLMODE": "disable",
            "PGHOST": str(socket_directory),
            "PGPORT": str(port),
            "PGPASSFILE": str(directory / "nonexistent-pgpass"),
            "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
            "DEFAULT_FROM_EMAIL": "NetCare <fixture@localhost>",
            "WECHAT_ENABLED": "0",
            "ALIPAY_ENABLED": "0",
            "SIMULATED_PAYMENTS": "0",
            "DJANGO_SETTINGS_MODULE": "config.settings",
        }
    )


def run_suite(socket_directory, port):
    sys.path.insert(0, str(BACKEND))
    import django

    django.setup()
    from django.conf import settings
    from django.db import connections
    from django.test.runner import DiscoverRunner

    # settings.py intentionally accepts only ordinary PostgreSQL URL authorities.
    # Override the local test connection in memory, not production URL parsing or
    # the production verify-full TLS requirement.
    settings.DATABASES["default"]["HOST"] = str(socket_directory)
    settings.DATABASES["default"]["PORT"] = port
    settings.DATABASES["default"]["CONN_MAX_AGE"] = 0
    Path(settings.STATIC_ROOT).mkdir(mode=0o700, parents=True, exist_ok=True)

    class TrackedResult(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.concurrency_tests = set()

        def startTest(self, test):
            super().startTest(test)
            if test.id().startswith(CONCURRENCY_PREFIX):
                self.concurrency_tests.add(test.id().removeprefix(CONCURRENCY_PREFIX))

    class AuditedRunner(DiscoverRunner):
        result = None

        def get_resultclass(self):
            return TrackedResult

        def run_suite(self, suite, **kwargs):
            self.result = super().run_suite(suite, **kwargs)
            return self.result

    runner = AuditedRunner(verbosity=1, interactive=False)
    try:
        connection = connections["default"]
        connection.ensure_connection()
        if (
            connection.vendor != "postgresql"
            or not connection.features.has_select_for_update
        ):
            raise RuntimeError(
                "Verification must execute on PostgreSQL with row locking"
            )
        if connection.connection.info.host != str(socket_directory):
            raise RuntimeError("Django did not connect through the private Unix socket")
        failures = runner.run_tests(["commerce.tests"])
        result = runner.result
        skipped_concurrency = {
            test.id().removeprefix(CONCURRENCY_PREFIX)
            for test, _ in result.skipped
            if test.id().startswith(CONCURRENCY_PREFIX)
        }
        missing = REQUIRED_CONCURRENCY - result.concurrency_tests
        if missing or skipped_concurrency:
            raise RuntimeError(
                "Required PostgreSQL concurrency tests were missing or skipped"
            )
        print(
            f"PostgreSQL verification: {result.testsRun} tests; "
            f"{len(result.concurrency_tests)} concurrency tests executed; "
            f"{len(result.skipped)} skipped; {failures} failures.",
            flush=True,
        )
        return bool(failures)
    finally:
        connections.close_all()


def main():
    if os.name != "posix" or os.geteuid() == 0:
        raise SystemExit(
            "Run as a normal macOS/Linux user; no system users or services are created."
        )
    # No inherited libpq service/config can influence the freshly spawned server.
    for key in tuple(os.environ):
        if key.startswith("PG"):
            os.environ.pop(key)
    import pgserver
    import psycopg

    with tempfile.TemporaryDirectory(prefix="relay-pg-", dir="/tmp") as temporary:
        directory = Path(temporary).resolve()
        if stat.S_IMODE(directory.stat().st_mode) != 0o700:
            raise RuntimeError("Temporary PostgreSQL parent must be private (0700)")
        pgdata = directory / "data"
        with pgserver.get_server(pgdata, cleanup_mode="delete") as server:
            info = server.get_postmaster_info()
            if info.hostname is not None or info.socket_dir is None:
                raise RuntimeError("Temporary PostgreSQL must not listen on TCP")
            socket_directory = info.socket_dir.resolve()
            if not socket_directory.is_relative_to(directory):
                raise RuntimeError(
                    "Temporary PostgreSQL socket escaped the private directory"
                )
            with psycopg.connect(
                server.get_uri("postgres"), sslmode="disable", autocommit=True
            ) as conn:
                version = conn.execute("SHOW server_version").fetchone()[0]
                if conn.execute("SHOW listen_addresses").fetchone()[0] != "":
                    raise RuntimeError(
                        "Temporary PostgreSQL unexpectedly listens on a network address"
                    )
                if (
                    Path(conn.execute("SHOW data_directory").fetchone()[0]).resolve()
                    != pgdata
                ):
                    raise RuntimeError(
                        "Connected PostgreSQL is not the disposable test cluster"
                    )
                conn.execute("CREATE DATABASE relay_verify")
            print(
                f"Private Unix-socket PostgreSQL {version}; "
                f"pgserver {importlib.metadata.version('pgserver')}; no TCP listener.",
                flush=True,
            )
            isolated_environment(directory, socket_directory, info.port)
            failed = run_suite(socket_directory, info.port)
        if pgdata.exists():
            raise RuntimeError(
                "Temporary PostgreSQL data was not removed after shutdown"
            )
    print("Disposable PostgreSQL stopped and removed.", flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
