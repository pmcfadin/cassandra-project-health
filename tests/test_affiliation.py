"""Tests for project_health.normalize.affiliation (issue #52, D6)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pyarrow as pa
import pytest

from project_health.normalize.affiliation import (
    AffiliationError,
    DomainOrgPeriod,
    build_affiliation_periods,
    load_affiliations_file,
    load_github_company_map,
    load_org_aliases,
    load_org_domains,
)
from project_health.normalize.identity import RawIdentifier, identity_id_for, resolve_identities
from project_health.schema import get_schema, validate

UTC = timezone.utc


# --- load_org_domains --------------------------------------------------


def test_load_org_domains_missing_file_returns_empty(tmp_path):
    assert load_org_domains(tmp_path / "nope.yaml") == {}


def test_load_org_domains_parses_flat_string_form_and_lowercases(tmp_path):
    path = tmp_path / "org_domains.yaml"
    path.write_text("Apple.com: Apple\ninstaclustr.com: Instaclustr\n")
    result = load_org_domains(path)
    assert set(result) == {"apple.com", "instaclustr.com"}
    assert result["apple.com"] == [DomainOrgPeriod(organization="Apple", start=None, end=None)]
    assert result["instaclustr.com"] == [
        DomainOrgPeriod(organization="Instaclustr", start=None, end=None)
    ]


def test_load_org_domains_parses_dated_list_form_for_an_acquisition(tmp_path):
    path = tmp_path / "org_domains.yaml"
    path.write_text(
        "instaclustr.com:\n"
        "  - org: Instaclustr\n"
        "    end: 2022-05-20\n"
        "  - org: NetApp\n"
        "    start: 2022-05-20\n"
    )
    result = load_org_domains(path)
    periods = result["instaclustr.com"]
    assert periods == [
        DomainOrgPeriod(organization="Instaclustr", start=None, end=date(2022, 5, 20)),
        DomainOrgPeriod(organization="NetApp", start=date(2022, 5, 20), end=None),
    ]


def test_load_org_domains_rejects_end_before_start_in_dated_list(tmp_path):
    path = tmp_path / "org_domains.yaml"
    path.write_text("example.com:\n  - org: X\n    start: 2022-01-01\n    end: 2021-01-01\n")
    with pytest.raises(AffiliationError, match="must be after"):
        load_org_domains(path)


@pytest.mark.parametrize("domain", ["gmail.com", "GMAIL.COM", "apache.org", "yahoo.com"])
def test_load_org_domains_rejects_freemail_and_apache_org(tmp_path, domain):
    path = tmp_path / "org_domains.yaml"
    path.write_text(f"{domain}: Some Org\n")
    with pytest.raises(AffiliationError, match="never be mapped"):
        load_org_domains(path)


def test_load_org_domains_rejects_empty_organization(tmp_path):
    path = tmp_path / "org_domains.yaml"
    path.write_text("example.com: \"\"\n")
    with pytest.raises(AffiliationError, match="empty organization"):
        load_org_domains(path)


def test_load_org_domains_rejects_non_mapping(tmp_path):
    path = tmp_path / "org_domains.yaml"
    path.write_text("- not\n- a\n- mapping\n")
    with pytest.raises(AffiliationError, match="mapping"):
        load_org_domains(path)


# --- load_affiliations_file ---------------------------------------------


def test_load_affiliations_file_missing_file_returns_empty(tmp_path):
    assert load_affiliations_file(tmp_path / "nope.yaml") == {}


def test_load_affiliations_file_parses_dated_range_keyed_by_lowercased_email(tmp_path):
    path = tmp_path / "affiliations.yaml"
    path.write_text(
        "Alice@Example.ORG:\n"
        "  - org: Example Corp\n"
        "    start: 2020-01-01\n"
        "    end: 2022-12-31\n"
        "  - org: Another Inc\n"
        "    start: 2023-01-01\n"
    )
    result = load_affiliations_file(path)
    assert set(result) == {"alice@example.org"}
    entries = result["alice@example.org"]
    assert entries[0].organization == "Example Corp"
    assert entries[0].start == date(2020, 1, 1)
    assert entries[0].end == date(2022, 12, 31)
    assert entries[1].organization == "Another Inc"
    assert entries[1].end is None


def test_load_affiliations_file_keeps_github_handle_case_sensitive(tmp_path):
    path = tmp_path / "affiliations.yaml"
    path.write_text("github:SomeHandle:\n  - org: Example Corp\n    start: 2020-01-01\n")
    result = load_affiliations_file(path)
    assert set(result) == {"github:SomeHandle"}


def test_load_affiliations_file_rejects_end_before_start(tmp_path):
    path = tmp_path / "affiliations.yaml"
    path.write_text("a@b.org:\n  - org: X\n    start: 2022-01-01\n    end: 2021-01-01\n")
    with pytest.raises(AffiliationError, match="must be after"):
        load_affiliations_file(path)


def test_load_affiliations_file_rejects_missing_required_fields(tmp_path):
    path = tmp_path / "affiliations.yaml"
    path.write_text("a@b.org:\n  - org: X\n")
    with pytest.raises(AffiliationError, match="'org' and 'start'"):
        load_affiliations_file(path)


def test_load_affiliations_file_placeholder_repo_file_loads_empty():
    """The real, checked-in repo-root `affiliations.yaml` (still a
    placeholder -- no reviewed entries yet) must load as an empty mapping,
    not error."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    assert load_affiliations_file(repo_root / "affiliations.yaml") == {}


