#!/usr/bin/env bash
# Start the full stack for local dev: Django JSON API (:8000) + Next.js UI (:3000).
# The Next dev server proxies /api/* to Django, so just open http://localhost:3000.
#
#   ./run-dev.sh
#   login: vennela / password
set -euo pipefail
cd "$(dirname "$0")"

# First run? bootstrap: venv + deps + .env + migrate + seed.
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
if [ ! -f .env ]; then
  SECRET=$(.venv/bin/python -c "from django.core.management.utils import get_random_secret_key as k; print(k())")
  FERNET=$(.venv/bin/python -c "from cryptography.fernet import Fernet as F; print(F.generate_key().decode())")
  cat > .env <<EOF
DJANGO_SECRET_KEY=$SECRET
DJANGO_DEBUG=true
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
TOKEN_ENCRYPTION_KEY=$FERNET
DASHBOARD_URL=http://localhost:3000
EOF
fi
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_demo
[ -d frontend/node_modules ] || (cd frontend && npm install)

# Run both; Ctrl-C stops the pair.
trap 'kill 0' EXIT
.venv/bin/python manage.py runserver 127.0.0.1:8000 &
(cd frontend && npm run dev) &
wait
