"""Newcomer determination for the private Cassandra communication run
(issue #114; COMMUNITY-HEALTH.md §2.3 rule 8, §5.2's newcomer response
rates).

"An author is a newcomer in a venue at message time if they have <N prior
messages in that venue across the *whole* Phase-1 metadata... not just the
sample" (issue #114, build item 3). `frame.load_dev_author_history`/
`load_jira_author_history` supply that whole-history timeline per raw
author, keyed by venue; this module answers, for one message's author and
`posted_at`, whether fewer than `N` of that author's *other* messages in
this venue happened strictly before it.
"""

from __future__ import annotations

import bisect
from datetime import datetime
from typing import Any

DEFAULT_NEWCOMER_N = 3


def _as_datetime(value: Any) -> datetime:
    """Normalize `value` (a `datetime`, already what `frame.
    load_dev_author_history`/`load_jira_author_history` store, or an
    ISO-8601-ish string, what `runner.py`'s `_PendingMessage.posted_at`
    stores for both venues -- a plain `.isoformat()` datetime for dev@, the
    raw JIRA `created` timestamp for JIRA, both of which Python's
    `datetime.fromisoformat` parses on 3.12+) into a `datetime` so the two
    can be compared regardless of which venue/side produced them."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def prior_message_count(history: dict[str, list[Any]], raw_author: str, before: Any) -> int:
    """How many of `raw_author`'s timestamps in `history` fall strictly
    before `before`. `0` if the author has no history at all (a first-ever
    message -- the strongest possible newcomer signal)."""
    timestamps = history.get(raw_author)
    if not timestamps:
        return 0
    return bisect.bisect_left(timestamps, _as_datetime(before))


def is_newcomer(
    history: dict[str, list[Any]],
    raw_author: str,
    at_time: Any,
    *,
    n: int = DEFAULT_NEWCOMER_N,
) -> bool:
    """Whether `raw_author` was a newcomer in this venue at `at_time`: fewer
    than `n` prior messages in the whole Phase-1 metadata for this venue,
    strictly before `at_time`."""
    if not raw_author:
        return False
    return prior_message_count(history, raw_author, at_time) < n