def test_repo_org_domains_file_loads_and_has_no_freemail_or_apache_org():
    """The real, checked-in repo-root `org_domains.yaml` must load cleanly
    under this module's own D6 guardrail."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    domains = load_org_domains(repo_root / "org_domains.yaml")
    assert domains  # the seed list is non-empty
    assert "gmail.com" not in domains
    assert "apache.org" not in domains


def test_repo_org_domains_file_has_dated_instaclustr_netapp_and_datastax_ibm_periods():
    """Issue #52 fixup cycle 1: verified acquisition dates (Instaclustr ->
    NetApp, DataStax -> IBM) must actually be dated ranges in the checked-in
    file, not flat undated entries."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    domains = load_org_domains(repo_root / "org_domains.yaml")

    instaclustr_periods = domains["instaclustr.com"]
    assert [p.organization for p in instaclustr_periods] == ["Instaclustr", "NetApp"]
    assert instaclustr_periods[0].end == date(2022, 5, 20)
    assert instaclustr_periods[1].start == date(2022, 5, 20)

    datastax_periods = domains["datastax.com"]
    assert [p.organization for p in datastax_periods] == ["DataStax", "IBM"]
    assert datastax_periods[0].end is not None
    assert datastax_periods[1].start == datastax_periods[0].end


# --- load_org_aliases ----------------------------------------------------


def test_load_org_aliases_missing_file_returns_empty(tmp_path):
    assert load_org_aliases(tmp_path / "nope.yaml") == {}


def test_load_org_aliases_normalizes_at_sign_and_case(tmp_path):
    path = tmp_path / "org_aliases.yaml"
    path.write_text("apple: Apple\n")
    aliases = load_org_aliases(path)
    assert aliases == {"apple": "Apple"}


def test_load_org_aliases_rejects_empty_organization(tmp_path):
    path = tmp_path / "org_aliases.yaml"
    path.write_text('apple: ""\n')
    with pytest.raises(AffiliationError, match="empty organization"):
        load_org_aliases(path)


