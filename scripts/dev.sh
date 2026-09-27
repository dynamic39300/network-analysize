#!/bin/bash
# Local-only workspace entry point; never installs over the running NetCare app.
set -euo pipefail
RELAY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-web}"
case "$MODE" in
  web|desktop) ;;
  --help) echo 'Usage: scripts/dev.sh [web|desktop]'; exit 0 ;;
  *) echo 'Expected web or desktop.' >&2; exit 2 ;;
esac
command -v uv >/dev/null || { echo 'Install uv first.' >&2; exit 1; }
# A development launcher must not inherit an external production database or keys.
unset DATABASE_URL LICENSE_PRIVATE_KEY_FILE DJANGO_SECRET_KEY TRUST_PROXY_HTTPS TRUSTED_PROXY_IPS
export RELAY_ENV=development PUBLIC_URL=http://127.0.0.1:8016
export RELAY_RUNTIME_DIR="$RELAY_ROOT/backend/.runtime"
export EMAIL_BACKEND=django.core.mail.backends.filebased.EmailBackend
export WECHAT_ENABLED=0 ALIPAY_ENABLED=0 SIMULATED_PAYMENTS=0
uv sync --directory "$RELAY_ROOT/backend" --locked
uv run --directory "$RELAY_ROOT/backend" python manage.py migrate --noinput
uv run --directory "$RELAY_ROOT/backend" python manage.py init_development
if [[ "$MODE" == web ]]; then
  echo 'NetCare preview: http://127.0.0.1:8016/ (local email files; real payments disabled)'
  exec uv run --directory "$RELAY_ROOT/backend" python manage.py runserver 127.0.0.1:8016 --noreload
fi
[[ "$(uname -s)" == Darwin ]] || { echo 'Desktop mode requires macOS.' >&2; exit 1; }
uv sync --directory "$RELAY_ROOT/apps/macos" --locked
uv run --directory "$RELAY_ROOT/backend" python "$RELAY_ROOT/scripts/export_desktop_config.py"
export RELAY_COMMERCIAL_CONFIG="$RELAY_RUNTIME_DIR/desktop-public.json"
exec uv run --directory "$RELAY_ROOT/apps/macos" python "$RELAY_ROOT/code/network-doctor-menu.py"
