"""
Management command run by the GitHub Actions cron every 5 minutes.

It is intentionally thin — all logic lives in core.publishing.run() so it can be
unit-tested without the command layer. This file just invokes it and reports.

    python manage.py publish_due_posts
"""

from django.core.management.base import BaseCommand

from core import publishing


class Command(BaseCommand):
    help = "Publish all ScheduledPosts whose time has come."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would run without claiming or publishing.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            from django.utils import timezone
            from core.models import ScheduledPost

            due = ScheduledPost.objects.filter(
                status=ScheduledPost.Status.PENDING,
                scheduled_time_utc__lte=timezone.now(),
            ).count()
            self.stdout.write(f"[dry-run] {due} post(s) due now.")
            return

        summary = publishing.run()
        self.stdout.write(self.style.SUCCESS(f"Done: {summary}"))
