"""Shared HTTP retry/backoff helpers for `collectors/*.py` (issue #86).

Every collector in this package independently paces requests
(`min_request_interval`) and retries transient failures (timeouts, transport
errors, 429/5xx) with capped exponential backoff plus jitter. Before this
module existed, the backoff-delay formula itself (`_exponential_backoff`)
was copy-pasted byte-for-byte into `github.py`, `github_commit_authors.py`,
`github_profile.py`, `github_checks.py`, `jira.py`, `jira_comments.py`, and
`ponymail.py`. This module factors that one piece out, plus a second piece
those seven modules previously didn't share at all: classifying a
truncated/undecodable response *body* as transient.

## Why a decoded-body failure belongs here too

A real nightly run (issue #57) hit two live examples of a failure mode none
of those collectors treated as retryable:

- GitHub's GraphQL API returned a response that was cut off mid-string
  (`json.JSONDecodeError: Unterminated string ... char 219262`) -- the HTTP
  request itself succeeded (status 200, no transport-level exception), but
  the body it delivered wasn't valid JSON.
- Pony Mail's `mbox.lua` closed the connection before finishing the body
  (`httpx.RemoteProtocolError`: "peer closed connection without sending
  complete message body (incomplete chunked read)").

Both are exactly as transient as a 5xx -- retrying the request from scratch
routinely gets a complete body next time -- so both should get the same
backoff treatment 429/5xx already get, not propagate straight through
`CollectionError` (or, worse, an unhandled exception) with no retry at all.
`httpx.RemoteProtocolError` is already a subclass of `httpx.TransportError`
(verified against the pinned `httpx` version this project uses), so every
collector's existing `except httpx.TransportError` already retries it when
it surfaces while *reading* the response; `is_transient_body_error` below
is for the other half of that picture -- a response that was read
successfully but whose *body* doesn't decode -- which every collector here
previously let propagate uncaught from its `response.json()` call.

Each collector still owns its own retry loop (attempt counting, its own
per-run call-count budget where one exists, and its own decision about
which HTTP statuses are retryable/terminal/special-cased) -- only the delay
formula and the body-error classification are shared here.
"""

from __future__ import annotations

import json
import random

import httpx

# Identical in every collector this project has today -- kept as one pair of
# constants so a future tuning change is made once, not in seven places.
BACKOFF_BASE = 0.5
BACKOFF_CAP = 20.0

# A response body that failed to decode -- truncated mid-stream, cut off by
# a proxy, or otherwise malformed -- is a transient failure, not a
# permanent one (module docstring). Passed to a collector's own
# `_backoff(attempt, retry_after=None)` for the same exponential-backoff-
# with-jitter treatment 429/5xx already get.
TRANSIENT_BODY_ERRORS: tuple[type[Exception], ...] = (
    json.JSONDecodeError,
    httpx.RemoteProtocolError,
)


def exponential_backoff(
    attempt: int, base: float = BACKOFF_BASE, cap: float = BACKOFF_CAP
) -> float:
    """Exponential backoff with jitter, capped at `cap` seconds.

    `attempt` is 1-based (the first retry passes `1`). Identical formula to
    what every collector in this package previously defined as its own
    private `_exponential_backoff`.
    """
    exp = min(cap, base * (2 ** (attempt - 1)))
    return exp + random.uniform(0, exp * 0.25)


def is_transient_body_error(exc: BaseException) -> bool:
    """True if `exc` is a truncated/undecodable response body that should be
    retried exactly like a 5xx (issue #86)."""
    return isinstance(exc, TRANSIENT_BODY_ERRORS)
