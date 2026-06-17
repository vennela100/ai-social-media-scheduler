"""
Archive Cloudinary sources of fully-published videos to reclaim storage.

The scheduler already calls this each tick (publishing.run), but this command
lets you run it on demand or with a custom retention window — e.g. --days 0 to
archive everything eligible right now (handy for testing or a manual sweep).

    python manage.py cleanup_sources
    python manage.py cleanup_sources --days 0
    python manage.py cleanup_sources --dry-run
"""

import datetime as dt

from django.core.management.base import BaseCommand
from django.utils import timezone

from core import publishing
from core.models import Video


class Command(BaseCommand):
    help = "Delete the Cloudinary source of fully-published videos (keeps a thumbnail)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=publishing.SOURCE_RETENTION_DAYS,
            help="Only archive sources older than this many days (default from settings).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List which videos would be archived, without deleting anything.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if options["dry_run"]:
            cutoff = timezone.now() - dt.timedelta(days=days)
            eligible = [
                v for v in Video.objects.filter(source_deleted=False, uploaded_at__lt=cutoff)
                .exclude(cloudinary_public_id="")
                if v.is_fully_published()
            ]
            self.stdout.write(f"[dry-run] {len(eligible)} source(s) would be archived (>{days}d, fully published):")
            for v in eligible:
                self.stdout.write(f"  - video {v.pk}: {v}")
            return

        archived = publishing.cleanup_published_sources(retention_days=days)
        self.stdout.write(self.style.SUCCESS(f"Archived {archived} source(s)."))
