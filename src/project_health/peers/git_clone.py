"""Disk-safe peer git acquisition (issue #145).

`collectors.git.clone_or_fetch` is Cassandra's own full-history, persistent
clone (cached across nightly runs, issue #4) -- deliberately NOT reused
here. Issue #145's own constraint: the collection machine has ~3.5 GB free,
so a peer clone must be a **bare, blobless, shallow-since** clone of the
peer's default branch only, and it must be deleted again as soon as this
run's commit walk (`collectors.git.GitCollector.collect`) is done with it --
never kept around the way Cassandra's own clone is.

`GitCollector.collect` reads a local clone via `git log`/`git rev-parse`
only (never a checkout), so a bare clone works unmodified -- its
`_resolve_ref` helper already documents trying the branch name directly
first specifically because "a bare clone... refs live at refs/heads/<branch>
directly."

`--shallow-since=<N months before the run>` bounds the walk to the commit
history `peer_metrics.compute_peer_metrics` can actually use (36 months of
display window + 12 months of `contributor_absence_factor`'s own trailing
window before the first displayed month, `peers.yaml`'s
`collection.commit_lookback_months`, default 48) -- never the whole
project's history, which for a project as large as apache/spark would blow
the disk budget outright.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from project_health.collectors.retry import exponential_backoff

# Real-run finding (2026-10-09): apache/datafusion's own clone failed twice
# in a row with a transient network error ("Recv failure: Operation timed
# out") -- a plain network hiccup, not anything about that repo in
# particular. `clone_shallow_bare` retries a handful of times with the same
# backoff every other collector in this package already uses
# (`collectors.retry.exponential_backoff`) before giving up.
DEFAULT_CLONE_MAX_RETRIES = 3


def github_clone_url(owner: str, name: str) -> str:
    return f"https://github.com/{owner}/{name}.git"


def shallow_since_date(as_of: datetime, lookback_months: int) -> str:
    """`YYYY-MM-DD` string `lookback_months` before `as_of`, for
    `git clone --shallow-since=`. A flat 30-day/month approximation --
    `--shallow-since` only needs to bound the walk generously, not land on
    an exact calendar boundary; a few extra days of history is harmless."""
    cutoff = as_of - timedelta(days=lookback_months * 30)
    return cutoff.date().isoformat()


def clone_shallow_bare(
    remote_url: str,
    local_path: str | Path,
    *,
    branch: str,
    shallow_since: str,
    max_retries: int = DEFAULT_CLONE_MAX_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """`git clone --bare --filter=blob:none --single-branch --branch
    <branch> --shallow-since=<shallow_since> <remote_url> <local_path>`.

    `--single-branch` (only `branch`'s history, not every ref) and
    `--filter=blob:none` (trees/commits only, no file contents) combine
    with `--shallow-since` to keep this clone's disk footprint small
    regardless of the peer repo's overall size. Retries up to `max_retries`
    times on failure (module docstring: a real run saw a transient network
    timeout, not anything repo-specific), clearing any partial clone
    directory between attempts; raises `subprocess.CalledProcessError` only
    once every attempt has failed -- callers decide whether a failed clone
    for one peer should stop the whole run or just skip that peer (see
    `peers.pipeline`).
    """
    local_path = Path(local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "git",
        "clone",
        "--bare",
        "--filter=blob:none",
        "--single-branch",
        "--branch",
        branch,
        f"--shallow-since={shallow_since}",
        remote_url,
        str(local_path),
    ]

    attempt = 0
    while True:
        attempt += 1
        try:
            subprocess.run(args, check=True, capture_output=True, text=True)
            return
        except subprocess.CalledProcessError:
            cleanup(local_path)
            if attempt >= max_retries:
                raise
            sleep_fn(exponential_backoff(attempt))


def cleanup(local_path: str | Path) -> None:
    """Best-effort removal of a peer clone -- never raises (issue #145:
    "delete each clone before the next"; a cleanup failure must not abort
    collection of the peers still to come)."""
    shutil.rmtree(Path(local_path), ignore_errors=True)


def du_bytes(path: str | Path) -> int:
    """Total on-disk size of `path` (files only, following no symlinks) --
    used by the real-run report's "peak disk used" figure."""
    path = Path(path)
    if not path.exists():
        return 0
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            total += entry.stat().st_size
    return total


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
