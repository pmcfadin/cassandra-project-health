"""Per-peer raw-table collection (issue #145).

Writes every peer's raw tables under `raw/peers/<id>/<table>/...` on the
data branch -- a namespace entirely separate from Cassandra's own
`raw/git/...`/`raw/github/...`/`raw/release/...` (issue #145's own
requirement: "Cassandra's own tables are untouched"). Achieved for free:
`project_health.storage.raw_table_dir`/`raw_partition_path` just join
`data_dir / "raw" / source / table` -- passing `source=f"peers/{peer.id}"`
needs no change to `storage.py` at all, since `pathlib.Path` handles the
embedded `/` the same as any other path segment. Watermarks use the same
trick against `storage.read_watermark`/`write_watermark`'s `source`
argument.

One GitHub GraphQL call (`peers.github.collect_peer_prs`) covers every
peer's PRs/reviews/comments in a single shared rate-limit budget (see that
module's docstring); this module splits the combined result by `repo`
before writing each peer's own partition. Git-commit collection and
release collection are genuinely per-peer (a separate clone/REST call
each), so `collect_peer_git`/`collect_peer_releases` loop one peer at a
time, with the git clone deleted immediately after each peer
(`peers.git_clone.cleanup`) -- issue #145's disk budget never holds more
than one peer's clone at a time.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa

from project_health.collectors.git import GitCollectionResult, GitCollector
from project_health.collectors.github import GitHubCollector
from project_health.collectors.reviewer_trailer import ReviewerExtractor
from project_health.config import BotPattern
from project_health.metrics.windows import add_months, month_start
from project_health.peers import git_clone
from project_health.peers.config import (
    DEFAULT_SETTLEMENT_FRESHNESS_DAYS,
    BotPatternConfig,
    PeerProject,
    PeersConfig,
)
from project_health.peers.github import (
    PassResult,
    PassState,
    build_peer_github_config,
    run_closed_search_pass,
    run_created_desc_pass,
    run_open_prs_pass,
)
from project_health.peers.release import (
    GaTag,
    VerificationResult,
    fetch_ga_tags,
    verify_release_counts,
)
from project_health.peers import rotation as peer_rotation
from project_health.schema import get_schema, validate
from project_health import storage


def _describe_exception(exc: Exception) -> str:
    """`str(exc)` plus a `subprocess.CalledProcessError`'s own captured
    stderr, when there is one -- `str(CalledProcessError)` alone (what a
    bare `str(exc)` gives) drops the actual git error text (e.g. "fatal:
    ..."), which is the one piece of information that actually explains a
    git-clone/log failure (real-run finding, 2026-10-09: a bare message
    read only "returned non-zero exit status 128," with no way to tell
    *why* without this)."""
    message = str(exc)
    if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
        stderr = exc.stderr.strip() if isinstance(exc.stderr, str) else exc.stderr
        message = f"{message}\nstderr: {stderr}"
    return message


def peer_source(peer_id: str, table_group: str) -> str:
    """The `storage.py` `source` argument for one of a peer's raw table
    groups, e.g. `peer_source("kafka", "git")` -> `"peers/kafka/git"`."""
    return f"peers/{peer_id}/{table_group}"


# --- GitHub PR/review/comment ------------------------------------------
#
# Three bounded-recency-window passes per repo (issue #145 fixup,
# orchestrator review of PR #147: the original single ASC-from-scratch
# `GitHubCollector.collect()` call reached only a peer's *oldest* PRs on a
# first-ever run, never anything recent) -- see `peers/github.py`'s module
# docstring for why each of `created_desc`/`open_prs`/`closed_search`
# exists. One shared `GitHubCollector` instance (one client, one token,
# one rate-limit-floor check) runs every peer's three passes in turn;
# once any pass hits the floor, every remaining (peer, pass) pair is
# recorded `'skipped'` with its watermark untouched, same "a shared budget
# means some work finishing cleanly beats everything finishing half
# finished" discipline `collectors/github.py`'s own module docstring
# documents for its multi-repo `collect()`.

PASS_NAMES: tuple[str, ...] = ("created_desc", "open_prs", "closed_search")

# 36-month display window + 1 month buffer (issue #145 fixup, orchestrator
# instruction) -- never the 12-month `contributor_absence_factor` buffer
# `git_clone.shallow_since_date`'s own lookback already adds; that buffer
# is about git-commit history for a *different* metric, not PR collection.
PR_WINDOW_MONTHS = 36
PR_WINDOW_BUFFER_MONTHS = 1


def pr_window_start(as_of: date) -> date:
    """First day of the month `PR_WINDOW_MONTHS + PR_WINDOW_BUFFER_MONTHS`
    before `as_of`'s last *completed* month (D5: never the in-progress
    current month) -- the `created_desc`/`closed_search` passes' stop
    bound."""
    last_completed_month = add_months(month_start(as_of), -1)
    return add_months(last_completed_month, -(PR_WINDOW_MONTHS + PR_WINDOW_BUFFER_MONTHS))


@dataclass(frozen=True)
class PeerGithubPassOutcome:
    peer_id: str
    pass_name: str
    status: str
    pr_count: int
    review_count: int
    comment_count: int
    pages_fetched: int
    issue_count: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class PeerGithubCollectionReport:
    per_peer_pr_counts: dict[str, int]
    pass_outcomes: list[PeerGithubPassOutcome]
    # Issue #148: this run's rotation offset and the resulting peer
    # processing order -- "deterministic and recorded in the run
    # manifest/log" (issue acceptance). `peer_order[0]` is whichever peer
    # this run started with, not necessarily `peers_config.peers[0]`.
    rotation_start_index: int = 0
    peer_order: list[str] = field(default_factory=list)
    # Peers whose three passes' persisted watermarks showed no resumable
    # work outstanding *before* this run started -- these get a minimal
    # single-page freshness check each pass instead of a fair-share slice
    # of this run's page budget (issue #148: "peers whose passes are all
    # complete ... are skipped cheaply").
    settled_peers: list[str] = field(default_factory=list)

    @property
    def overall_status(self) -> str:
        statuses = {o.status for o in self.pass_outcomes}
        if statuses <= {"completed"}:
            return "ok"
        # Any non-'completed' pass (budget-floor-limited, page-capped for
        # fairness, skipped because an earlier pass hit the real floor, or
        # a hard failure) means resumable work remains -- never silently
        # "ok" (same "a clean, resumable partial backfill is not the same
        # thing as a failure" distinction `collectors/github.py`'s own
        # `GitHubCollectionResult.status` docstring draws).
        return "partial"


def _pass_watermark_table(pass_name: str) -> str:
    return f"pr_pass_{pass_name}"


def _pass_settled(
    pass_name: str,
    state: PassState,
    *,
    freshness_days: int | None,
    now: datetime,
) -> bool:
    """One pass's own settledness rule, given its already-loaded `state`
    (issue #150 fixup -- see `peer_pass_settlement`'s module comment for
    the bug this replaces).

    `state.last_completed_at` is the sticky signal: once set, it is never
    cleared by a later partial/page-capped run of the same pass (`peers/
    github.py`'s own `PassState` docstring, `collect_and_write_github`'s
    `_stamp_last_completed_at`) -- so a pass that completed once stays
    settled through any number of subsequent in-progress runs, bounded
    only by `freshness_days` (`None` disables the bound -- sticky forever).

    A watermark written before this field existed has `last_completed_at
    is None` -- read as *unknown*, not *never*, and resolved by falling
    back to the exact pre-fix rule (`cursor is None`, plus `high_watermark`
    for `created_desc`) with no freshness bound at all, since there is no
    timestamp to bound it by ("absent field + cursor None = completed as
    today," this issue's own backward-compatibility requirement). The very
    next run that gives this (peer, pass) its turn stamps a real
    `last_completed_at` (`_stamp_last_completed_at`), after which this
    fallback never applies to it again.
    """
    if state.last_completed_at is not None:
        if freshness_days is None:
            return True
        completed_at = datetime.fromisoformat(state.last_completed_at)
        return now - completed_at <= timedelta(days=freshness_days)
    if state.cursor is not None:
        return False
    if pass_name == "created_desc" and state.high_watermark is None:
        return False
    return True


def peer_pass_settlement(
    data_dir: str | Path,
    peer: PeerProject,
    *,
    freshness_days: int | None = DEFAULT_SETTLEMENT_FRESHNESS_DAYS,
    now: datetime | None = None,
) -> dict[str, bool]:
    """Per-(peer, pass) settledness, keyed by `PASS_NAMES` (issue #150: the
    `/peers/` page needs to know, pass by pass, which of a peer's
    GitHub-derived metrics are safe to render -- `created_desc` reached the
    comparison window start gates Time to First Response, `closed_search`
    completing gates Change Request Closure Ratio, `open_prs` completing
    gates the Open PR backlog).

    ## The bug this fixes (issue #150 fixup, live backfill finding
    2026-10-13)

    The original rule treated `PassState.cursor is not None` as
    unconditionally "unsettled" -- correct for a pass that has *never*
    completed, but wrong for one that completed once and is simply
    mid-*another* refresh: `open_prs`/`closed_search` are redone in full
    every run (`peers/github.py`'s own module docstring, "cheap to redo"),
    and a page-capped or rate-limited *redo* leaves `cursor` non-null even
    though a perfectly good, complete snapshot from an earlier run already
    exists. On CI's page-capped budget, some peer is mid-refresh on
    essentially every run -- real backfill evidence (30 hourly passes):
    `open_prs` settlement for datafusion/flink/kafka/pulsar/spark flipped
    between `true`/`false` on almost every single pass, with no two
    consecutive passes agreeing on which peers were "settled." Since
    `/peers/` gates Change Request Closure Ratio on `closed_search` and
    Open PR backlog on `open_prs`, this hid those two metrics for whichever
    peers happened to be mid-refresh on any given run -- not because their
    data was actually stale, but because this function conflated "a refresh
    is in progress" with "no good data exists."

    Fix: settlement is now **sticky** -- it looks at whether the pass has
    *ever* completed (`state.last_completed_at`, stamped by
    `collect_and_write_github`'s `_stamp_last_completed_at` the moment any
    run of that pass finishes with `status == 'completed'`), not whether
    it happens to be mid-walk *right now*. A refresh in progress never
    unsettles a pass that already has a valid completed snapshot. Bounded
    by `freshness_days` (`projects/peers.yaml`'s `collection.
    settlement_freshness_days`, default 14) so a pass that's gone stale for
    that long -- no completed run in that whole window -- does eventually
    fall back to "Collecting," rather than showing an arbitrarily old
    snapshot forever; `None` disables the bound.

    A pass whose watermark was never written at all (`storage.
    read_watermark` returns `None` -- this peer's turn never got far enough
    for this pass to run even once) is **never** "settled" regardless of
    any of the above -- it has produced zero rows, the opposite of "safe to
    show a number for." See `_pass_settled` for the exact per-pass rule
    once a watermark does exist.
    """
    source = peer_source(peer.id, "github")
    now = now or datetime.now(timezone.utc)
    settlement: dict[str, bool] = {}
    for pass_name in PASS_NAMES:
        raw = storage.read_watermark(data_dir, source, table=_pass_watermark_table(pass_name))
        if raw is None:
            settlement[pass_name] = False
            continue
        state = PassState.from_json(raw)
        settlement[pass_name] = _pass_settled(
            pass_name, state, freshness_days=freshness_days, now=now
        )
    return settlement


def peer_pass_last_completed_at(data_dir: str | Path, peer: PeerProject) -> dict[str, str | None]:
    """Per-(peer, pass) `PassState.last_completed_at`, keyed by
    `PASS_NAMES` (issue #150 fixup) -- the raw ISO-8601 timestamp each
    pass's watermark currently carries, `None` if that pass's watermark
    was never written, never written with a `last_completed_at` at all
    (a legacy, pre-fix watermark this run hasn't touched yet), or never
    actually completed. Purely informational -- `peer_pass_settlement`,
    not this function, is what gates a metric; this is what lets
    `peers.pipeline`/`site.peers_page` show *when* a gated pass last
    produced the snapshot a reader is (or isn't) currently seeing."""
    source = peer_source(peer.id, "github")
    last_completed_at: dict[str, str | None] = {}
    for pass_name in PASS_NAMES:
        raw = storage.read_watermark(data_dir, source, table=_pass_watermark_table(pass_name))
        last_completed_at[pass_name] = PassState.from_json(raw).last_completed_at if raw else None
    return last_completed_at


def _peer_is_settled(
    data_dir: str | Path,
    peer: PeerProject,
    *,
    freshness_days: int | None = DEFAULT_SETTLEMENT_FRESHNESS_DAYS,
    now: datetime | None = None,
) -> bool:
    """`True` if every one of `peer`'s three passes' persisted watermarks
    show no resumable work outstanding (issue #148: "peers whose passes
    are all complete ... are skipped cheaply") -- `all(peer_pass_settlement
    (...).values())`, see that function's own docstring for the exact
    per-pass rule."""
    settlement = peer_pass_settlement(data_dir, peer, freshness_days=freshness_days, now=now)
    return all(settlement.values())


def _stamp_last_completed_at(
    pass_name: str, raw_before: str | None, result: PassResult, now: datetime
) -> PassState:
    """Attach `last_completed_at` to `result.next_state` before it's
    persisted (issue #150 fixup) -- the one piece of sticky-settlement
    bookkeeping the three `run_*_pass` functions in `peers/github.py`
    themselves know nothing about (they have no `now`, and no concept of
    "settled" at all; `PassState`'s own docstring).

    - `status == 'completed'`: stamp `now` -- a fresh, real completion.
    - `status in ('partial', 'page_capped')`: carry forward whichever
      `last_completed_at` this (peer, pass) already had (`raw_before`'s own
      `PassState`, if any) -- this is the sticky half of the fix: a refresh
      currently in progress must never erase the fact that an earlier run
      already completed this pass once.
    - Otherwise (`'rate_limited'`/`'failed'`): `result.next_state` already
      *is* the untouched input `state` (`run_*_pass`'s own contract,
      "leave state untouched, retry next run") -- nothing to stamp, it
      already carries whatever `last_completed_at` it had.

    One more case the "carry forward" branch handles, deliberately:
    `raw_before` is a **legacy** watermark -- written before this field
    existed, so `last_completed_at` is `None` on it even though `cursor is
    None` (plus, for `created_desc`, `high_watermark` set) already meant
    "completed" under the pre-fix rule. The *first* time collection gives
    that (peer, pass) its turn again, and that turn doesn't itself finish
    (`'partial'`/`'page_capped'`), this stamps `now` rather than leaving
    `last_completed_at` as `None` -- "absent field + cursor None =
    completed as today," this issue's own backward-compatibility
    requirement, turned into this fix's own real, persisted timestamp
    instead of staying an inference forever. Deliberately **not** applied
    when `raw_before is None` (this pass has never actually run before at
    all) -- an empty `PassState()` is indistinguishable from a legacy
    "completed" one by its fields alone, and only `raw_before is None`
    (no watermark key ever written) tells the two apart; a genuinely
    first-ever run that goes partial must stay unsettled, not sticky-settled
    from a completion that never happened.
    """
    if result.status == "completed":
        return replace(result.next_state, last_completed_at=now.isoformat())
    if result.status not in ("partial", "page_capped"):
        return result.next_state

    inherited = result.next_state.last_completed_at
    if inherited is None and raw_before is not None:
        state_before = PassState.from_json(raw_before)
        if state_before.last_completed_at is not None:
            inherited = state_before.last_completed_at
        elif state_before.cursor is None and (
            pass_name != "created_desc" or state_before.high_watermark is not None
        ):
            inherited = now.isoformat()
    return replace(result.next_state, last_completed_at=inherited)


def _recently_completed(
    state: PassState, now: datetime, skip_rerun_within_hours: int | None
) -> bool:
    """`True` if `state.last_completed_at` is set and within
    `skip_rerun_within_hours` of `now` (issue #150 fixup's "reduce waste"
    half) -- `collect_and_write_github` skips this (peer, pass)'s turn
    entirely (no API call, no page spent) when this is `True`, instead of
    redoing a pass that just finished. `skip_rerun_within_hours=None`
    disables this entirely (always re-run)."""
    if not skip_rerun_within_hours or state.last_completed_at is None:
        return False
    completed_at = datetime.fromisoformat(state.last_completed_at)
    return now - completed_at < timedelta(hours=skip_rerun_within_hours)


# A fully "settled" peer (module comment above) only needs a minimal
# freshness check each pass this run, never a fair-share slice of the page
# budget -- `run_created_desc_pass` restarts from the top every run
# regardless, so one page is enough to tell whether anything's new since
# `high_watermark`, and `open_prs`/`closed_search` are already cheap to
# redo in full once completed (their own docstrings).
_SETTLED_PEER_MAX_PAGES = 1


def collect_and_write_github(
    peers_config: PeersConfig,
    data_dir: str | Path,
    run_id: str,
    partition_date: date,
    *,
    token: str | None = None,
    as_of: date | None = None,
    transport=None,
    rotation_start_index: int = 0,
    now: datetime | None = None,
) -> PeerGithubCollectionReport:
    """Run every peer repo's three PR-collection passes (module comment
    above), sharing one `GitHubCollector`/one rate-limit-floor budget, then
    write each peer's own `raw/peers/<id>/github/{pr,pr_review,pr_comment}`
    partition (one combined write per table, the three passes' rows
    concatenated -- any cross-pass overlap, e.g. a PR both created in the
    window and still open, is resolved at *read* time by `peers.pipeline`'s
    existing `_dedupe_pr_rows`/etc., the same way a re-fetched PR's
    cross-*run* overlap already is) and persist each pass's own resumable
    `PassState` (`storage.write_watermark`, `table=_pass_watermark_table
    (pass_name)`), stamped with sticky-settlement's own `last_completed_at`
    (`_stamp_last_completed_at`, issue #150 fixup).

    Issue #148 -- two additional fairness layers, both on top of (never
    replacing) the existing per-(peer, pass) `max_pages_per_pass` ceiling
    and the real `rate_limit_floor` stop:

    - `rotation_start_index` (`peers.rotation`, caller-persisted across
      runs): peers are processed starting at this offset into
      `peers_config.peers`, wrapping around, instead of always starting at
      index 0 -- the fix for the real starvation this issue reports
      (kafka/spark always going first meant flink/pulsar/datafusion never
      got a turn across several real runs).
    - A per-run shared page budget (`collection.github_page_budget_per_run`)
      is split across whichever peers aren't already "done" this run
      (`_peer_is_settled`) -- `remaining_budget // peers_not_done`,
      recomputed before each not-done peer's turn, so a peer that finishes
      early (uses fewer pages than its share) leaves more of the shared
      budget for whichever peers haven't had their turn yet.

    Issue #150 fixup -- a third fairness/waste layer, `skip_rerun_within_
    hours` (`collection.skip_rerun_within_hours`): a (peer, pass) whose
    `last_completed_at` is recent enough is skipped outright this run, no
    API call and no page spent (`_recently_completed`) -- freeing this
    run's shared page budget for whichever peer/pass hasn't caught up yet,
    `created_desc` backfills especially, instead of redoing a pass that's
    "cheap to redo" but was never actually free.
    """
    as_of = as_of or datetime.now(timezone.utc).date()
    window_start = pr_window_start(as_of)
    now = now or datetime.now(timezone.utc)
    freshness_days = peers_config.collection.settlement_freshness_days
    skip_rerun_within_hours = peers_config.collection.skip_rerun_within_hours

    config = build_peer_github_config(peers_config.peers, peers_config.collection.bot_patterns)
    pass_outcomes: list[PeerGithubPassOutcome] = []
    per_peer_pr_counts: dict[str, int] = {}
    budget_exhausted = False

    ordered_peers = peer_rotation.rotate(peers_config.peers, rotation_start_index)
    configured_max_pages = peers_config.collection.max_pages_per_pass
    settled_by_peer = {
        peer.id: _peer_is_settled(data_dir, peer, freshness_days=freshness_days, now=now)
        for peer in ordered_peers
    }
    settled_peers = [peer.id for peer in ordered_peers if settled_by_peer[peer.id]]
    peers_not_done_remaining = sum(1 for peer in ordered_peers if not settled_by_peer[peer.id])
    page_budget_remaining = peers_config.collection.github_page_budget_per_run

    with GitHubCollector(
        config,
        token=token,
        rate_limit_floor=peers_config.collection.github_rate_limit_floor,
        rate_limit_floor_fraction=peers_config.collection.github_rate_limit_floor_fraction,
        transport=transport,
    ) as collector:
        for peer in ordered_peers:
            source = peer_source(peer.id, "github")
            snapshot_id = str(uuid.uuid4())
            bot_pattern_objs = [
                BotPattern(field=p.field, regex=p.regex)
                for p in peers_config.collection.bot_patterns
            ]

            pr_rows: list[dict] = []
            review_rows: list[dict] = []
            comment_rows: list[dict] = []
            pr_count_this_peer = 0
            pages_used_this_peer = 0

            if settled_by_peer[peer.id]:
                peer_max_pages = min(configured_max_pages, _SETTLED_PEER_MAX_PAGES)
            else:
                fair_share = max(1, page_budget_remaining // peers_not_done_remaining)
                peer_max_pages = min(configured_max_pages, fair_share)

            for pass_name in PASS_NAMES:
                if budget_exhausted:
                    pass_outcomes.append(
                        PeerGithubPassOutcome(
                            peer_id=peer.id,
                            pass_name=pass_name,
                            status="skipped",
                            pr_count=0,
                            review_count=0,
                            comment_count=0,
                            pages_fetched=0,
                            error="rate limit budget exhausted earlier this run",
                        )
                    )
                    continue

                raw_before = storage.read_watermark(
                    data_dir, source, table=_pass_watermark_table(pass_name)
                )
                state = PassState.from_json(raw_before)

                if _recently_completed(state, now, skip_rerun_within_hours):
                    # Issue #150 fixup: already completed within the
                    # configured freshness/skip window -- no API call, no
                    # page spent, watermark left exactly as it is (already
                    # settled, nothing to carry forward).
                    pass_outcomes.append(
                        PeerGithubPassOutcome(
                            peer_id=peer.id,
                            pass_name=pass_name,
                            status="completed",
                            pr_count=0,
                            review_count=0,
                            comment_count=0,
                            pages_fetched=0,
                            error=None,
                        )
                    )
                    continue

                max_pages = peer_max_pages
                if pass_name == "created_desc":
                    result = run_created_desc_pass(
                        collector,
                        peer.repo,
                        state,
                        window_start,
                        bot_pattern_objs,
                        snapshot_id,
                        max_pages=max_pages,
                    )
                elif pass_name == "open_prs":
                    result = run_open_prs_pass(
                        collector,
                        peer.repo,
                        state,
                        bot_pattern_objs,
                        snapshot_id,
                        max_pages=max_pages,
                    )
                else:
                    result = run_closed_search_pass(
                        collector,
                        peer.repo,
                        state,
                        window_start,
                        bot_pattern_objs,
                        snapshot_id,
                        max_pages=max_pages,
                    )

                stamped_state = _stamp_last_completed_at(pass_name, raw_before, result, now)
                storage.write_watermark(
                    data_dir,
                    source,
                    stamped_state.to_json(),
                    table=_pass_watermark_table(pass_name),
                )
                pr_rows.extend(result.pr_rows)
                review_rows.extend(result.review_rows)
                comment_rows.extend(result.comment_rows)
                pr_count_this_peer += len(result.pr_rows)
                pages_used_this_peer += result.pages_fetched

                pass_outcomes.append(
                    PeerGithubPassOutcome(
                        peer_id=peer.id,
                        pass_name=pass_name,
                        status=result.status,
                        pr_count=len(result.pr_rows),
                        review_count=len(result.review_rows),
                        comment_count=len(result.comment_rows),
                        pages_fetched=result.pages_fetched,
                        issue_count=result.issue_count,
                        error=result.error,
                    )
                )
                if result.status in ("partial", "rate_limited"):
                    budget_exhausted = True

            per_peer_pr_counts[peer.id] = pr_count_this_peer
            # Issue #148 fair-share recompute: deduct what this peer
            # actually used (never more than `page_budget_remaining`, so a
            # last, over-budget peer can't drive the pool negative) and,
            # for a peer that wasn't already settled, shrink the not-done
            # count -- a peer that finished early (used fewer pages than
            # its fair share) leaves more of `page_budget_remaining` for
            # whichever not-done peers haven't had their turn yet.
            page_budget_remaining = max(page_budget_remaining - pages_used_this_peer, 0)
            if not settled_by_peer[peer.id]:
                peers_not_done_remaining = max(peers_not_done_remaining - 1, 0)

            if pr_rows:
                pr_table = pa.Table.from_pylist(pr_rows, schema=get_schema("pr"))
                storage.write_partition(data_dir, source, "pr", partition_date, run_id, pr_table)
            if review_rows:
                review_table = pa.Table.from_pylist(review_rows, schema=get_schema("pr_review"))
                storage.write_partition(
                    data_dir, source, "pr_review", partition_date, run_id, review_table
                )
            if comment_rows:
                comment_table = pa.Table.from_pylist(comment_rows, schema=get_schema("pr_comment"))
                storage.write_partition(
                    data_dir, source, "pr_comment", partition_date, run_id, comment_table
                )

    return PeerGithubCollectionReport(
        per_peer_pr_counts=per_peer_pr_counts,
        pass_outcomes=pass_outcomes,
        rotation_start_index=rotation_start_index,
        peer_order=[peer.id for peer in ordered_peers],
        settled_peers=settled_peers,
    )


# --- git commits ---------------------------------------------------------


@dataclass(frozen=True)
class PeerGitCollectionReport:
    peer_id: str
    commits_collected: int
    clone_bytes: int
    error: str | None = None


def collect_and_write_git(
    peer: PeerProject,
    commit_lookback_months: int,
    data_dir: str | Path,
    workdir: str | Path,
    run_id: str,
    partition_date: date,
    bot_patterns: list[BotPatternConfig],
) -> PeerGitCollectionReport:
    """Shallow bare clone -> `GitCollector.collect()` -> write
    `raw/peers/<id>/git/contribution_event` -> delete the clone. Never
    raises on a clone/collection failure -- returns a report with `error`
    set so one peer's failure doesn't abort the others (`pipeline.py`'s
    per-peer loop)."""
    clone_path = Path(workdir) / f"{peer.id}-clone"
    git_clone.cleanup(clone_path)  # defensive: never start atop a stale clone

    now = git_clone.utc_now()
    shallow_since = git_clone.shallow_since_date(now, commit_lookback_months)
    try:
        git_clone.clone_shallow_bare(
            git_clone.github_clone_url(peer.owner, peer.name),
            clone_path,
            branch=peer.default_branch,
            shallow_since=shallow_since,
        )
    except Exception as exc:  # noqa: BLE001 - reported, never propagated
        git_clone.cleanup(clone_path)
        return PeerGitCollectionReport(
            peer_id=peer.id, commits_collected=0, clone_bytes=0, error=_describe_exception(exc)
        )

    clone_bytes = git_clone.du_bytes(clone_path)
    snapshot_id = str(uuid.uuid4())
    source = peer_source(peer.id, "git")
    watermark = storage.read_watermark(data_dir, source)

    try:
        collector = GitCollector(ReviewerExtractor())
        bot_pattern_objs = [BotPattern(field=p.field, regex=p.regex) for p in bot_patterns]
        result: GitCollectionResult = collector.collect(
            repo_path=clone_path,
            repo_label=peer.repo,
            default_branch=peer.default_branch,
            watermark=watermark,
            bot_patterns=bot_pattern_objs,
            source_snapshot_id=snapshot_id,
        )
    except Exception as exc:  # noqa: BLE001
        git_clone.cleanup(clone_path)
        return PeerGitCollectionReport(
            peer_id=peer.id,
            commits_collected=0,
            clone_bytes=clone_bytes,
            error=_describe_exception(exc),
        )

    git_clone.cleanup(clone_path)

    if result.contribution_event.num_rows:
        storage.write_partition(
            data_dir,
            source,
            "contribution_event",
            partition_date,
            run_id,
            result.contribution_event,
        )
    storage.write_watermark(data_dir, source, result.next_watermark)

    return PeerGitCollectionReport(
        peer_id=peer.id,
        commits_collected=result.commits_collected,
        clone_bytes=clone_bytes,
    )


# --- releases --------------------------------------------------------------


@dataclass(frozen=True)
class PeerReleaseCollectionReport:
    peer_id: str
    release_count: int
    verification: VerificationResult


_RELEASE_SCHEMA = get_schema("release")


def _ga_tags_to_release_rows(tags: list[GaTag], repo: str, snapshot_id: str) -> pa.Table:
    collected_at = datetime.now(timezone.utc)
    rows = [
        {
            "release_id": tag.tag_name,
            "tag_name": tag.tag_name,
            "version": tag.version,
            "major_minor": tag.major_minor,
            "release_date": tag.release_date,
            "release_date_source": "git_tag",
            # Peer release discovery has no archive-existence cross-check
            # of its own (module docstring: `verify_release_counts` is a
            # separate per-year count comparison, not a per-release flag).
            "archive_verified": None,
            "archive_date": None,
            "repo": repo,
            "source_snapshot_id": snapshot_id,
            "collected_at": collected_at,
        }
        for tag in tags
    ]
    if not rows:
        return _RELEASE_SCHEMA.empty_table()
    return validate("release", pa.Table.from_pylist(rows, schema=_RELEASE_SCHEMA))


def collect_and_write_releases(
    peer: PeerProject,
    data_dir: str | Path,
    run_id: str,
    partition_date: date,
    *,
    token: str | None = None,
) -> PeerReleaseCollectionReport:
    """GA tags via the REST Tags API (`peers.release.fetch_ga_tags`, no
    clone needed) -> write `raw/peers/<id>/release/release` -> verify the
    resulting per-year counts against `peer.release_verification`'s
    independent source."""
    tags = fetch_ga_tags(peer.owner, peer.name, peer.tag_prefix, token=token)
    snapshot_id = str(uuid.uuid4())
    table = _ga_tags_to_release_rows(tags, peer.repo, snapshot_id)

    source = peer_source(peer.id, "release")
    if table.num_rows:
        storage.write_partition(data_dir, source, "release", partition_date, run_id, table)

    verification = verify_release_counts(peer, [tag.release_date for tag in tags], token=token)
    return PeerReleaseCollectionReport(
        peer_id=peer.id, release_count=len(tags), verification=verification
    )
