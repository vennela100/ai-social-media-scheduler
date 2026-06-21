"""
Pull fresh engagement stats (views/likes/comments) for all published posts.

Meant to run on the same GitHub Actions cron as publish_due_posts so the
dashboard numbers stay current without anyone clicking "Refresh":

    python manage.py refresh_stats
    python manage.py refresh_stats --force   # ignore the staleness window
"""

from django.core.management.base import BaseCommand

from core import analytics


class Command(BaseCommand):
    help = "Refresh post-publish engagement stats for every published post."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-fetch even posts refreshed within the staleness window.",
        )

    def handle(self, *args, **options):
        result = analytics.refresh_all(force=options["force"])
        self.stdout.write(self.style.SUCCESS(
            f"Refreshed {result['updated']} of {result['considered']} published post(s)."
        ))
