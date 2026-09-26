"""Organizational affiliation resolution (D6; ARCHITECTURE.md §3
`affiliation_period`; issue #52, fixup cycles 1-2).

Produces `affiliation_period` rows for the organizational-diversity metrics
(`elephant_factor`, `organizational_hhi`, `single_org_share`,
`unknown_affiliation_rate` -- METRICS.md §5) from three sources, in
descending order of trust (D6: "`affiliations.yaml` is the curated override
and wins over heuristics"):

1. **curated** -- `affiliations.yaml` at the repo root: a PR-reviewed,
   dated-range mapping keyed by email (lowercased) or `"github:<handle>"`.
2. **email_domain** -- `org_domains.yaml`: a reviewed domain -> organization
   map, each domain either a single (undated) organization or a dated list
   of organizations (for an acquisition that changed who a domain's
   employees work for -- e.g. `instaclustr.com` before/after NetApp's
   acquisition closed, verified against NetApp's own SEC 8-K). Freemail
   providers (gmail.com, etc.) and `apache.org` are explicitly **not**
   organizations under D6 (an ASF address says only "has an ASF account,"
   not who employs someone) -- `load_org_domains` raises if either is ever
   mapped to an org, so a future edit can't silently reintroduce that
   shortcut.
3. **github_company** -- a GitHub profile's public `company` field, fetched
   once per login and cached in the `github_profile` raw table
   (`collectors/github_profile.py`), for logins found via
   `collectors/github_commit_authors.py`'s GitHub-asserted commit-author
   association (this is what makes this source useful for the
   gmail.com/apache.org/personal-domain majority that `email_domain` can
   never resolve -- verified live, 2026-09-25: a contributor committing as
   `...@gmail.com` whose GitHub profile names `"Apple"`). The free-text
   `company` field is normalized (a leading `@` stripped, casefolded) and
   looked up in `org_aliases.yaml`'s reviewed alias map; **unmatched text
   resolves to unknown, never fuzzy-matched** (D6) -- this source never
   invents an organization name from raw profile text. It is also a
   **current-employer signal only** (fixup cycle 2): a `company` value is
   observed once, at `fetched_at`, and says nothing about who a contributor
   worked for years earlier, so it is bounded to a trailing lookback window
   ending at `fetched_at` (`DEFAULT_GITHUB_COMPANY_LOOKBACK_MONTHS`,
   configurable) rather than applied to that person's entire history -- a
   commit older than the lookback resolves via `email_domain`/`curated`, or
   stays `unknown`, exactly like any other date outside a dated row's range.

Anything left over resolves to `UNKNOWN_ORG` -- D6's "never guess" rule:
this module never infers an organization from a display name, writing
style, or anything other than the three sources above. `unknown` is shown
in the denominator by every organizational-diversity metric, never silently
redistributed or dropped (METRICS.md §0.5).

`affiliation_period` is derived, deterministic data (a pure function of
`identity_link` + the YAML files + the accumulated `github_profile` raw
table), so -- like `person_identity`/`identity_link` (issue #6) -- it is
recomputed fresh every run rather than persisted to the data branch (D3:
"never incremental").
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import yaml

from project_health.schema import get_schema, validate

# D6, issue #52 fixup cycle 2: a GitHub profile's `company` field is a
# CURRENT-employer signal, observed at `fetched_at` -- it says nothing about
# who a contributor worked for years earlier. Applying it to that person's
# *entire* commit history would credit old commits to their current employer
# (inflating that employer's historical share and distorting trend lines).
# `build_affiliation_periods` therefore bounds every `github_company` row to
# a trailing lookback window ending at `fetched_at` (`effective_from =
# fetched_at` minus this many months, `effective_to = None`, i.e. open-
# ended forward -- a company observed today is assumed to still hold as of
# "now" and beyond, per D6's bias toward the most-recent, most-verifiable
# reading): a commit older than the lookback resolves via `email_domain`/
# `curated` instead, or stays `unknown` if neither covers it -- never
# silently back-filled onto a stale current-employer guess. Configurable via
# `projects/<id>.yaml`'s `github_company_lookback_months` (see `config.py`);
# this is the default absent that setting.
DEFAULT_GITHUB_COMPANY_LOOKBACK_MONTHS = 24

UNKNOWN_ORG = "unknown"

SOURCE_CURATED = "curated"
SOURCE_EMAIL_DOMAIN = "email_domain"
SOURCE_GITHUB_COMPANY = "github_company"

AFFILIATION_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "cassandra-project-health.affiliation")

# D6: an ASF address says only "has an ASF account," not who employs someone,
# and mainstream freemail providers say nothing about employer at all --
# neither is ever a valid `org_domains.yaml` target. This list is
# deliberately conservative (common global providers); it exists as a
# guardrail against an accidental future mapping, not as an exhaustive
# freemail registry.
FREEMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "yahoo.com",
        "outlook.com",
        "hotmail.com",
        "icloud.com",
        "protonmail.com",
        "aol.com",
        "live.com",
        "gmx.com",
        "msn.com",
        "me.com",
        "mail.com",
        "fastmail.com",
        "yandex.com",
        "163.com",
        "qq.com",
        # Added issue #52 fixup cycle 1, observed in real Cassandra commit
        # history (`git log --since=3.years`): regional freemail/webmail
        # providers, not corporate domains.
        "yahoo.fr",
        "ya.ru",
        "interia.pl",
    }
)
NON_ORG_DOMAINS = FREEMAIL_DOMAINS | {"apache.org"}


class AffiliationError(ValueError):
    """Raised on a malformed `affiliations.yaml` / `org_domains.yaml` entry."""


@dataclass(frozen=True)
class CuratedAffiliation:
    """One dated organization period from `affiliations.yaml`."""

    organization: str
    start: date
    end: date | None


@dataclass(frozen=True)
class DomainOrgPeriod:
    """One (possibly dated) organization period for an `org_domains.yaml`
    domain. `start`/`end` are `None` for the common case (a domain that has
    always belonged to the same organization, with no acquisition to date);
    both are set for a domain that changed owners (an acquisition)."""

    organization: str
    start: date | None
    end: date | None


def _add_months(d: date, n: int) -> date:
    """`d`'s month, shifted by `n` months (`n` may be negative), day fixed
    to 1. A local copy of `metrics.windows.add_months`'s exact logic --
    not imported from there, since `metrics/__init__.py` eagerly imports
    `metrics.engine`, which itself imports this module, and `metrics.windows`
    is only reachable through the `metrics` package's own `__init__.py`;
    importing it from here would be a circular import. This module has no
    other reason to depend on the `metrics` package, so a small, independent
    copy (identical arithmetic, covered by its own tests here) keeps
    `normalize/` dependency-free the same way `normalize/identity.py` is.
    """
    month_index = d.month - 1 + n
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def _parse_date(value: object, *, context: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise AffiliationError(f"{context}: invalid date {value!r}") from exc


def load_affiliations_file(path: str | Path) -> dict[str, list[CuratedAffiliation]]:
    """Load `affiliations.yaml`'s curated overrides.

    Returns `{}` if `path` doesn't exist (a project with no curated
    affiliations yet, or a caller that didn't configure one) -- unlike
    `identity_overrides.yaml`'s loader, a missing curated affiliations file
    is not an error, since D6 never requires one to exist before the
    organizational metrics can run (everything simply resolves to
    `email_domain`/`github_company`/`unknown`).

    Keys are normalized: an email key is lowercased; a GitHub-handle key
    must be written as `"github:<handle>"` (per the file's own documented
    format) and is kept case-sensitive, matching GitHub login semantics.
    """
    resolved = Path(path)
    if not resolved.is_file():
        return {}

    raw = yaml.safe_load(resolved.read_text())
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AffiliationError(
            f"{resolved}: expected a top-level YAML mapping of identifier -> affiliation list, "
            f"got {type(raw).__name__}"
        )

    result: dict[str, list[CuratedAffiliation]] = {}
    for identifier, entries in raw.items():
        if not isinstance(entries, list):
            raise AffiliationError(f"{resolved}: entry for {identifier!r} must be a YAML list")
        key = str(identifier).strip()
        if not key.lower().startswith("github:"):
            key = key.lower()

        parsed: list[CuratedAffiliation] = []
        for index, entry in enumerate(entries):
            context = f"{resolved}: {identifier!r}[{index}]"
            if not isinstance(entry, dict) or "org" not in entry or "start" not in entry:
                raise AffiliationError(f"{context} must be a mapping with 'org' and 'start'")
            org = str(entry["org"]).strip()
            if not org:
                raise AffiliationError(f"{context}: 'org' must not be empty")
            start = _parse_date(entry["start"], context=context)
            end_raw = entry.get("end")
            end = None if end_raw is None else _parse_date(end_raw, context=context)
            if end is not None and end <= start:
                raise AffiliationError(f"{context}: 'end' ({end}) must be after 'start' ({start})")
            parsed.append(CuratedAffiliation(organization=org, start=start, end=end))
        result[key] = parsed
    return result


def load_org_domains(path: str | Path) -> dict[str, list[DomainOrgPeriod]]:
    """Load `org_domains.yaml`'s reviewed email-domain -> organization map.

    Each domain's value is either:

    - a plain string (`domain: OrgName`) -- the common case, one
      organization for all time; or
    - a list of dated periods (`domain: [{org: X, end: ...}, {org: Y,
      start: ...}, ...]`, same dated-range shape as `affiliations.yaml`'s
      entries except `start` is optional here -- omitted/`None` means "since
      always") -- for a domain whose organization changed (an acquisition).

    Returns `{}` if `path` doesn't exist. Raises `AffiliationError` if any
    domain is a freemail provider or `apache.org` (D6 -- see module
    docstring and `NON_ORG_DOMAINS`), or if a domain maps to an empty
    organization name.
    """
    resolved = Path(path)
    if not resolved.is_file():
        return {}

    raw = yaml.safe_load(resolved.read_text())
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AffiliationError(
            f"{resolved}: expected a top-level YAML mapping of domain -> organization, "
            f"got {type(raw).__name__}"
        )

    mapping: dict[str, list[DomainOrgPeriod]] = {}
    for domain, value in raw.items():
        normalized_domain = str(domain).strip().lower()
        if normalized_domain in NON_ORG_DOMAINS:
            raise AffiliationError(
                f"{resolved}: {normalized_domain!r} is a freemail provider or apache.org and "
                "must never be mapped to an organization (D6) -- unresolved affiliation stays "
                "'unknown', it is never guessed"
            )

        if isinstance(value, str):
            normalized_org = value.strip()
            if not normalized_org:
                raise AffiliationError(
                    f"{resolved}: domain {domain!r} maps to an empty organization"
                )
            mapping[normalized_domain] = [
                DomainOrgPeriod(organization=normalized_org, start=None, end=None)
            ]
            continue

        if not isinstance(value, list):
            raise AffiliationError(
                f"{resolved}: domain {domain!r} must map to a string or a list of dated "
                f"periods, got {type(value).__name__}"
            )
        periods: list[DomainOrgPeriod] = []
        for index, entry in enumerate(value):
            context = f"{resolved}: {domain!r}[{index}]"
            if not isinstance(entry, dict) or "org" not in entry:
                raise AffiliationError(f"{context} must be a mapping with 'org'")
            org = str(entry["org"]).strip()
            if not org:
                raise AffiliationError(f"{context}: 'org' must not be empty")
            start_raw = entry.get("start")
            start = None if start_raw is None else _parse_date(start_raw, context=context)
            end_raw = entry.get("end")
            end = None if end_raw is None else _parse_date(end_raw, context=context)
            if start is not None and end is not None and end <= start:
                raise AffiliationError(f"{context}: 'end' ({end}) must be after 'start' ({start})")
            periods.append(DomainOrgPeriod(organization=org, start=start, end=end))
        mapping[normalized_domain] = periods
    return mapping


def _normalize_company_text(raw: str) -> str:
    """Normalize a GitHub profile `company` string for `org_aliases.yaml`
    lookup: strip a leading `@` (a common GitHub-handle-style convention,
    e.g. `"@apple"`) and surrounding whitespace, then casefold. This is
    string normalization only -- never fuzzy/partial matching (D6)."""
    return raw.strip().lstrip("@").strip().casefold()


def load_org_aliases(path: str | Path) -> dict[str, str]:
    """Load `org_aliases.yaml`'s reviewed GitHub-`company`-text -> canonical
    organization name map. Keys are normalized the same way
    `_normalize_company_text` normalizes an observed `company` value, so a
    lookup is always an exact match against a reviewed alias -- never a
    fuzzy/substring one (D6: "unmatched free text stays unknown").

    Returns `{}` if `path` doesn't exist.
    """
    resolved = Path(path)
    if not resolved.is_file():
        return {}

    raw = yaml.safe_load(resolved.read_text())
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AffiliationError(
            f"{resolved}: expected a top-level YAML mapping of company text -> organization, "
            f"got {type(raw).__name__}"
        )

    mapping: dict[str, str] = {}
    for alias, org in raw.items():
        normalized_alias = _normalize_company_text(str(alias))
        normalized_org = str(org).strip()
        if not normalized_alias:
            raise AffiliationError(f"{resolved}: alias {alias!r} normalizes to an empty string")
        if not normalized_org:
            raise AffiliationError(f"{resolved}: alias {alias!r} maps to an empty organization")
        mapping[normalized_alias] = normalized_org
    return mapping


def load_github_company_map(github_profile: pa.Table) -> dict[str, tuple[str, datetime]]:
    """`{login: (company, fetched_at)}` from the accumulated `github_profile`
    raw table, keeping only logins whose most-recently-fetched row has a
    non-empty `company` field. A login fetched more than once (re-collected)
    keeps its latest `fetched_at` row, matching this project's other
    raw-table "keep the latest snapshot" read-time dedupe convention
    (`pipeline.py`). `fetched_at` is carried through (not dropped) because
    `build_affiliation_periods` needs it to bound the resulting
    `affiliation_period` row to a trailing lookback window (issue #52 fixup
    cycle 2 -- see module docstring's "current-employer signal" note).
    """
    latest: dict[str, tuple] = {}
    for row in github_profile.to_pylist():
        login = row["login"]
        fetched_at = row["fetched_at"]
        current = latest.get(login)
        if current is None or fetched_at > current[0]:
            latest[login] = (fetched_at, row["company"])
    return {
        login: (company.strip(), fetched_at)
        for login, (fetched_at, company) in latest.items()
        if company and company.strip()
    }


def _entry_id(
    identity_id: str, source: str, organization: str, start: date | None, end: date | None
) -> str:
    return str(
        uuid.uuid5(
            AFFILIATION_NAMESPACE,
            f"{identity_id}:{source}:{organization}:{start.isoformat() if start else ''}:"
            f"{end.isoformat() if end else ''}",
        )
    )


def _row(
    identity_id: str,
    organization: str,
    start: date | None,
    end: date | None,
    source: str,
    evidence: str,
) -> dict:
    return {
        "entry_id": _entry_id(identity_id, source, organization, start, end),
        "identity_id": identity_id,
        "organization": organization,
        "effective_from": start,
        "effective_to": end,
        "source": source,
        "evidence": evidence,
    }


def build_affiliation_periods(
    *,
    identity_link: pa.Table,
    curated: dict[str, list[CuratedAffiliation]] | None = None,
    org_domains: dict[str, list[DomainOrgPeriod]] | None = None,
    org_aliases: dict[str, str] | None = None,
    github_profile: pa.Table | None = None,
    github_company_lookback_months: int = DEFAULT_GITHUB_COMPANY_LOOKBACK_MONTHS,
) -> pa.Table:
    """Build `affiliation_period` rows for every identity `identity_link`
    knows about, from the curated file, the email-domain map, and the
    cached GitHub profile company field, in that trust order (module
    docstring). Multiple rows per identity are expected and intended --
    `metrics.engine._organization_commit_counts` resolves the winning row
    per commit at query time (curated's dated range first, so a person
    whose curated entry doesn't cover a given commit's date still falls
    through to the heuristic rows for that date).

    An identity with no matching curated/heuristic row emits **no** row
    here at all -- callers (the metrics engine) treat "no covering
    `affiliation_period` row" as `UNKNOWN_ORG`, per D6's "never guess."

    `github_company_lookback_months` (issue #52 fixup cycle 2): a
    `github_company` row is bounded to the trailing N months before that
    login's `company` field was fetched -- see module docstring's
    "current-employer signal" note. A commit older than the lookback is
    outside this row's `effective_from`/`effective_to` range and falls
    through to `email_domain`/`curated`, or `unknown`, exactly like any
    other date outside a dated row's range.
    """
    curated = curated or {}
    org_domains = org_domains or {}
    org_aliases = org_aliases or {}
    github_company_by_login = (
        load_github_company_map(github_profile) if github_profile is not None else {}
    )

    by_identity: dict[str, list[tuple[str, str]]] = {}
    for row in identity_link.to_pylist():
        by_identity.setdefault(row["identity_id"], []).append(
            (row["source_type"], row["source_value"])
        )

    rows: list[dict] = []
    for identity_id, links in sorted(by_identity.items()):
        emails = sorted({value for source_type, value in links if source_type == "git_email"})
        # `github_login` links come from two places: a future collector that
        # extracts it as a raw identifier directly (none does yet -- see
        # `metrics/engine.py`'s `_FIELD_TO_RAW_TYPE` note), and, since issue
        # #52 fixup cycle 1, `normalize.identity.link_github_commit_authors`'
        # automated high-confidence links from GitHub's own commit-author
        # association (`collectors/github_commit_authors.py`).
        logins = sorted({value for source_type, value in links if source_type == "github_login"})

        for email in emails:
            for affiliation in curated.get(email.lower(), []):
                rows.append(
                    _row(
                        identity_id,
                        affiliation.organization,
                        affiliation.start,
                        affiliation.end,
                        SOURCE_CURATED,
                        f"affiliations.yaml entry for {email}",
                    )
                )
        for login in logins:
            for affiliation in curated.get(f"github:{login}", []):
                rows.append(
                    _row(
                        identity_id,
                        affiliation.organization,
                        affiliation.start,
                        affiliation.end,
                        SOURCE_CURATED,
                        f"affiliations.yaml entry for github:{login}",
                    )
                )

        for email in emails:
            domain = email.rsplit("@", 1)[-1].lower()
            for period in org_domains.get(domain, []):
                rows.append(
                    _row(
                        identity_id,
                        period.organization,
                        period.start,
                        period.end,
                        SOURCE_EMAIL_DOMAIN,
                        f"email domain {domain!r} -> {period.organization!r} (org_domains.yaml)",
                    )
                )

        for login in logins:
            company_entry = github_company_by_login.get(login)
            if not company_entry:
                continue
            raw_company, fetched_at = company_entry
            # D6: unmatched free text stays unknown, never fuzzy-matched --
            # only an exact, reviewed org_aliases.yaml entry produces a row.
            canonical_org = org_aliases.get(_normalize_company_text(raw_company))
            if canonical_org:
                # Issue #52 fixup cycle 2: bound to a trailing lookback
                # before `fetched_at` -- a current-employer signal is never
                # back-filled onto a commit older than the lookback (module
                # docstring).
                lookback_start = _add_months(fetched_at.date(), -github_company_lookback_months)
                rows.append(
                    _row(
                        identity_id,
                        canonical_org,
                        lookback_start,
                        None,
                        SOURCE_GITHUB_COMPANY,
                        f"GitHub profile company field {raw_company!r} for @{login}, fetched "
                        f"{fetched_at.date().isoformat()} (org_aliases.yaml -> {canonical_org!r}; "
                        f"bounded to the trailing {github_company_lookback_months} months, "
                        f"effective_from={lookback_start.isoformat()})",
                    )
                )

    schema = get_schema("affiliation_period")
    table = pa.Table.from_pylist(rows, schema=schema) if rows else schema.empty_table()
    return validate("affiliation_period", table)
