"""Request middleware for the core app.

TimezoneMiddleware activates each visitor's local timezone so that all
template datetimes render in their wall-clock time and naive form input is
interpreted as local. We still STORE everything in UTC (USE_TZ=True) — this
only affects display and form parsing, never the database.

The timezone is detected client-side (browser `Intl` API) and sent up in a
`tz` cookie; see the small script in templates/base.html. We validate it
against the system tz database before trusting it, and fall back to UTC
(the project default) on anything unexpected.
"""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone


class TimezoneMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tzname = request.COOKIES.get("tz")
        if tzname:
            try:
                timezone.activate(ZoneInfo(tzname))
            except (ZoneInfoNotFoundError, ValueError, KeyError):
                # Unknown/garbage cookie value — render in UTC rather than 500.
                timezone.deactivate()
        else:
            timezone.deactivate()
        return self.get_response(request)
