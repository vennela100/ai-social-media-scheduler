"""Dashboard analytics helpers.

These turn a user's ScheduledPost rows into the numbers shown on the dashboard.
We compute the raw status counts here (cheaply, in one grouped query at the
call site) and expose a couple of small reductions over them.
"""

from .models import ScheduledPost

S = ScheduledPost.Status

# Which statuses mean "this post still has a future" vs "needs a human".
QUEUED_STATUSES = (S.PENDING, S.PROCESSING)
ATTENTION_STATUSES = (S.FAILED, S.NEEDS_RECONNECT)


def summarize(counts: dict[str, int]) -> dict[str, int]:
    """Roll per-status counts into the four headline dashboard numbers.

    `counts` maps a status value (e.g. "published") to how many posts have it.
    Missing statuses are treated as zero.
    """
    return {
        "published": counts.get(S.PUBLISHED, 0),
        "queued": sum(counts.get(s, 0) for s in QUEUED_STATUSES),
        "attention": sum(counts.get(s, 0) for s in ATTENTION_STATUSES),
        "total": sum(counts.values()),
    }


def success_rate(counts: dict[str, int]) -> float | None:
    """Return the publish success rate as a fraction in [0.0, 1.0], or None.

    `counts` maps a status value to a number of posts (same shape as the dict
    passed to summarize()). The available statuses are:
        S.PUBLISHED, S.PENDING, S.PROCESSING, S.FAILED, S.NEEDS_RECONNECT

    This metric is genuinely a product decision, which is why it's yours to make:

      * What's the DENOMINATOR? Counting only *terminal* posts
        (published + failed) answers "of the posts that finished, how many
        succeeded?" — a stable reliability number. Counting *all* posts
        (including pending/processing) drags the rate down just because work is
        still queued, which can look alarming on a fresh account.
      * Does NEEDS_RECONNECT count as a failure, or is it excluded as "user
        action pending, not the system's fault"?
      * What should an empty account return? A rate over zero posts is
        undefined — returning None lets the template show a friendly "—"
        instead of a misleading 0% or a divide-by-zero crash.

    TODO(you): implement this. Roughly:
        published = counts.get(S.PUBLISHED, 0)
        denominator = <the set of statuses you decide count>
        return None if denominator == 0 else published / denominator

    Until you do, the dashboard guards the NotImplementedError and shows "—".
    """
    raise NotImplementedError("Implement success_rate() — see the docstring.")
