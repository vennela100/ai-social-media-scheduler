#!/usr/bin/env bash
# Render runs this on every deploy. It must be idempotent.
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input   # gather static files for WhiteNoise
python manage.py migrate                     # apply DB schema to Neon
