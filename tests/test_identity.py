"""Tests for project_health.normalize.identity (ARCHITECTURE.md §3, issue #6)."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from project_health.normalize.identity import (
    GIT_EMAIL,
    GIT_NAME,
    JIRA_USERNAME,
    IdentityResolutionError,
    ManualOverride,
    RawIdentifier,
    build_resolver,
    extract_raw_identifiers,
    identity_id_for,
    link_github_commit_authors,
    load_overrides,
    normalize_value,
    resolve_identities,
)

NOW = datetime(2026, 9, 25, 6, 17, tzinfo=timezone.utc)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _write_parquet_bytes(table: pa.Table) -> bytes:
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


# --- Acceptance criterion: two emails differing only by case -> one identity, exact


def test_two_emails_differing_only_by_case_resolve_to_one_identity_exact():
    raws = [
        RawIdentifier(GIT_EMAIL, "Alice@Example.org", display_name="Alice"),
        RawIdentifier(GIT_EMAIL, "alice@example.org", display_name="Alice"),
    ]

    result = resolve_identities(raws, now=NOW)

    assert result.person_identity.num_rows == 1
    assert result.person_identity.column("status").to_pylist() == ["provisional"]
    assert result.identity_link.num_rows == 2
    assert set(result.identity_link.column("confidence").to_pylist()) == {"exact"}
    identity_ids = set(result.identity_link.column("identity_id").to_pylist())
    assert len(identity_ids) == 1


def test_identical_jira_username_resolves_to_one_identity_exact():
    raws = [
        RawIdentifier(JIRA_USERNAME, "jdoe"),
        RawIdentifier(JIRA_USERNAME, "jdoe"),
    ]

    result = resolve_identities(raws, now=NOW)

    assert result.person_identity.num_rows == 1
    assert result.identity_link.num_rows == 1  # same raw string, not two rows
    assert result.identity_link.column("confidence").to_pylist() == ["exact"]


def test_jira_username_is_case_sensitive_unlike_email():
    raws = [
        RawIdentifier(JIRA_USERNAME, "jdoe"),
        RawIdentifier(JIRA_USERNAME, "JDoe"),
    ]

    result = resolve_identities(raws, now=NOW)

    # Unlike git_email, jira_username is not lowercased — these are two
    # distinct identities.
    assert result.person_identity.num_rows == 2


# --- Acceptance criterion: trailer name vs git author -> two identities +
# one low candidate, never merged


def test_trailer_name_and_git_author_never_auto_merge_but_produce_one_low_candidate():
    raws = [
        RawIdentifier(GIT_NAME, "Jane Doe"),  # commit-trailer reviewer name
        RawIdentifier(GIT_EMAIL, "jane@x.org", display_name="Jane Doe"),  # git author
    ]

    result = resolve_identities(raws, now=NOW)

    assert result.person_identity.num_rows == 2
    identity_ids = set(result.person_identity.column("identity_id").to_pylist())
    link_identity_ids = set(result.identity_link.column("identity_id").to_pylist())
    assert link_identity_ids == identity_ids  # never merged: each has its own link

    assert result.identity_candidates.num_rows == 1
    candidate = result.identity_candidates.to_pylist()[0]
    assert candidate["confidence"] == "low"
    assert {candidate["source_type_a"], candidate["source_type_b"]} == {GIT_NAME, GIT_EMAIL}
    assert {candidate["identity_id_a"], candidate["identity_id_b"]} == identity_ids

    # The resolver (what metrics use) must NOT merge them.
    id_git_name = result.resolver(GIT_NAME, "Jane Doe")
    id_git_email = result.resolver(GIT_EMAIL, "jane@x.org")
    assert id_git_name is not None
    assert id_git_email is not None
    assert id_git_name != id_git_email


def test_unrelated_names_produce_no_candidates():
    raws = [
        RawIdentifier(GIT_NAME, "Jane Doe"),
        RawIdentifier(GIT_EMAIL, "bob@example.org", display_name="Bob Smith"),
    ]

    result = resolve_identities(raws, now=NOW)

    assert result.identity_candidates.num_rows == 0


# --- Acceptance criterion: manual override merges with a manual: link and evidence


def test_manual_override_merges_via_new_link_with_manual_prefix_and_evidence():
    raws = [
        RawIdentifier(GIT_NAME, "Jane Doe"),
        RawIdentifier(GIT_EMAIL, "jane@x.org", display_name="Jane Doe"),
    ]
    overrides = [
        ManualOverride(
            source_type=GIT_NAME,
            source_value="Jane Doe",
            into_source_type=GIT_EMAIL,
            into_source_value="jane@x.org",
            reviewer="pmcfadin",
            evidence="Confirmed same person on CASSANDRA-12345.",
        )
    ]

    result = resolve_identities(raws, overrides, now=NOW)

    # The original naive identities are untouched (append-only) ...
    assert result.person_identity.num_rows == 2

    # ... but a new manual identity_link row now exists.
    linked_bys = result.identity_link.column("linked_by").to_pylist()
    manual_rows = [
        row
        for row in result.identity_link.to_pylist()
        if row["linked_by"] == "manual:pmcfadin"
    ]
    assert len(manual_rows) == 1
    manual_row = manual_rows[0]
    assert manual_row["source_type"] == GIT_NAME
    assert manual_row["source_value"] == "Jane Doe"
    assert manual_row["evidence"] == "Confirmed same person on CASSANDRA-12345."
    assert "naive_identity_v1" in linked_bys  # original link rows still present too

    # The resolver now merges them for metrics purposes.
    id_git_name = result.resolver(GIT_NAME, "Jane Doe")
    id_git_email = result.resolver(GIT_EMAIL, "jane@x.org")
    assert id_git_name == id_git_email
    assert manual_row["identity_id"] == id_git_email


# --- Acceptance criterion: running twice on the same input yields byte-identical Parquet


def test_rerun_on_same_input_yields_byte_identical_parquet():
    raws = [
        RawIdentifier(GIT_EMAIL, "Alice@Example.org", display_name="Alice"),
        RawIdentifier(GIT_NAME, "Jane Doe"),
        RawIdentifier(GIT_EMAIL, "jane@x.org", display_name="Jane Doe"),
        RawIdentifier(JIRA_USERNAME, "jdoe"),
    ]
    overrides = [
        ManualOverride(
            source_type=GIT_NAME,
            source_value="Jane Doe",
            into_source_type=GIT_EMAIL,
            into_source_value="jane@x.org",
            reviewer="pmcfadin",
            evidence="Confirmed same person.",
        )
    ]

    first = resolve_identities(raws, overrides, now=NOW)
    # Feed the identifiers in a different order the second time — the output
    # must not depend on input ordering either.
    second = resolve_identities(list(reversed(raws)), overrides, now=NOW)

    assert _write_parquet_bytes(first.person_identity) == _write_parquet_bytes(
        second.person_identity
    )
    assert _write_parquet_bytes(first.identity_link) == _write_parquet_bytes(
        second.identity_link
    )
    assert _write_parquet_bytes(first.identity_candidates) == _write_parquet_bytes(
        second.identity_candidates
    )


def test_identity_id_is_a_deterministic_uuid5_of_type_and_normalized_value():
    first = identity_id_for(GIT_EMAIL, "alice@example.org")
    second = identity_id_for(GIT_EMAIL, "alice@example.org")
    assert first == second

    different_value = identity_id_for(GIT_EMAIL, "bob@example.org")
    assert different_value != first

    # Same normalized value, different type -> different identity (no
    # cross-type collision).
    different_type = identity_id_for(JIRA_USERNAME, "alice@example.org")
    assert different_type != first


def test_now_must_be_timezone_aware():
    with pytest.raises(IdentityResolutionError):
        resolve_identities([RawIdentifier(GIT_EMAIL, "a@x.org")], now=datetime(2026, 9, 25))


def test_unknown_raw_type_raises():
    with pytest.raises(IdentityResolutionError):
        resolve_identities([RawIdentifier("carrier_pigeon", "x")], now=NOW)


def test_normalize_value_rejects_unknown_type():
    with pytest.raises(IdentityResolutionError):
        normalize_value("carrier_pigeon", "x")


# --- build_resolver (metrics-facing mapping) ---------------------------------


def test_build_resolver_prefers_manual_link_over_naive_link_for_same_raw_identifier():
    raws = [RawIdentifier(GIT_NAME, "Jane Doe"), RawIdentifier(GIT_EMAIL, "jane@x.org")]
    overrides = [
        ManualOverride(
            source_type=GIT_NAME,
            source_value="Jane Doe",
            into_source_type=GIT_EMAIL,
            into_source_value="jane@x.org",
            reviewer="pmcfadin",
            evidence="Confirmed.",
        )
    ]
    result = resolve_identities(raws, overrides, now=NOW)

    resolver = build_resolver(result.identity_link)
    assert resolver(GIT_NAME, "Jane Doe") == resolver(GIT_EMAIL, "jane@x.org")


def test_build_resolver_returns_none_for_unknown_identifier():
    result = resolve_identities([RawIdentifier(GIT_EMAIL, "a@x.org")], now=NOW)
    resolver = build_resolver(result.identity_link)
    assert resolver(GIT_EMAIL, "nobody@x.org") is None


# --- extract_raw_identifiers (pulls raw columns out of the M0 fact tables) --


def test_extract_raw_identifiers_from_contribution_review_and_issue_tables():
    contribution_events = pa.table(
        {
            "author_raw_type": pa.array([GIT_EMAIL], type=pa.string()),
            "author_raw_value": pa.array(["alice@example.org"], type=pa.string()),
            "author_display_name": pa.array(["Alice"], type=pa.string()),
        }
    )
    review_events = pa.table(
        {
            "reviewer_raw_type": pa.array([GIT_NAME], type=pa.string()),
            "reviewer_raw_value": pa.array(["Jane Doe"], type=pa.string()),
            "author_raw_type": pa.array([GIT_EMAIL], type=pa.string()),
            "author_raw_value": pa.array(["bob@example.org"], type=pa.string()),
        }
    )
    issues = pa.table(
        {
            "reporter_raw": pa.array(["jdoe"], type=pa.string()),
            "assignee_raw": pa.array([None], type=pa.string()),
        }
    )

    identifiers = extract_raw_identifiers(
        contribution_events=contribution_events,
        review_events=review_events,
        issues=issues,
    )

    assert RawIdentifier(GIT_EMAIL, "alice@example.org", "Alice") in identifiers
    assert RawIdentifier(GIT_NAME, "Jane Doe") in identifiers
    assert RawIdentifier(GIT_EMAIL, "bob@example.org") in identifiers
    assert RawIdentifier(JIRA_USERNAME, "jdoe") in identifiers
    # assignee_raw was null -> not included
    assert len(identifiers) == 4


def test_extract_raw_identifiers_handles_null_review_author_raw():
    review_events = pa.table(
        {
            "reviewer_raw_type": pa.array([JIRA_USERNAME], type=pa.string()),
            "reviewer_raw_value": pa.array(["jdoe"], type=pa.string()),
            "author_raw_type": pa.array([None], type=pa.string()),
            "author_raw_value": pa.array([None], type=pa.string()),
        }
    )

    identifiers = extract_raw_identifiers(review_events=review_events)

    assert identifiers == [RawIdentifier(JIRA_USERNAME, "jdoe")]


# --- identity_overrides.yaml loading -----------------------------------------


def test_load_overrides_from_repo_root_file_is_an_empty_list_by_default():
    overrides = load_overrides(REPO_ROOT / "identity_overrides.yaml")
    assert overrides == []


def test_load_overrides_parses_populated_file(tmp_path):
    path = tmp_path / "identity_overrides.yaml"
    path.write_text(
        """
        - source_type: git_name
          source_value: "Jane Doe"
          into_source_type: git_email
          into_source_value: "jane@x.org"
          reviewer: pmcfadin
          evidence: "Confirmed via JIRA comment."
        """
    )

    overrides = load_overrides(path)

    assert overrides == [
        ManualOverride(
            source_type=GIT_NAME,
            source_value="Jane Doe",
            into_source_type=GIT_EMAIL,
            into_source_value="jane@x.org",
            reviewer="pmcfadin",
            evidence="Confirmed via JIRA comment.",
        )
    ]


def test_load_overrides_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_overrides("/nonexistent/identity_overrides.yaml")


def test_load_overrides_rejects_entry_missing_required_field(tmp_path):
    path = tmp_path / "identity_overrides.yaml"
    path.write_text(
        """
        - source_type: git_name
          source_value: "Jane Doe"
          reviewer: pmcfadin
          evidence: "Missing into_* fields."
        """
    )

    with pytest.raises(IdentityResolutionError):
        load_overrides(path)


def test_load_overrides_rejects_non_list_top_level(tmp_path):
    path = tmp_path / "identity_overrides.yaml"
    path.write_text("overrides: []\n")

    with pytest.raises(IdentityResolutionError):
        load_overrides(path)


# --- link_github_commit_authors (D6, issue #52 fixup cycle 1) ---------------


def test_link_github_commit_authors_adds_high_confidence_link_to_the_email_identity():
    raws = [RawIdentifier(GIT_EMAIL, "alice@gmail.com")]
    base = resolve_identities(raws, now=NOW)
    associations = [("alice@gmail.com", "alice-gh", "sha-abc123")]

    linked = link_github_commit_authors(base.identity_link, associations, now=NOW)

    identity_id = identity_id_for(GIT_EMAIL, "alice@gmail.com")
    new_rows = [
        r
        for r in linked.to_pylist()
        if r["source_type"] == "github_login" and r["source_value"] == "alice-gh"
    ]
    assert len(new_rows) == 1
    row = new_rows[0]
    assert row["identity_id"] == identity_id
    assert row["confidence"] == "high"
    assert row["linked_by"] == "github_commit_author_v1"
    assert row["evidence"] == "GitHub commit author association, sha sha-abc123"
    # The original naive-resolution rows are untouched (append-only).
    assert linked.num_rows == base.identity_link.num_rows + 1


def test_link_github_commit_authors_is_case_insensitive_on_email_and_keeps_min_sha():
    raws = [RawIdentifier(GIT_EMAIL, "alice@gmail.com")]
    base = resolve_identities(raws, now=NOW)
    associations = [
        ("Alice@Gmail.com", "alice-gh", "sha-zzz"),
        ("alice@gmail.com", "alice-gh", "sha-aaa"),
    ]

    linked = link_github_commit_authors(base.identity_link, associations, now=NOW)
    new_rows = [r for r in linked.to_pylist() if r["source_type"] == "github_login"]
    assert len(new_rows) == 1
    assert new_rows[0]["evidence"] == "GitHub commit author association, sha sha-aaa"


def test_link_github_commit_authors_no_associations_returns_input_unchanged():
    raws = [RawIdentifier(GIT_EMAIL, "alice@gmail.com")]
    base = resolve_identities(raws, now=NOW)
    linked = link_github_commit_authors(base.identity_link, [], now=NOW)
    assert linked is base.identity_link


def test_link_github_commit_authors_ignores_incomplete_triples():
    raws = [RawIdentifier(GIT_EMAIL, "alice@gmail.com")]
    base = resolve_identities(raws, now=NOW)
    associations = [("alice@gmail.com", "", "sha-1"), ("", "some-login", "sha-2")]
    linked = link_github_commit_authors(base.identity_link, associations, now=NOW)
    assert linked is base.identity_link


def test_link_github_commit_authors_is_deterministic_across_reruns():
    raws = [RawIdentifier(GIT_EMAIL, "alice@gmail.com")]
    base = resolve_identities(raws, now=NOW)
    associations = [("alice@gmail.com", "alice-gh", "sha-abc123")]

    first = link_github_commit_authors(base.identity_link, associations, now=NOW)
    second = link_github_commit_authors(base.identity_link, associations, now=NOW)
    assert first.equals(second)
