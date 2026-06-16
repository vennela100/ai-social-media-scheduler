# AI Social Media Scheduler

Upload a video → AI generates platform-appropriate titles/descriptions/hashtags →
schedule a publish time → it auto-publishes to YouTube, Instagram, and LinkedIn,
even when your laptop is off. Built to run at **$0/month**.

## Architecture

One Django codebase, two execution contexts, one shared Postgres database:

| Context | Runs on | Job |
|---|---|---|
| Web app | Render (free) | auth, upload, OAuth, AI generation, dashboard |
| Scheduler | GitHub Actions (cron, every 5 min) | `publish_due_posts` management command |

The two never talk directly — they coordinate **only through the database**.
That's why scheduled posts publish on time regardless of whether the web app is awake.

## Tech stack

Django 5.2 · Neon Postgres · Cloudinary · Google Gemini 2.5 Flash · Render ·
GitHub Actions · WhiteNoise · Fernet-encrypted OAuth tokens.

## Local setup

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env            # then fill in keys (SQLite is used until DATABASE_URL is set)
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Generate the two keys you need for `.env`:

```bash
# DJANGO_SECRET_KEY
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
# TOKEN_ENCRYPTION_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Build phases

- [x] **Phase 0** — Foundation: project, models, encrypted tokens, deploy config
- [x] **Phase 1** — Upload & storage (Cloudinary) + dashboard
- [x] **Phase 2** — AI metadata (Gemini)
- [x] **Phase 3** — YouTube integration
- [x] **Phase 4** — The scheduler (`publish_due_posts` + GitHub Actions)
- [x] **Phase 5** — Instagram
- [x] **Phase 6** — LinkedIn
- [ ] **Phase 7** — Polish (analytics, timezone UI)
- [ ] **Phase 8** — Optional React frontend

## Security notes

- OAuth tokens are encrypted at rest (Fernet) via a custom model field; tokens
  are never exposed in the admin and never logged.
- All secrets come from environment variables / GitHub Secrets — never committed.
