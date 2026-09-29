"""Tests for project_health.metrics.review_responsiveness (issue #102).

Covers tier assignment, author reconstruction (including the documented
divergence from the one-off `blend.py` reference script's `from_value is
None` fallback bug -- see `scripts/verify_review_responsiveness.py`'s module
docstring), right-censoring denominators, bot/author exclusion, and
first-response source attribution.

Per-submission correctness (author reconstruction, first-response source,
exclusions) is checked by calling `_build_submissions` directly against a
DuckDB connection built the same way `compute_review_responsiveness` builds
one -- this sidesteps METRICS.md §0.6's sample-size floor (5), which would
otherwise force every tiny hand-built scenario's `value`/`flag` to
`None`/`insufficient_data` regardless of whether the underlying logic is
correct. Floor/censoring behavior itself is checked through the public
`compute_review_responsiveness` entry point, using `details_json`'s
`n_hit`/`n_denominator` fields (never floor-gated) rather than `value`.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from project_health.config import load_project
from project_health.metrics.engine import _bot_identifiers, _connect
from project_health.metrics.review_responsiveness import (
    TIER_2_5,
    TIER_6PLUS,
    TIER_FIRST,
    WINDOW_TRAILING12M,
    WINDOW_YEARLY,
    _build_submissions,
    compute_review_responsiveness,
    metric_id,
)
from tests.fixtures.metrics.builders import (
    issue_comments,
    issues,
    jira_changelog,
    pr_comments,
    pr_reviews,
    prs,
)

UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = load_project(REPO_ROOT / "projects" / "cassandra.yaml")
REPO = "apache/cassandra"
RUN_ID = "run-test"


def _ts(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def _pa_transition(issue_key, when, actor="jbellis"):
    return {
        "issue_key": issue_key,
        "changed_at": when,
        "field": "status",
        "from_value": "Open",
        "to_value": "Patch Available",
        "actor_raw_value": actor,
    }


def _empty_tables(**overrides):
    tables = {
        "issue": issues([]),
        "jira_changelog": jira_changelog([]),
        "issue_comment": issue_comments([]),
        "pr": prs([]),
        "pr_review": pr_reviews([]),
        "pr_comment": pr_comments([]),
    }
    tables.update(overrides)
    return tables


def _submissions(tables, config=CONFIG):
    """Build `_Submission` objects the same way `compute_review_
    responsiveness` does, keyed by `issue_key` for easy lookup."""
    con = _connect(tables)
    try:
        con.register("bot_identifier", _bot_identifiers(con, config))
        subs = _build_submissions(con)
    finally:
        con.close()
    return {s.issue_key: s for s in subs}


def _details(row):
    return json.loads(row["details_json"]) if row and row["details_json"] else {}


def _row_for_year(result, stat, tier, year):
    target = metric_id(stat, WINDOW_YEARLY, tier)
    for row in result.to_pylist():
        if row["metric_id"] == target and row["window_start"] == date(year, 1, 1):
            return row
    return None


# --- Tier assignment ---------------------------------------------------


def test_tier_assignment_first_then_2_5_then_6plus():
    author = "carol"
    changelog_rows = [
        _pa_transition(f"CASSANDRA-{i}", _ts(2020, i + 1, 1), actor=author) for i in range(7)
    ]
    issue_rows = [
        {
            "issue_key": f"CASSANDRA-{i}",
            "created_at": _ts(2020, i + 1, 1),
            "updated_at": _ts(2020, i + 1, 1),
            "assignee_raw": author,
        }
        for i in range(7)
    ]
    tables = _empty_tables(issue=issues(issue_rows), jira_changelog=jira_changelog(changelog_rows))
    subs = _submissions(tables)

    tiers = [subs[f"CASSANDRA-{i}"].tier for i in range(7)]
    assert tiers == [TIER_FIRST] + [TIER_2_5] * 4 + [TIER_6PLUS] * 2


def test_tier_assignment_is_per_author_not_global():
    changelog_rows = [
        _pa_transition("CASSANDRA-1", _ts(2020, 1, 1), actor="alice"),
        _pa_transition("CASSANDRA-2", _ts(2020, 1, 2), actor="bob"),
    ]
    issue_rows = [
        {
            "issue_key": "CASSANDRA-1",
            "created_at": _ts(2020, 1, 1),
            "updated_at": _ts(2020, 1, 1),
            "assignee_raw": "alice",
        },
        {
            "issue_key": "CASSANDRA-2",
            "created_at": _ts(2020, 1, 2),
            "updated_at": _ts(2020, 1, 2),
            "assignee_raw": "bob",
        },
    ]
    tables = _empty_tables(issue=issues(issue_rows), jira_changelog=jira_changelog(changelog_rows))
    subs = _submissions(tables)
    # Both are each author's own first submission -- both tier `first`.
    assert subs["CASSANDRA-1"].tier == TIER_FIRST
    assert subs["CASSANDRA-2"].tier == TIER_FIRST


# --- Author reconstruction -----------------------------------------------


def test_author_from_assignee_history_after_transition():
    """When an assignee change happens after the PA transition, the author
    is the *prior* assignee (`from_value`) -- who the ticket was assigned to
    at the moment it became Patch Available."""
    changelog_rows = [
        _pa_transition("CASSANDRA-1", _ts(2020, 1, 1), actor="reporter1"),
        {
            "issue_key": "CASSANDRA-1",
            "changed_at": _ts(2020, 1, 5),
            "field": "assignee",
            "from_value": "alice",
            "to_value": "bob",
            "actor_raw_value": "bob",
        },
    ]
    tables = _empty_tables(
        issue=issues(
            [
                {
                    "issue_key": "CASSANDRA-1",
                    "created_at": _ts(2020, 1, 1),
                    "updated_at": _ts(2020, 1, 1),
                    "assignee_raw": "bob",
                }
            ]
        ),
        jira_changelog=jira_changelog(changelog_rows),
    )
    subs = _submissions(tables)
    assert subs["CASSANDRA-1"].author == "alice"


def test_author_falls_back_to_current_assignee_when_history_value_is_null():
    """The documented divergence from `blend.py`: when the first assignee
    change after the PA transition has a null `from_value` (the ticket had
    no assignee yet at PA time), this project falls through to the issue's
    *current* assignee -- not straight to the transition's actor, which is
    `blend.py`'s Python `or`-chaining artifact (see `scripts/verify_review_
    responsiveness.py` module docstring)."""
    changelog_rows = [
        _pa_transition("CASSANDRA-1", _ts(2020, 1, 1), actor="reporter1"),
        {
            "issue_key": "CASSANDRA-1",
            "changed_at": _ts(2020, 1, 5),
            "field": "assignee",
            "from_value": None,
            "to_value": "alice",
            "actor_raw_value": "alice",
        },
    ]
    tables = _empty_tables(
        issue=issues(
            [
                {
                    "issue_key": "CASSANDRA-1",
                    "created_at": _ts(2020, 1, 1),
                    "updated_at": _ts(2020, 1, 1),
                    "assignee_raw": "current-assignee",
                }
            ]
        ),
        jira_changelog=jira_changelog(changelog_rows),
    )
    subs = _submissions(tables)
    assert subs["CASSANDRA-1"].author == "current-assignee"


def test_author_falls_back_to_transition_actor_when_never_assigned():
    """No assignee change ever, and the issue has no current assignee
    either -- the author is whoever performed the Patch-Available
    transition."""
    changelog_rows = [_pa_transition("CASSANDRA-1", _ts(2020, 1, 1), actor="patch-submitter")]
    tables = _empty_tables(
        issue=issues(
            [
                {
                    "issue_key": "CASSANDRA-1",
                    "created_at": _ts(2020, 1, 1),
                    "updated_at": _ts(2020, 1, 1),
                    "assignee_raw": None,
                }
            ]
        ),
        jira_changelog=jira_changelog(changelog_rows),
    )
    subs = _submissions(tables)
    assert subs["CASSANDRA-1"].author == "patch-submitter"


def test_submitted_at_is_earliest_of_pa_transition_and_linked_pr():
    changelog_rows = [_pa_transition("CASSANDRA-1", _ts(2020, 2, 1), actor="author1")]
    tables = _empty_tables(
        issue=issues(
            [
                {
                    "issue_key": "CASSANDRA-1",
                    "created_at": _ts(2020, 2, 1),
                    "updated_at": _ts(2020, 2, 1),
                    "assignee_raw": "author1",
                }
            ]
        ),
        jira_changelog=jira_changelog(changelog_rows),
        pr=prs(
            [
                {
                    "repo": REPO,
                    "number": 1,
                    "created_at": _ts(2020, 1, 1),  # PR opened before PA transition
                    "author_raw_value": "author1",
                    "linked_issue_keys": ["CASSANDRA-1"],
                }
            ]
        ),
    )
    subs = _submissions(tables)
    assert subs["CASSANDRA-1"].submitted_at == _ts(2020, 1, 1)


# --- Blended first-response source attribution ----------------------------


def _single_submission_tables(
    *, comments=None, changelog_extra=None, prs_=None, reviews=None, comment_rows=None
):
    changelog_rows = [_pa_transition("CASSANDRA-1", _ts(2020, 1, 1), actor="author1")] + (
        changelog_extra or []
    )
    # Every PR row defaults to being linked to CASSANDRA-1 unless the caller
    # already specified `linked_issue_keys` explicitly (e.g. to test a PR
    # that's *not* linked to this issue) -- avoids every review/comment test
    # needing to repeat this boilerplate.
    pr_rows = [{"linked_issue_keys": ["CASSANDRA-1"], **row} for row in (prs_ or [])]
    return _empty_tables(
        issue=issues(
            [
                {
                    "issue_key": "CASSANDRA-1",
                    "created_at": _ts(2020, 1, 1),
                    "updated_at": _ts(2020, 1, 1),
                    "assignee_raw": "author1",
                }
            ]
        ),
        jira_changelog=jira_changelog(changelog_rows),
        issue_comment=issue_comments(comment_rows or []),
        pr=prs(pr_rows),
        pr_review=pr_reviews(reviews or []),
        pr_comment=pr_comments(comments or []),
    )


def test_first_response_source_jira_comment():
    tables = _single_submission_tables(
        comment_rows=[
            {
                "issue_key": "CASSANDRA-1",
                "author_raw_value": "reviewer1",
                "created_at": _ts(2020, 1, 2),
            }
        ]
    )
    sub = _submissions(tables)["CASSANDRA-1"]
    assert sub.first_response_source == "jira_comment"
    assert sub.first_response_at == _ts(2020, 1, 2)


def test_first_response_source_jira_status_transition():
    tables = _single_submission_tables(
        changelog_extra=[
            {
                "issue_key": "CASSANDRA-1",
                "changed_at": _ts(2020, 1, 3),
                "field": "status",
                "from_value": "Patch Available",
                "to_value": "Review In Progress",
                "actor_raw_value": "reviewer1",
            }
        ]
    )
    sub = _submissions(tables)["CASSANDRA-1"]
    assert sub.first_response_source == "jira_status"
    assert sub.first_response_at == _ts(2020, 1, 3)


def test_first_response_source_jira_status_ready_to_commit():
    tables = _single_submission_tables(
        changelog_extra=[
            {
                "issue_key": "CASSANDRA-1",
                "changed_at": _ts(2020, 1, 3),
                "field": "status",
                "from_value": "Review In Progress",
                "to_value": "Ready to Commit",
                "actor_raw_value": "reviewer1",
            }
        ]
    )
    sub = _submissions(tables)["CASSANDRA-1"]
    assert sub.first_response_source == "jira_status"


def test_first_response_source_github_review():
    tables = _single_submission_tables(
        prs_=[
            {
                "repo": REPO,
                "number": 1,
                "created_at": _ts(2020, 1, 1),
                "author_raw_value": "author1",
            }
        ],
        reviews=[
            {
                "repo": REPO,
                "pr_number": 1,
                "reviewer_raw_value": "reviewer1",
                "submitted_at": _ts(2020, 1, 4),
            }
        ],
    )
    sub = _submissions(tables)["CASSANDRA-1"]
    assert sub.first_response_source == "gh_review"
    assert sub.first_response_at == _ts(2020, 1, 4)


def test_first_response_source_github_comment():
    tables = _single_submission_tables(
        prs_=[
            {
                "repo": REPO,
                "number": 1,
                "created_at": _ts(2020, 1, 1),
                "author_raw_value": "author1",
            }
        ],
        comments=[
            {
                "repo": REPO,
                "pr_number": 1,
                "author_raw_value": "reviewer1",
                "created_at": _ts(2020, 1, 6),
            }
        ],
    )
    sub = _submissions(tables)["CASSANDRA-1"]
    assert sub.first_response_source == "gh_comment"
    assert sub.first_response_at == _ts(2020, 1, 6)


def test_first_response_picks_earliest_across_sources():
    tables = _single_submission_tables(
        comment_rows=[
            {
                "issue_key": "CASSANDRA-1",
                "author_raw_value": "reviewer1",
                "created_at": _ts(2020, 1, 10),
            }
        ],
        changelog_extra=[
            {
                "issue_key": "CASSANDRA-1",
                "changed_at": _ts(2020, 1, 2),  # earlier than the comment above
                "field": "status",
                "from_value": "Patch Available",
                "to_value": "Ready to Commit",
                "actor_raw_value": "reviewer1",
            }
        ],
    )
    sub = _submissions(tables)["CASSANDRA-1"]
    assert sub.first_response_source == "jira_status"
    assert sub.first_response_at == _ts(2020, 1, 2)


def test_no_qualifying_event_leaves_first_response_none():
    tables = _single_submission_tables()
    sub = _submissions(tables)["CASSANDRA-1"]
    assert sub.first_response_at is None
    assert sub.first_response_source is None


def test_event_before_submitted_at_does_not_count():
    """A JIRA comment posted before the ticket ever reached Patch Available
    (e.g. pre-review discussion) is not a review response."""
    tables = _single_submission_tables(
        comment_rows=[
            {
                "issue_key": "CASSANDRA-1",
                "author_raw_value": "reviewer1",
                "created_at": _ts(2019, 12, 31),  # before submitted_at (2020-01-01)
            }
        ]
    )
    sub = _submissions(tables)["CASSANDRA-1"]
    assert sub.first_response_at is None


# --- Author / bot exclusion ------------------------------------------------


def test_excludes_authors_own_jira_comment():
    tables = _single_submission_tables(
        comment_rows=[
            {
                "issue_key": "CASSANDRA-1",
                "author_raw_value": "author1",
                "created_at": _ts(2020, 1, 2),
            }
        ]
    )
    sub = _submissions(tables)["CASSANDRA-1"]
    assert sub.first_response_at is None


def test_excludes_bot_jira_comment():
    """`projects/cassandra.yaml`'s real `bot_patterns` (jira_username:
    `^svn-role$|^git-role$`), not a hardcoded test-only list."""
    tables = _single_submission_tables(
        comment_rows=[
            {
                "issue_key": "CASSANDRA-1",
                "author_raw_value": "svn-role",
                "created_at": _ts(2020, 1, 2),
            }
        ]
    )
    sub = _submissions(tables)["CASSANDRA-1"]
    assert sub.first_response_at is None


def test_excludes_bot_jira_comment_does_not_hide_a_later_real_response():
    tables = _single_submission_tables(
        comment_rows=[
            {
                "issue_key": "CASSANDRA-1",
                "author_raw_value": "svn-role",
                "created_at": _ts(2020, 1, 2),
            },
            {
                "issue_key": "CASSANDRA-1",
                "author_raw_value": "reviewer1",
                "created_at": _ts(2020, 1, 3),
            },
        ]
    )
    sub = _submissions(tables)["CASSANDRA-1"]
    assert sub.first_response_source == "jira_comment"
    assert sub.first_response_at == _ts(2020, 1, 3)


def test_excludes_pr_authors_own_review():
    tables = _single_submission_tables(
        prs_=[
            {
                "repo": REPO,
                "number": 1,
                "created_at": _ts(2020, 1, 1),
                "author_raw_value": "author1",
            }
        ],
        reviews=[
            {
                "repo": REPO,
                "pr_number": 1,
                "reviewer_raw_value": "author1",  # the PR's own author
                "submitted_at": _ts(2020, 1, 4),
            }
        ],
    )
    sub = _submissions(tables)["CASSANDRA-1"]
    assert sub.first_response_at is None


def test_excludes_other_linked_pr_authors_review():
    """A submission with two linked PRs: a review by the *other* PR's
    author is still a self-review of sorts (blend.py's own "ghauthors"
    exclusion) and must not count as a qualifying response."""
    tables = _single_submission_tables(
        prs_=[
            {
                "repo": REPO,
                "number": 1,
                "created_at": _ts(2020, 1, 1),
                "author_raw_value": "author1",
            },
            {
                "repo": REPO,
                "number": 2,
                "created_at": _ts(2020, 1, 1),
                "author_raw_value": "coauthor2",
            },
        ],
        reviews=[
            {
                "repo": REPO,
                "pr_number": 1,
                "reviewer_raw_value": "coauthor2",  # authored PR #2 on the same issue
                "submitted_at": _ts(2020, 1, 4),
            }
        ],
    )
    sub = _submissions(tables)["CASSANDRA-1"]
    assert sub.first_response_at is None


# --- Right-censoring denominators (public API, details_json) ---------------


def test_within_30d_share_denominator_excludes_recent_submissions():
    """A submission less than 30 days old at `as_of` is excluded from the
    ≤30d share's denominator entirely (right-censoring), not counted as a
    miss."""
    tables = _empty_tables(
        issue=issues(
            [
                {
                    "issue_key": "CASSANDRA-1",
                    "created_at": _ts(2020, 12, 28),
                    "updated_at": _ts(2020, 12, 28),
                    "assignee_raw": "author1",
                }
            ]
        ),
        jira_changelog=jira_changelog(
            [_pa_transition("CASSANDRA-1", _ts(2020, 12, 28), actor="author1")]
        ),
    )
    result = compute_review_responsiveness(
        tables,
        as_of=date(2021, 1, 6),  # only 9 days after submission, but already in 2021
        run_id=RUN_ID,
        computed_at=_ts(2021, 1, 6),
        config=CONFIG,
    )
    row = _row_for_year(result, "review_response_within_30d_share", TIER_FIRST, 2020)
    assert row is not None
    assert _details(row)["n_denominator"] == 0
    assert row["value"] is None
    assert row["flag"] == "insufficient_data"


def test_within_30d_share_includes_old_enough_submission():
    tables = _single_submission_tables(
        comment_rows=[
            {
                "issue_key": "CASSANDRA-1",
                "author_raw_value": "reviewer1",
                "created_at": _ts(2020, 1, 5),
            }
        ]
    )
    result = compute_review_responsiveness(
        tables, as_of=date(2021, 1, 1), run_id=RUN_ID, computed_at=_ts(2021, 1, 1), config=CONFIG
    )
    row = _row_for_year(result, "review_response_within_30d_share", TIER_FIRST, 2020)
    details = _details(row)
    assert details["n_denominator"] == 1
    assert details["n_hit"] == 1


def test_within_7d_and_30d_use_separate_age_gates():
    """Issue #102's own spec (not `blend.py`'s single 30-day gate for every
    stat): the 7d share's denominator only requires >=7 days old, so a
    submission 10 days old at `as_of` enters the 7d denominator but not the
    30d one."""
    tables = _empty_tables(
        issue=issues(
            [
                {
                    "issue_key": "CASSANDRA-1",
                    "created_at": _ts(2020, 12, 28),
                    "updated_at": _ts(2020, 12, 28),
                    "assignee_raw": "author1",
                }
            ]
        ),
        jira_changelog=jira_changelog(
            [_pa_transition("CASSANDRA-1", _ts(2020, 12, 28), actor="author1")]
        ),
    )
    result = compute_review_responsiveness(
        tables,
        as_of=date(2021, 1, 7),  # 10 days after submission, but already in 2021
        run_id=RUN_ID,
        computed_at=_ts(2021, 1, 7),
        config=CONFIG,
    )
    row_7d = _row_for_year(result, "review_response_within_7d_share", TIER_FIRST, 2020)
    row_30d = _row_for_year(result, "review_response_within_30d_share", TIER_FIRST, 2020)
    assert _details(row_7d)["n_denominator"] == 1
    assert _details(row_30d)["n_denominator"] == 0


def test_no_visible_response_share_counts_unanswered_old_submission():
    tables = _single_submission_tables()  # no response at all
    result = compute_review_responsiveness(
        tables, as_of=date(2021, 1, 1), run_id=RUN_ID, computed_at=_ts(2021, 1, 1), config=CONFIG
    )
    row = _row_for_year(result, "review_no_visible_response_share", TIER_FIRST, 2020)
    details = _details(row)
    assert details["n_denominator"] == 1
    assert details["n_hit"] == 1  # the one no-visible-response submission


def test_no_visible_response_share_excludes_submissions_that_got_a_response():
    tables = _single_submission_tables(
        comment_rows=[
            {
                "issue_key": "CASSANDRA-1",
                "author_raw_value": "reviewer1",
                "created_at": _ts(2020, 1, 5),
            }
        ]
    )
    result = compute_review_responsiveness(
        tables, as_of=date(2021, 1, 1), run_id=RUN_ID, computed_at=_ts(2021, 1, 1), config=CONFIG
    )
    row = _row_for_year(result, "review_no_visible_response_share", TIER_FIRST, 2020)
    details = _details(row)
    assert details["n_denominator"] == 1
    assert details["n_hit"] == 0


def test_committed_within_365d_share_requires_resolution_fixed():
    tables = _single_submission_tables()
    tables["issue"] = issues(
        [
            {
                "issue_key": "CASSANDRA-1",
                "created_at": _ts(2020, 1, 1),
                "updated_at": _ts(2020, 1, 1),
                "assignee_raw": "author1",
                "resolution": "Won't Fix",
                "resolved_at": _ts(2020, 1, 10),
            }
        ]
    )
    result = compute_review_responsiveness(
        tables, as_of=date(2021, 6, 1), run_id=RUN_ID, computed_at=_ts(2021, 6, 1), config=CONFIG
    )
    row = _row_for_year(result, "patch_committed_within_365d_share", TIER_FIRST, 2020)
    details = _details(row)
    assert details["n_denominator"] == 1
    assert details["n_hit"] == 0  # resolved, but not "Fixed" -- doesn't count


def test_committed_within_365d_share_true_when_fixed_in_time():
    tables = _single_submission_tables()
    tables["issue"] = issues(
        [
            {
                "issue_key": "CASSANDRA-1",
                "created_at": _ts(2020, 1, 1),
                "updated_at": _ts(2020, 1, 1),
                "assignee_raw": "author1",
                "resolution": "Fixed",
                "resolved_at": _ts(2020, 3, 1),
            }
        ]
    )
    result = compute_review_responsiveness(
        tables, as_of=date(2021, 6, 1), run_id=RUN_ID, computed_at=_ts(2021, 6, 1), config=CONFIG
    )
    row = _row_for_year(result, "patch_committed_within_365d_share", TIER_FIRST, 2020)
    details = _details(row)
    assert details["n_denominator"] == 1
    assert details["n_hit"] == 1


def test_committed_within_365d_share_excludes_submissions_not_yet_365d_old():
    tables = _empty_tables(
        issue=issues(
            [
                {
                    "issue_key": "CASSANDRA-1",
                    "created_at": _ts(2020, 12, 28),
                    "updated_at": _ts(2020, 12, 28),
                    "assignee_raw": "author1",
                    "resolution": "Fixed",
                    "resolved_at": _ts(2021, 1, 2),
                }
            ]
        ),
        jira_changelog=jira_changelog(
            [_pa_transition("CASSANDRA-1", _ts(2020, 12, 28), actor="author1")]
        ),
    )
    result = compute_review_responsiveness(
        tables,
        as_of=date(2021, 3, 1),  # well under 365 days after submission, but 2020 is completed
        run_id=RUN_ID,
        computed_at=_ts(2021, 3, 1),
        config=CONFIG,
    )
    row = _row_for_year(result, "patch_committed_within_365d_share", TIER_FIRST, 2020)
    assert _details(row)["n_denominator"] == 0


# --- Headcounts (first_patch_submissions) -----------------------------------


def test_first_patch_submissions_counts_only_first_tier():
    author = "dora"
    changelog_rows = [
        _pa_transition(f"CASSANDRA-{i}", _ts(2020, i + 1, 1), actor=author) for i in range(3)
    ]
    issue_rows = [
        {
            "issue_key": f"CASSANDRA-{i}",
            "created_at": _ts(2020, i + 1, 1),
            "updated_at": _ts(2020, i + 1, 1),
            "assignee_raw": author,
        }
        for i in range(3)
    ]
    tables = _empty_tables(issue=issues(issue_rows), jira_changelog=jira_changelog(changelog_rows))
    result = compute_review_responsiveness(
        tables, as_of=date(2021, 1, 1), run_id=RUN_ID, computed_at=_ts(2021, 1, 1), config=CONFIG
    )
    row = _row_for_year(result, "first_patch_submissions", None, 2020)
    # 3 submissions by the same author this year: 1 first, 2 tier_2_5 -- only
    # the 1 counts here (headcount, no floor: value == n == 1, flag 'ok').
    assert row["value"] == 1.0
    assert row["n"] == 1
    assert row["flag"] == "ok"


# --- Windows: yearly vs trailing12m -----------------------------------------


def test_trailing12m_rows_are_also_produced():
    tables = _single_submission_tables(
        comment_rows=[
            {
                "issue_key": "CASSANDRA-1",
                "author_raw_value": "reviewer1",
                "created_at": _ts(2020, 1, 2),
            }
        ]
    )
    result = compute_review_responsiveness(
        tables, as_of=date(2020, 6, 1), run_id=RUN_ID, computed_at=_ts(2020, 6, 1), config=CONFIG
    )
    target = metric_id("review_first_response_median_days", WINDOW_TRAILING12M, TIER_FIRST)
    trailing_rows = [r for r in result.to_pylist() if r["metric_id"] == target]
    assert trailing_rows  # at least one dense monthly trailing-12m row exists
    assert all(json.loads(r["details_json"])["n_submitted"] == 1 for r in trailing_rows)


def test_not_wired_into_scored_metric_ids():
    """Deliberately outside `metrics/registry.py::METRIC_IDS` -- see module
    docstring for why (D25-style exclusion, default "no" on composite
    scoring per issue #102)."""
    from project_health.metrics.registry import METRIC_IDS
    from project_health.metrics.review_responsiveness import ALL_METRIC_IDS

    assert not set(ALL_METRIC_IDS) & set(METRIC_IDS)
