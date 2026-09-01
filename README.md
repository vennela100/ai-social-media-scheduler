# AI Social Media Scheduler

Upload a video or image -> AI generates platform-appropriate titles, descriptions
and hashtags -> schedule a publish time -> the app auto-publishes to YouTube,
Instagram, and LinkedIn, even when your laptop is off. Built to run at $0/month.

## Architecture

One Django codebase runs in multiple execution contexts. The web app handles the
user workflow, while scheduled background jobs pick up due work through the
shared database.

```mermaid
flowchart LR
    User[Creator] --> Browser[Browser]
    Browser --> Render[Django web app on Render]
    Browser -- direct large upload --> R2[Cloudflare R2]
    Render -- small media and thumbnails --> Cloudinary[Cloudinary]
    Render --> DB[(Neon Postgres)]
    Render --> Gemini[Google Gemini]
    Render --> OAuth[YouTube / Instagram / LinkedIn OAuth]

    Cron[GitHub Actions or external cron] --> Commands[Django management commands]
    Commands --> DB
    Commands --> Gemini
    Commands --> R2
    Commands --> Cloudinary
    Commands --> Platforms[YouTube / Instagram / LinkedIn APIs]
    Commands --> Alerts[Email / Telegram alerts]
```

### Runtime Contexts

```mermaid
flowchart TB
    subgraph Render["Render web service"]
        Web[Django views and templates]
        API[Upload, OAuth, AI, schedule, analytics endpoints]
        WhiteNoise[WhiteNoise static files]
    end

    subgraph Jobs["Scheduled workers"]
        Publisher[publish_due_posts]
        Analyzer[analyze_pending_media]
        Stats[refresh_stats]
        Cleanup[cleanup_sources]
    end

    subgraph Shared["Shared state"]
        Postgres[(Postgres or local SQLite)]
        Media[Cloudinary and R2 media]
    end

    Web --> API
    API --> Postgres
    API --> Media
    Jobs --> Postgres
    Jobs --> Media
```

### Upload, Generate, Schedule Flow

```mermaid
sequenceDiagram
    participant U as Creator
    participant W as Django Web App
    participant S as Cloudinary / R2
    participant G as Gemini
    participant D as Database

    U->>W: Upload media and brief
    alt small media
        W->>S: Store file and thumbnail
    else large video
        W->>S: Create presigned R2 upload URL
        U->>S: Upload directly from browser
    end
    W->>D: Create Video row
    W->>G: Generate platform metadata
    W->>D: Save AIContent rows
    U->>W: Pick account, caption, time, visibility
    W->>D: Create ScheduledPost row
```

### Publishing Flow

```mermaid
sequenceDiagram
    participant C as Cron
    participant P as publish_due_posts
    participant D as Database
    participant A as Platform API
    participant N as Notifications
    participant M as Media Storage

    C->>P: Run on schedule
    P->>D: Recover stuck PROCESSING posts
    P->>D: Query due PENDING posts
    P->>D: Atomically claim post as PROCESSING
    P->>A: Publish using encrypted OAuth token
    alt publish succeeds
        P->>D: Mark PUBLISHED and save platform id
        P->>N: Send success alert
        P->>M: Archive source when all posts are published
    else token expired or revoked
        P->>D: Mark NEEDS_RECONNECT
        P->>N: Send reconnect alert
    else transient or API failure
        P->>D: Retry with exponential backoff, then mark FAILED
        P->>N: Send failure alert when retries are exhausted
    end
```

### Data Model

```mermaid
erDiagram
    USER ||--o{ VIDEO : uploads
    USER ||--o{ SOCIAL_ACCOUNT : connects
    VIDEO ||--o{ AI_CONTENT : generates
    VIDEO ||--o{ SCHEDULED_POST : schedules
    SOCIAL_ACCOUNT ||--o{ SCHEDULED_POST : publishes_to
    AI_CONTENT ||--o{ SCHEDULED_POST : supplies_copy
    SCHEDULED_POST ||--o{ STAT_SNAPSHOT : records

    VIDEO {
        string media_type
        string file_url
        string thumbnail_url
        string r2_object_key
        text ai_media_analysis
        string ai_analysis_status
    }

    SOCIAL_ACCOUNT {
        string platform
        encrypted access_token
        encrypted refresh_token
        datetime token_expires_at
        string status
    }

    AI_CONTENT {
        string platform
        string generated_title
        text generated_description
        text generated_hashtags
        string generation_status
    }

    SCHEDULED_POST {
        datetime scheduled_time_utc
        string visibility
        string status
        string platform_post_id
        int retry_count
    }
```

## Tech Stack

Django 5.2, Neon Postgres, Cloudinary, Cloudflare R2, Google Gemini 2.5 Flash,
Render, GitHub Actions, WhiteNoise, Brevo or SMTP email, Telegram alerts, and
Fernet-encrypted OAuth tokens.

## Local Setup

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows; use source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env            # then fill in keys; SQLite is used until DATABASE_URL is set
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

## Build Phases

- [x] Phase 0 - Foundation: project, models, encrypted tokens, deploy config
- [x] Phase 1 - Upload and storage with Cloudinary/R2 plus dashboard
- [x] Phase 2 - AI metadata and media analysis with Gemini
- [x] Phase 3 - YouTube integration
- [x] Phase 4 - Scheduler with `publish_due_posts` and cron
- [x] Phase 5 - Instagram integration
- [x] Phase 6 - LinkedIn integration
- [x] Phase 7 - Analytics, storage cleanup, notifications, timezone UI
- [ ] Phase 8 - Optional React frontend

## Security Notes

- OAuth tokens are encrypted at rest with Fernet via a custom model field.
- Tokens are not exposed in admin screens and are not logged.
- Secrets come from environment variables, Render env vars, and GitHub Secrets.
- Large uploads go directly from the browser to R2 using short-lived presigned URLs.