def test_repo_org_aliases_file_loads_and_matches_apple_domain_entry():
    """The real, checked-in repo-root `org_aliases.yaml` must load cleanly
    and its "apple" alias must match `org_domains.yaml`'s "Apple" org name
    exactly (same canonical spelling used across both reviewed files)."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    aliases = load_org_aliases(repo_root / "org_aliases.yaml")
    assert aliases  # the seed list is non-empty
    assert aliases["apple"] == "Apple"


# --- load_github_company_map ---------------------------------------------


def _github_profile_table(rows: list[dict]) -> pa.Table:
    schema = get_schema("github_profile")
    return validate("github_profile", pa.Table.from_pylist(rows, schema=schema))


def test_load_github_company_map_keeps_latest_fetch_and_drops_blank_company():
    table = _github_profile_table(
        [
            {
                "login": "alice",
                "company": "Old Corp",
                "fetched_at": datetime(2024, 1, 1, tzinfo=UTC),
                "source_snapshot_id": "s1",
            },
            {
                "login": "alice",
                "company": "New Corp",
                "fetched_at": datetime(2024, 6, 1, tzinfo=UTC),
                "source_snapshot_id": "s2",
            },
            {
                "login": "bob",
                "company": None,
                "fetched_at": datetime(2024, 1, 1, tzinfo=UTC),
                "source_snapshot_id": "s1",
            },
            {
                "login": "carol",
                "company": "   ",
                "fetched_at": datetime(2024, 1, 1, tzinfo=UTC),
                "source_snapshot_id": "s1",
            },
        ]
    )
    result = load_github_company_map(table)
    assert set(result) == {"alice"}
    company, fetched_at = result["alice"]
    assert company == "New Corp"
    assert fetched_at == datetime(2024, 6, 1, tzinfo=UTC)


# --- build_affiliation_periods -------------------------------------------


def test_build_affiliation_periods_curated_wins_over_domain_and_domain_over_none():
    now = datetime(2026, 9, 25, tzinfo=UTC)
    raws = [
        RawIdentifier("git_email", "curated@known.example"),
        RawIdentifier("git_email", "domain@apple.com"),
        RawIdentifier("git_email", "nobody@nowhere.example"),
    ]
    identity_link = resolve_identities(raws, now=now).identity_link

    curated = load_affiliations_file_from_dict(
        {"curated@known.example": [{"org": "Curated Org", "start": "2020-01-01"}]}
    )
    org_domains = {"apple.com": [DomainOrgPeriod(organization="Apple", start=None, end=None)]}

    result = build_affiliation_periods(
        identity_link=identity_link, curated=curated, org_domains=org_domains
    )
    rows = result.to_pylist()

    curated_id = identity_id_for("git_email", "curated@known.example")
    domain_id = identity_id_for("git_email", "domain@apple.com")
    nobody_id = identity_id_for("git_email", "nobody@nowhere.example")

    by_identity = {}
    for row in rows:
        by_identity.setdefault(row["identity_id"], []).append(row)

    assert by_identity[curated_id][0]["organization"] == "Curated Org"
    assert by_identity[curated_id][0]["source"] == "curated"
    assert by_identity[domain_id][0]["organization"] == "Apple"
    assert by_identity[domain_id][0]["source"] == "email_domain"
    # `nobody@nowhere.example` has no curated entry and no matching domain --
    # it gets NO affiliation_period row at all (unknown is the caller's
    # default for "no covering row", not a row this module writes).
    assert nobody_id not in by_identity


def load_affiliations_file_from_dict(raw: dict) -> dict:
    """Test helper: build the same `{key: [CuratedAffiliation, ...]}` shape
    `load_affiliations_file` returns, without writing a YAML file to disk."""
    import tempfile
    from pathlib import Path

    import yaml

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "affiliations.yaml"
        path.write_text(yaml.safe_dump(raw))
        return load_affiliations_file(path)


def _identity_link_with_github_login(email: str, login: str, now: datetime) -> pa.Table:
    """Hand-built `identity_link` linking `email`'s identity to `login`,
    mimicking what `normalize.identity.link_github_commit_authors` produces
    -- identity.py's RAW_TYPES doesn't (yet) accept `github_login` through
    the naive resolver directly, so this builds the extra row by hand."""
    identity_id = identity_id_for("git_email", email)
    raws = [RawIdentifier("git_email", email)]
    base_link = resolve_identities(raws, now=now).identity_link
    extra_row = {
        "link_id": "github-commit-author-link",
        "identity_id": identity_id,
        "source_type": "github_login",
        "source_value": login,
        "confidence": "high",
        "evidence": "test",
        "linked_by": "github_commit_author_v1",
        "linked_at": now,
    }
    return pa.concat_tables([base_link, pa.Table.from_pylist([extra_row], schema=base_link.schema)])


def test_build_affiliation_periods_github_company_requires_a_reviewed_alias():
    """D6: an unmatched `company` string never becomes an organization --
    only an exact `org_aliases.yaml` entry does (issue #52 fixup cycle 1)."""
    now = datetime(2026, 9, 25, tzinfo=UTC)
    email = "hasgithub@nowhere.example"
    identity_id = identity_id_for("git_email", email)
    identity_link = _identity_link_with_github_login(email, "octo-dev", now)

    github_profile = _github_profile_table(
        [
            {
                "login": "octo-dev",
                "company": "@examplecorp",
                "fetched_at": now,
                "source_snapshot_id": "s1",
            }
        ]
    )

    # No org_aliases given at all -- "@examplecorp" is unmatched free text,
    # so it stays unknown (no row), never fuzzy-matched into an org.
    result = build_affiliation_periods(identity_link=identity_link, github_profile=github_profile)
    rows = [r for r in result.to_pylist() if r["identity_id"] == identity_id]
    assert rows == []


def test_build_affiliation_periods_github_company_matches_reviewed_alias():
    now = datetime(2026, 9, 25, tzinfo=UTC)
    email = "hasgithub@nowhere.example"
    identity_id = identity_id_for("git_email", email)
    identity_link = _identity_link_with_github_login(email, "octo-dev", now)

    github_profile = _github_profile_table(
        [
            {
                "login": "octo-dev",
                "company": "@ExampleCorp",
                "fetched_at": now,
                "source_snapshot_id": "s1",
            }
        ]
    )
    org_aliases = {"examplecorp": "Example Corp"}

    result = build_affiliation_periods(
        identity_link=identity_link, org_aliases=org_aliases, github_profile=github_profile
    )
    rows = [r for r in result.to_pylist() if r["identity_id"] == identity_id]
    assert len(rows) == 1
    assert rows[0]["organization"] == "Example Corp"
    assert rows[0]["source"] == "github_company"
    # Issue #52 fixup cycle 2: bounded to the trailing lookback before
    # `fetched_at` (default 24 months), open-ended forward -- never applied
    # to a person's entire history. Month arithmetic fixes the day to 1
    # (same convention as metrics.windows.add_months).
    assert rows[0]["effective_from"] == date(2024, 9, 1)
    assert rows[0]["effective_to"] is None


def test_build_affiliation_periods_github_company_lookback_is_configurable():
    now = datetime(2026, 9, 25, tzinfo=UTC)
    email = "hasgithub@nowhere.example"
    identity_id = identity_id_for("git_email", email)
    identity_link = _identity_link_with_github_login(email, "octo-dev", now)
    github_profile = _github_profile_table(
        [
            {
                "login": "octo-dev",
                "company": "@ExampleCorp",
                "fetched_at": now,
                "source_snapshot_id": "s1",
            }
        ]
    )
    org_aliases = {"examplecorp": "Example Corp"}

    result = build_affiliation_periods(
        identity_link=identity_link,
        org_aliases=org_aliases,
        github_profile=github_profile,
        github_company_lookback_months=6,
    )
    rows = [r for r in result.to_pylist() if r["identity_id"] == identity_id]
    assert rows[0]["effective_from"] == date(2026, 3, 1)
    assert rows[0]["effective_to"] is None
