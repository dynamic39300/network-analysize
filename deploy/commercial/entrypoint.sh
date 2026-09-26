#!/bin/sh
set -eu
umask 077
if [ "${RELAY_ENV:-}" != production ]; then
  echo 'This deployment image requires RELAY_ENV=production.' >&2
  exit 1
fi
# Django validates all required production inputs before any server can start.
python manage.py check --deploy --fail-level ERROR
if [ "${1:-}" = gunicorn ]; then
  python manage.py migrate --check
  python manage.py collectstatic --noinput
fi
exec "$@"
