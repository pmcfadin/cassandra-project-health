"""Tests for project_health.leaderboard (D19, issue #56)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pyarrow as pa
import pytest

from project_health.config import ProjectConfig
from project_health.leaderboard import (
    ACTIVITY_COMMITS,
    ACTIVITY_JIRA_RESOLVED,
    ACTIVITY_REVIEWS,
    ACTIVITY_TYPES,
    TOP_N,
    build_leaderboards,
)
from project_health.normalize.identity import identity_id_for, normalize_value
from project_health.schema import get_schema, validate

NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)
AS_OF = date(2026, 9, 25)
# _leaderboard_window(AS_OF): trailing 12 completed months ending Aug 2026.
IN_WINDOW = datetime(2026, 3, 15, tzinfo=timezone.utc)
BEFORE_WINDOW = datetime(2024, 1, 15, tzinfo=timezone.utc)


def _config(bot_patterns: list[dict] | None = None) -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "project": {"id": "x", "display_name": "X"},
            "reviewer_extraction": {
                "commit_trailer": {"type": "commit_trailer", "pattern": "x"},
                "jira_fields": {"type": "jira_fields"},
            },
            "bot_patterns": bot_patterns or [],
        }
    )


def _contribution_event(rows: list[dict]) -> pa.Table:
    defaults = {
        "identity_id": None,
        "author_display_name": None,
        "event_type": "code_commit",
        "occurred_at": IN_WINDOW,
        "repo": "r",
        "source_snapshot_id": "s",
    }
    full = []
    for i, row in enumerate(rows):
        merged = {**defaults, "event_id": f"c{i}", "source_ref": f"sha{i}", **row}
        full.append(merged)
    schema = get_schema("contribution_event")
    return validate("contribution_event", pa.Table.from_pylist(full, schema=schema))


def _review_event(rows: list[dict]) -> pa.Table:
    defaults = {
        "source": "commit_trailer",
        "reviewer_identity_id": None,
        "author_identity_id": None,
        "author_raw_type": None,
        "author_raw_value": None,
        "issue_key": None,
        "repo": "r",
        "occurred_at": IN_WINDOW,
        "evidence": None,
        "source_snapshot_id": "s",
    }
    full = []
    for i, row in enumerate(rows):
        merged = {**defaults, "event_id": f"r{i}", **row}
        full.append(merged)
    schema = get_schema("review_event")
    return validate("review_event", pa.Table.from_pylist(full, schema=schema))


def _issue(rows: list[dict]) -> pa.Table:
    defaults = {
        "summary": None,
        "status": None,
        "status_category": None,
        "priority": None,
        "issue_type": None,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": IN_WINDOW,
        "resolved_at": IN_WINDOW,
        "reporter_identity_id": None,
        "reporter_raw": None,
        "assignee_identity_id": None,
        "source_snapshot_id": "s",
    }
    full = []
    for i, row in enumerate(rows):
        merged = {**defaults, "issue_key": f"CASSANDRA-{i}", **row}
        full.append(merged)
    schema = get_schema("issue")
    return validate("issue", pa.Table.from_pylist(full, schema=schema))


def _identity_link(pairs: list[tuple[str, str]], overrides: list[dict] | None = None) -> pa.Table:
    rows = []
    for source_type, source_value in pairs:
        normalized = normalize_value(source_type, source_value)
        identity_id = identity_id_for(source_type, normalized)
        rows.append(
            {
                "link_id": f"link-{source_type}-{source_value}",
                "identity_id": identity_id,
                "source_type": source_type,
                "source_value": source_value,
                "confidence": "exact",
                "evidence": "e",
                "linked_by": "naive_identity_v1",
                "linked_at": NOW,
            }
        )
    for override in overrides or []:
        rows.append({**override, "linked_at": NOW})
    schema = get_schema("identity_link")
    return validate("identity_link", pa.Table.from_pylist(rows, schema=schema))


def _person_identity(entries: list[tuple[str, str, str]]) -> pa.Table:
    """entries: (source_type, source_value, display_name)."""
    rows = [
        {
            "identity_id": identity_id_for(source_type, normalize_value(source_type, source_value)),
            "display_name": display_name,
            "status": "provisional",
            "created_at": NOW,
        }
        for source_type, source_value, display_name in entries
    ]
    schema = get_schema("person_identity")
    return validate("person_identity", pa.Table.from_pylist(rows, schema=schema))


def _identity_id(source_type: str, value: str) -> str:
    return identity_id_for(source_type, normalize_value(source_type, value))


def _run(**tables) -> dict:
    result = build_leaderboards(
        tables, as_of=AS_OF, run_id="run1", computed_at=NOW, config=_config()
    )
    return result


def test_commits_ranked_desc_by_count_with_identity_resolution():
    ce = _contribution_event(
        [
            {"author_raw_type": "git_email", "author_raw_value": "alice@x.org"},
            {"author_raw_type": "git_email", "author_raw_value": "alice@x.org"},
            {"author_raw_type": "git_email", "author_raw_value": "bob@x.org"},
        ]
    )
    identity_link = _identity_link([("git_email", "alice@x.org"), ("git_email", "bob@x.org")])
    result = build_leaderboards(
        {"contribution_event": ce, "identity_link": identity_link},
        as_of=AS_OF,
        run_id="run1",
        computed_at=NOW,
        config=_config(),
    )
    commits = result.lists[ACTIVITY_COMMITS]
    assert [e.count for e in commits.entries] == [2, 1]
    assert commits.entries[0].identity_id == _identity_id("git_email", "alice@x.org")
    assert commits.window_start == date(2025, 9, 1)
    assert commits.window_end == date(2026, 8, 31)


def test_activity_types_are_never_blended():
    """D19: three separate lists, never one combined score."""
    result = _run(
        contribution_event=_contribution_event([]),
        identity_link=_identity_link([]),
    )
    assert set(result.lists) == set(ACTIVITY_TYPES)
    assert ACTIVITY_TYPES == (ACTIVITY_COMMITS, ACTIVITY_REVIEWS, ACTIVITY_JIRA_RESOLVED)
    # Each list is independently rankable -- no combined field exists at all.
    for leaderboard_list in result.lists.values():
        for entry in leaderboard_list.entries:
            assert not hasattr(entry, "score")


def test_reviews_use_commit_trailer_source_only_matching_reviewer_hhi():
    re_table = _review_event(
        [
            {
                "source": "commit_trailer",
                "reviewer_raw_type": "git_name",
                "reviewer_raw_value": "Carol",
            },
            {
                "source": "jira_field",
                "reviewer_raw_type": "jira_username",
                "reviewer_raw_value": "carol2",
            },
        ]
    )
    identity_link = _identity_link([("git_name", "Carol"), ("jira_username", "carol2")])
    result = build_leaderboards(
        {"review_event": re_table, "identity_link": identity_link},
        as_of=AS_OF,
        run_id="run1",
        computed_at=NOW,
        config=_config(),
    )
    reviews = result.lists[ACTIVITY_REVIEWS]
    assert len(reviews.entries) == 1
    assert reviews.entries[0].count == 1
    assert reviews.entries[0].identity_id == _identity_id("git_name", "Carol")


def test_jira_issues_resolved_grouped_by_assignee():
    issue_table = _issue(
        [
            {"assignee_raw": "dave"},
            {"assignee_raw": "dave"},
            {"assignee_raw": "erin"},
            {"assignee_raw": None},  # unassigned -- never counted
            {"assignee_raw": "frank", "resolved_at": None},  # unresolved -- never counted
        ]
    )
    identity_link = _identity_link(
        [("jira_username", "dave"), ("jira_username", "erin"), ("jira_username", "frank")]
    )
    result = build_leaderboards(
        {"issue": issue_table, "identity_link": identity_link},
        as_of=AS_OF,
        run_id="run1",
        computed_at=NOW,
        config=_config(),
    )
    resolved = result.lists[ACTIVITY_JIRA_RESOLVED]
    assert [e.count for e in resolved.entries] == [2, 1]
    assert resolved.entries[0].identity_id == _identity_id("jira_username", "dave")


def test_events_outside_the_trailing_12_completed_months_are_excluded():
    ce = _contribution_event(
        [
            {
                "author_raw_type": "git_email",
                "author_raw_value": "alice@x.org",
                "occurred_at": IN_WINDOW,
            },
            {
                "author_raw_type": "git_email",
                "author_raw_value": "alice@x.org",
                "occurred_at": BEFORE_WINDOW,
            },
        ]
    )
    identity_link = _identity_link([("git_email", "alice@x.org")])
    result = build_leaderboards(
        {"contribution_event": ce, "identity_link": identity_link},
        as_of=AS_OF,
        run_id="run1",
        computed_at=NOW,
        config=_config(),
    )
    commits = result.lists[ACTIVITY_COMMITS]
    assert commits.entries[0].count == 1


def test_bots_are_excluded_per_bot_patterns():
    ce = _contribution_event(
        [
            {"author_raw_type": "git_email", "author_raw_value": "alice@x.org"},
            {"author_raw_type": "git_email", "author_raw_value": "bot@x.org"},
        ]
    )
    identity_link = _identity_link([("git_email", "alice@x.org"), ("git_email", "bot@x.org")])
    result = build_leaderboards(
        {"contribution_event": ce, "identity_link": identity_link},
        as_of=AS_OF,
        run_id="run1",
        computed_at=NOW,
        config=_config([{"field": "git_author_email", "regex": r"^bot@"}]),
    )
    commits = result.lists[ACTIVITY_COMMITS]
    assert commits.population_size == 1
    assert commits.entries[0].identity_id == _identity_id("git_email", "alice@x.org")


def test_manual_identity_override_wins_over_naive_resolution():
    """D19: 'same identity resolution as the metrics' -- a manual
    identity_overrides.yaml merge (linked_by='manual:<reviewer>') must
    collapse two raw identifiers into one leaderboard row."""
    alice_email_id = _identity_id("git_email", "alice@x.org")
    ce = _contribution_event(
        [
            {"author_raw_type": "git_email", "author_raw_value": "alice@x.org"},
            {"author_raw_type": "git_email", "author_raw_value": "alice2@x.org"},
        ]
    )
    identity_link = _identity_link(
        [("git_email", "alice@x.org"), ("git_email", "alice2@x.org")],
        overrides=[
            {
                "link_id": "manual-link-1",
                "identity_id": alice_email_id,
                "source_type": "git_email",
                "source_value": "alice2@x.org",
                "confidence": "exact",
                "evidence": "same person, confirmed on CASSANDRA-1",
                "linked_by": "manual:pmcfadin",
            }
        ],
    )
    result = build_leaderboards(
        {"contribution_event": ce, "identity_link": identity_link},
        as_of=AS_OF,
        run_id="run1",
        computed_at=NOW,
        config=_config(),
    )
    commits = result.lists[ACTIVITY_COMMITS]
    assert commits.population_size == 1
    assert commits.entries[0].count == 2
    assert commits.entries[0].identity_id == alice_email_id


def test_organization_resolved_from_affiliation_period_curated_wins():
    ce = _contribution_event(
        [{"author_raw_type": "git_email", "author_raw_value": "alice@x.org"}]
    )
    identity_link = _identity_link([("git_email", "alice@x.org")])
    alice_id = _identity_id("git_email", "alice@x.org")
    affiliation_period = validate(
        "affiliation_period",
        pa.Table.from_pylist(
            [
                {
                    "entry_id": "e1",
                    "identity_id": alice_id,
                    "organization": "HeuristicOrg",
                    "effective_from": None,
                    "effective_to": None,
                    "source": "email_domain",
                    "evidence": "x",
                },
                {
                    "entry_id": "e2",
                    "identity_id": alice_id,
                    "organization": "CuratedOrg",
                    "effective_from": None,
                    "effective_to": None,
                    "source": "curated",
                    "evidence": "x",
                },
            ],
            schema=get_schema("affiliation_period"),
        ),
    )
    result = build_leaderboards(
        {
            "contribution_event": ce,
            "identity_link": identity_link,
            "affiliation_period": affiliation_period,
        },
        as_of=AS_OF,
        run_id="run1",
        computed_at=NOW,
        config=_config(),
    )
    commits = result.lists[ACTIVITY_COMMITS]
    assert commits.entries[0].organization == "CuratedOrg"


def test_organization_is_unknown_when_no_affiliation_period_row_covers_it():
    ce = _contribution_event(
        [{"author_raw_type": "git_email", "author_raw_value": "alice@x.org"}]
    )
    identity_link = _identity_link([("git_email", "alice@x.org")])
    result = build_leaderboards(
        {"contribution_event": ce, "identity_link": identity_link},
        as_of=AS_OF,
        run_id="run1",
        computed_at=NOW,
        config=_config(),
    )
    commits = result.lists[ACTIVITY_COMMITS]
    assert commits.entries[0].organization == "unknown"


def test_display_name_from_person_identity_when_provided():
    ce = _contribution_event(
        [{"author_raw_type": "git_email", "author_raw_value": "alice@x.org"}]
    )
    identity_link = _identity_link([("git_email", "alice@x.org")])
    person_identity = _person_identity([("git_email", "alice@x.org", "Alice Smith")])
    result = build_leaderboards(
        {
            "contribution_event": ce,
            "identity_link": identity_link,
            "person_identity": person_identity,
        },
        as_of=AS_OF,
        run_id="run1",
        computed_at=NOW,
        config=_config(),
    )
    commits = result.lists[ACTIVITY_COMMITS]
    assert commits.entries[0].display_name == "Alice Smith"


def test_display_name_falls_back_to_raw_identifier_without_person_identity():
    """No `person_identity` table at all (or no display name resolved for
    this identity) shows the person's own raw identifier (here, their git
    email) rather than a bare, meaningless `identity_id` UUID -- the same
    handles-vs-full-names gap `IDENTITY_LIMITATIONS_NOTE` already discloses."""
    ce = _contribution_event(
        [{"author_raw_type": "git_email", "author_raw_value": "alice@x.org"}]
    )
    identity_link = _identity_link([("git_email", "alice@x.org")])
    result = build_leaderboards(
        {"contribution_event": ce, "identity_link": identity_link},
        as_of=AS_OF,
        run_id="run1",
        computed_at=NOW,
        config=_config(),
    )
    commits = result.lists[ACTIVITY_COMMITS]
    assert commits.entries[0].display_name == "alice@x.org"


def test_display_name_falls_back_when_person_identity_has_no_name_for_this_identity():
    """A jira_username-only identity is the realistic case this matters for:
    M0's JIRA collector never attaches a display name to assignee_raw, so
    person_identity's own display_name for it is None (identity.py's
    `_canonical_display_name`) -- the raw JIRA username is shown instead."""
    issue_table = _issue([{"assignee_raw": "dave"}])
    identity_link = _identity_link([("jira_username", "dave")])
    person_identity = _person_identity([("jira_username", "dave", None)])
    result = build_leaderboards(
        {
            "issue": issue_table,
            "identity_link": identity_link,
            "person_identity": person_identity,
        },
        as_of=AS_OF,
        run_id="run1",
        computed_at=NOW,
        config=_config(),
    )
    resolved = result.lists[ACTIVITY_JIRA_RESOLVED]
    assert resolved.entries[0].display_name == "dave"


def test_top_n_truncates_but_population_size_reports_the_full_count():
    rows = [
        {"author_raw_type": "git_email", "author_raw_value": f"person{i}@x.org"}
        for i in range(TOP_N + 5)
    ]
    ce = _contribution_event(rows)
    identity_link = _identity_link(
        [("git_email", f"person{i}@x.org") for i in range(TOP_N + 5)]
    )
    result = build_leaderboards(
        {"contribution_event": ce, "identity_link": identity_link},
        as_of=AS_OF,
        run_id="run1",
        computed_at=NOW,
        config=_config(),
    )
    commits = result.lists[ACTIVITY_COMMITS]
    assert len(commits.entries) == TOP_N
    assert commits.population_size == TOP_N + 5


def test_empty_input_produces_empty_lists_not_a_crash():
    result = build_leaderboards(
        {}, as_of=AS_OF, run_id="run1", computed_at=NOW, config=_config()
    )
    for activity_type in ACTIVITY_TYPES:
        leaderboard_list = result.lists[activity_type]
        assert leaderboard_list.entries == []
        assert leaderboard_list.population_size == 0
        assert not leaderboard_list.has_data


def test_table_output_matches_contributor_leaderboard_schema():
    ce = _contribution_event(
        [{"author_raw_type": "git_email", "author_raw_value": "alice@x.org"}]
    )
    identity_link = _identity_link([("git_email", "alice@x.org")])
    result = build_leaderboards(
        {"contribution_event": ce, "identity_link": identity_link},
        as_of=AS_OF,
        run_id="run1",
        computed_at=NOW,
        config=_config(),
    )
    assert result.table.schema.equals(get_schema("contributor_leaderboard"))
    rows = result.table.to_pylist()
    assert any(r["activity_type"] == ACTIVITY_COMMITS for r in rows)
    # All three activity types get at least the empty-window bookkeeping --
    # rows only exist for entries that actually made a top-N list, so an
    # activity type with zero qualifying identities contributes zero rows,
    # never a placeholder row.
    assert all(r["run_id"] == "run1" for r in rows)


def test_identity_limitations_note_is_present_and_non_empty():
    result = build_leaderboards(
        {}, as_of=AS_OF, run_id="run1", computed_at=NOW, config=_config()
    )
    assert result.identity_limitations_note
    assert "identity_overrides.yaml" in result.identity_limitations_note


@pytest.mark.parametrize("activity_type", ACTIVITY_TYPES)
def test_every_activity_type_window_is_trailing_12_completed_months(activity_type):
    result = build_leaderboards(
        {}, as_of=AS_OF, run_id="run1", computed_at=NOW, config=_config()
    )
    leaderboard_list = result.lists[activity_type]
    assert leaderboard_list.window_start == date(2025, 9, 1)
    assert leaderboard_list.window_end == date(2026, 8, 31)
