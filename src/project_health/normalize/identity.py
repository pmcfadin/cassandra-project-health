"""Naive identity resolution (M0) — ARCHITECTURE.md §3 unresolved-identity rule.

Deliberately naive, per ROADMAP.md's M0 risk note ("Identity resolution done
naively (email-exact-match only) — acceptable here, must be revisited at M2
before it's load-bearing for contributor-count metrics"):

- An identical, normalized `git_email` (lowercased) resolves to one identity.
- An identical, normalized `jira_username` resolves to one identity.
- Everything else — most importantly a `git_name` (commit-trailer reviewer
  names aren't emails) — gets its own new identity.
- **No cross-type merging happens automatically in M0.** A commit-trailer
  name is never linked to a git email or JIRA username, even when the
  strings plainly refer to the same person. Instead, a same-looking name
  across two different raw-identifier types is recorded as a `low`
  confidence row in a separate `identity_candidates` output, for a human to
  review later — it never auto-attaches (ARCHITECTURE.md §3: "Links at
  confidence = medium or low never auto-attach").
- Every identity this module creates is `status = 'provisional'` — there is
  no source of truth in M0 (e.g. an ASF roster) that would justify
  `'resolved'`, so nothing is ever silently promoted (issue #6's own title:
  "naive identity resolution ... provisional by default").
- `identity_overrides.yaml` (repo root) is the only way two identities are
  ever merged for metrics purposes: each entry is a human-authored,
  PR-reviewed decision that adds a *new* `identity_link` row
  (`linked_by='manual:<reviewer>'`, with evidence) pointing one raw
  identifier at another identity's `identity_id`. It never rewrites or
  deletes the identifier's original naive link/identity — those rows stay,
  append-only, exactly as ARCHITECTURE.md §3 requires.
- `identity_id = uuid5(IDENTITY_NAMESPACE, "<source_type>:<normalized
  value>")` — a pure function of the normalized raw identifier, not of
  processing order or wall-clock time. Re-running resolution over the same
  raw identifiers (with the same caller-supplied `now`) therefore reproduces
  byte-identical `person_identity`/`identity_link` Parquet, which is what
  the reproducibility check (ARCHITECTURE.md §9) exercises.

Metrics never need to re-run this module themselves: `build_resolver` turns
a materialized `identity_link` table (as read back from Parquet) into a
`(source_type, source_value) -> identity_id` function, per
`schema/README.md`'s "Metrics resolve identities by joining a fact table's
raw identifier column(s) against `identity_link.source_value`" contract.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import yaml

from project_health.schema import validate

# --- Raw identifier types this resolver understands -------------------------
# (schema/README.md "Raw identifiers vs. resolved identity"; the mailing-list
# and github_login source types identity_link.source_type also allows aren't
# produced by any M0 collector yet, so they're out of scope here.)

GIT_EMAIL = "git_email"
GIT_NAME = "git_name"
JIRA_USERNAME = "jira_username"

RAW_TYPES = frozenset({GIT_EMAIL, GIT_NAME, JIRA_USERNAME})

# Fixed, time-invariant namespace: identity_id/link_id/candidate_id are pure
# functions of (source_type, normalized value) (or, for a manual link, of the
# override triple), never of wall-clock time or processing order — this is
# what makes reruns over the same input byte-identical (issue #6 acceptance
# criterion).
IDENTITY_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "cassandra-project-health.identity")

# `identity_candidates` isn't one of the shared M0 tables in
# `project_health.schema` (ARCHITECTURE.md §3's table list doesn't include
# it) — it's a resolver-internal output for human review, so its schema lives
# here rather than in `schema/tables.py`.
IDENTITY_CANDIDATE_SCHEMA = pa.schema(
    [
        pa.field("candidate_id", pa.string(), nullable=False),
        pa.field("identity_id_a", pa.string(), nullable=False),
        pa.field("identity_id_b", pa.string(), nullable=False),
        pa.field("source_type_a", pa.string(), nullable=False),
        pa.field("source_value_a", pa.string(), nullable=False),
        pa.field("source_type_b", pa.string(), nullable=False),
        pa.field("source_value_b", pa.string(), nullable=False),
        # confidence is always 'low' in M0 — candidates never auto-attach.
        pa.field("confidence", pa.string(), nullable=False),
        pa.field("evidence", pa.string(), nullable=True),
        pa.field("linked_by", pa.string(), nullable=True),
        pa.field("linked_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)


class IdentityResolutionError(ValueError):
    """Raised on malformed input to identity resolution (unknown raw type, etc.)."""


@dataclass(frozen=True)
class RawIdentifier:
    """One raw identifier as observed by a collector.

    Mirrors the raw columns `schema/README.md` documents: e.g.
    `contribution_event.author_raw_type`/`author_raw_value`/
    `author_display_name`, `review_event.reviewer_raw_type`/
    `reviewer_raw_value`, `issue.reporter_raw`/`assignee_raw`.
    """

    source_type: str
    source_value: str
    display_name: str | None = None


@dataclass(frozen=True)
class ManualOverride:
    """One reviewed entry from `identity_overrides.yaml`.

    Merges `source_type`/`source_value`'s identity into the identity that
    `into_source_type`/`into_source_value` naively resolves to.
    """

    source_type: str
    source_value: str
    into_source_type: str
    into_source_value: str
    reviewer: str
    evidence: str


@dataclass(frozen=True)
class IdentityResolution:
    """Output of `resolve_identities`."""

    person_identity: pa.Table
    identity_link: pa.Table
    identity_candidates: pa.Table
    resolver: Callable[[str, str], str | None]


@dataclass
class _Group:
    source_type: str
    normalized_value: str
    raw_values: set[str] = field(default_factory=set)
    display_names: set[str] = field(default_factory=set)


# --- Normalization ------------------------------------------------------


def _collapse_whitespace(value: str) -> str:
    return " ".join(value.strip().split())


def normalize_value(source_type: str, value: str) -> str:
    """Normalize a raw identifier's value for identity-key comparison.

    `git_email` is lowercased (acceptance criterion: two emails differing
    only by case resolve to one identity). `git_name` is whitespace-collapsed
    and casefolded (commit-trailer extraction can vary in spacing/case
    without being a different person). `jira_username` is only
    whitespace-trimmed — JIRA usernames are case-sensitive identifiers, not
    free text.
    """
    if source_type not in RAW_TYPES:
        raise IdentityResolutionError(
            f"unknown raw identifier type {source_type!r}; expected one of {sorted(RAW_TYPES)}"
        )
    collapsed = _collapse_whitespace(value)
    if source_type == GIT_EMAIL:
        return collapsed.lower()
    if source_type == GIT_NAME:
        return collapsed.casefold()
    return collapsed


def normalize_name_for_candidates(name: str) -> str:
    """Normalize a display/trailer name for cross-type candidate matching.

    Used only to *suggest* candidates (never to auto-merge): it's deliberately
    the same shape as `git_name`'s own normalization so a trailer name and a
    git-author display name that are "the same string" land on the same key.
    """
    return _collapse_whitespace(name).casefold()


def identity_id_for(source_type: str, normalized_value: str) -> str:
    """Deterministic `identity_id` for a naive-resolution identity key."""
    return str(uuid.uuid5(IDENTITY_NAMESPACE, f"{source_type}:{normalized_value}"))


def _naive_evidence(source_type: str) -> str:
    if source_type == GIT_EMAIL:
        return "exact match on lowercased git_email (naive identity resolution, M0)"
    if source_type == JIRA_USERNAME:
        return "exact match on jira_username (naive identity resolution, M0)"
    return (
        "raw git_name commit-trailer identifier; no cross-type auto-merge in M0 "
        "(ARCHITECTURE.md §3)"
    )


def _canonical_display_name(source_type: str, group: _Group) -> str | None:
    """Pick a deterministic display name for an identity, independent of
    input ordering (so reruns over the same input, in any order, agree)."""
    if source_type == GIT_NAME:
        return min(group.raw_values) if group.raw_values else None
    return min(group.display_names) if group.display_names else None


# --- Table construction --------------------------------------------------


def _rows_to_table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    if not rows:
        return schema.empty_table()
    return pa.Table.from_pylist(rows, schema=schema)


# --- Resolution ------------------------------------------------------------


def resolve_identities(
    raw_identifiers: Iterable[RawIdentifier],
    overrides: Sequence[ManualOverride] = (),
    *,
    now: datetime,
) -> IdentityResolution:
    """Resolve raw identifiers into `person_identity`/`identity_link` rows.

    `now` is required (not defaulted to wall-clock time) so callers control
    reproducibility explicitly — the pipeline passes the run's own
    `started_at`, and tests pass a fixed timestamp to prove two runs over the
    same input produce byte-identical output. Must be timezone-aware (UTC).
    """
    if now.tzinfo is None:
        raise IdentityResolutionError("now must be a timezone-aware (UTC) datetime")

    groups: dict[tuple[str, str], _Group] = {}
    for raw in raw_identifiers:
        normalized = normalize_value(raw.source_type, raw.source_value)
        key = (raw.source_type, normalized)
        group = groups.setdefault(
            key, _Group(source_type=raw.source_type, normalized_value=normalized)
        )
        group.raw_values.add(raw.source_value.strip())
        if raw.display_name:
            group.display_names.add(_collapse_whitespace(raw.display_name))

    person_rows: list[dict] = []
    link_rows: list[dict] = []
    identity_key_for_id: dict[str, tuple[str, str]] = {}
    name_index: dict[str, set[str]] = {}

    for (source_type, normalized_value), group in groups.items():
        identity_id = identity_id_for(source_type, normalized_value)
        identity_key_for_id[identity_id] = (source_type, normalized_value)

        person_rows.append(
            {
                "identity_id": identity_id,
                "display_name": _canonical_display_name(source_type, group),
                "status": "provisional",
                "created_at": now,
            }
        )

        evidence = _naive_evidence(source_type)
        for raw_value in sorted(group.raw_values):
            link_rows.append(
                {
                    "link_id": str(
                        uuid.uuid5(IDENTITY_NAMESPACE, f"link:{source_type}:{raw_value}")
                    ),
                    "identity_id": identity_id,
                    "source_type": source_type,
                    "source_value": raw_value,
                    "confidence": "exact",
                    "evidence": evidence,
                    "linked_by": "naive_identity_v1",
                    "linked_at": now,
                }
            )

        candidate_names: set[str] = set()
        if source_type == GIT_NAME:
            candidate_names.add(normalized_value)
        else:
            candidate_names.update(
                normalize_name_for_candidates(dn) for dn in group.display_names
            )
        for name_key in candidate_names:
            name_index.setdefault(name_key, set()).add(identity_id)

    candidate_rows: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for name_key, identity_ids in name_index.items():
        if len(identity_ids) < 2:
            continue
        ordered = sorted(identity_ids)
        for i, id_a in enumerate(ordered):
            for id_b in ordered[i + 1 :]:
                pair = (id_a, id_b)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                type_a, value_a = identity_key_for_id[id_a]
                type_b, value_b = identity_key_for_id[id_b]
                candidate_rows.append(
                    {
                        "candidate_id": str(
                            uuid.uuid5(IDENTITY_NAMESPACE, f"candidate:{id_a}:{id_b}")
                        ),
                        "identity_id_a": id_a,
                        "identity_id_b": id_b,
                        "source_type_a": type_a,
                        "source_value_a": value_a,
                        "source_type_b": type_b,
                        "source_value_b": value_b,
                        "confidence": "low",
                        "evidence": (
                            f"normalized name {name_key!r} matches across raw identifiers "
                            "of different types; no cross-type auto-merge in M0 "
                            "(ARCHITECTURE.md §3) — needs human review"
                        ),
                        "linked_by": "naive_identity_v1",
                        "linked_at": now,
                    }
                )

    for override in overrides:
        into_normalized = normalize_value(override.into_source_type, override.into_source_value)
        into_identity_id = identity_id_for(override.into_source_type, into_normalized)
        source_value = override.source_value.strip()
        link_rows.append(
            {
                "link_id": str(
                    uuid.uuid5(
                        IDENTITY_NAMESPACE,
                        f"manual:{override.source_type}:{source_value}:{into_identity_id}",
                    )
                ),
                "identity_id": into_identity_id,
                "source_type": override.source_type,
                "source_value": source_value,
                "confidence": "exact",
                "evidence": override.evidence,
                "linked_by": f"manual:{override.reviewer}",
                "linked_at": now,
            }
        )

    person_rows.sort(key=lambda r: r["identity_id"])
    link_rows.sort(
        key=lambda r: (r["source_type"], r["source_value"], r["linked_by"], r["identity_id"])
    )
    candidate_rows.sort(key=lambda r: r["candidate_id"])

    person_identity = validate(
        "person_identity", _rows_to_table(person_rows, _person_identity_schema())
    )
    identity_link = validate(
        "identity_link", _rows_to_table(link_rows, _identity_link_schema())
    )
    identity_candidates = _rows_to_table(candidate_rows, IDENTITY_CANDIDATE_SCHEMA)

    return IdentityResolution(
        person_identity=person_identity,
        identity_link=identity_link,
        identity_candidates=identity_candidates,
        resolver=build_resolver(identity_link),
    )


def _person_identity_schema() -> pa.Schema:
    from project_health.schema import get_schema

    return get_schema("person_identity")


def _identity_link_schema() -> pa.Schema:
    from project_health.schema import get_schema

    return get_schema("identity_link")


# --- Automated high-confidence links: GitHub commit-author association -----
# (D6, issue #52 fixup cycle 1)


def link_github_commit_authors(
    identity_link: pa.Table,
    associations: Iterable[tuple[str, str, str]],
    *,
    now: datetime,
) -> pa.Table:
    """Append automated, high-confidence `identity_link` rows asserting that
    a `git_email` identity is also a given GitHub `login`, from
    `(email, login, sha)` triples (`collectors/github_commit_authors.py`'s
    `github_commit_author` raw table: GitHub's own GraphQL commit-history
    `author.user.login` field).

    This is a platform-asserted **fact** (GitHub itself says this commit's
    author email belongs to this account), not a heuristic guess -- exactly
    the case ARCHITECTURE.md §3 carves out: "Links at confidence = exact or
    high may be created automatically by the pipeline." It is deliberately
    distinct from `identity_overrides.yaml`'s manual merges: `linked_by` is
    a heuristic name+version (`'github_commit_author_v1'`), not
    `'manual:<reviewer>'`, since no human reviewed this specific link (the
    review already happened once, when GitHub built the account-email
    association its API now reports).

    One row is added per distinct `(email, login)` pair, keyed to the
    `identity_id` that email's own naive resolution already produced (so
    `github_login`-typed lookups against that identity_id -- e.g.
    `normalize/affiliation.py`'s per-identity grouping -- see this login
    without the git_email and github_login identities being two separate
    people). Where the same pair was observed on more than one commit, the
    lexicographically smallest `sha` is kept as the evidence trail (a
    deterministic, order-independent choice, not "whichever commit iteration
    order happened to see first").

    Idempotent and deterministic (`link_id` is a pure function of the pair):
    calling this again over the same accumulated `associations` -- e.g. next
    run, after more commits/pages have been collected -- reproduces the same
    rows for pairs already seen, plus any new ones, matching this project's
    "recompute fresh every run" convention for derived identity data
    (`resolve_identities` itself is never called incrementally either, D3).
    """
    best_sha: dict[tuple[str, str], str] = {}
    for email, login, sha in associations:
        if not email or not login or not sha:
            continue
        key = (email.strip().lower(), login.strip())
        if key not in best_sha or sha < best_sha[key]:
            best_sha[key] = sha

    if not best_sha:
        return identity_link

    rows = []
    for (email, login), sha in sorted(best_sha.items()):
        identity_id = identity_id_for(GIT_EMAIL, email)
        rows.append(
            {
                "link_id": str(
                    uuid.uuid5(IDENTITY_NAMESPACE, f"github_commit_author:{email}:{login}")
                ),
                "identity_id": identity_id,
                "source_type": "github_login",
                "source_value": login,
                "confidence": "high",
                "evidence": f"GitHub commit author association, sha {sha}",
                "linked_by": "github_commit_author_v1",
                "linked_at": now,
            }
        )
    extra = pa.Table.from_pylist(rows, schema=identity_link.schema)
    combined = pa.concat_tables([identity_link, extra])
    return validate("identity_link", combined)


# --- Metrics-facing resolver -------------------------------------------------


def build_resolver(identity_link: pa.Table) -> Callable[[str, str], str | None]:
    """Build a `(source_type, source_value) -> identity_id | None` function
    from a materialized `identity_link` table.

    Per `schema/README.md`, metrics resolve identities by joining a fact
    table's raw identifier column(s) against `identity_link.source_value`
    (matched on `source_type`) — this is that join, wrapped as a plain Python
    function so metric code doesn't need to hand-roll it. When a raw
    identifier has more than one link row (its naive-resolution link, plus a
    later `identity_overrides.yaml` merge), the manual link wins — that's the
    whole point of a manual override.
    """
    mapping: dict[tuple[str, str], tuple[str, bool]] = {}
    source_types = identity_link.column("source_type").to_pylist()
    source_values = identity_link.column("source_value").to_pylist()
    identity_ids = identity_link.column("identity_id").to_pylist()
    linked_bys = identity_link.column("linked_by").to_pylist()

    for s_type, s_value, identity_id, linked_by in zip(
        source_types, source_values, identity_ids, linked_bys, strict=True
    ):
        is_manual = bool(linked_by) and linked_by.startswith("manual:")
        key = (s_type, s_value)
        existing = mapping.get(key)
        if existing is None or (is_manual and not existing[1]):
            mapping[key] = (identity_id, is_manual)

    def resolve(source_type: str, source_value: str) -> str | None:
        found = mapping.get((source_type, source_value))
        return found[0] if found else None

    return resolve


# --- Extraction from M0 fact tables -----------------------------------------


def extract_raw_identifiers(
    *,
    contribution_events: pa.Table | None = None,
    review_events: pa.Table | None = None,
    issues: pa.Table | None = None,
) -> list[RawIdentifier]:
    """Pull every raw identifier out of the M0 fact tables collectors wrote.

    Collectors (#4, #5) always populate the raw identifier columns and leave
    `*_identity_id` null (schema/README.md); this walks those columns so
    their output can be fed straight into `resolve_identities`.
    """
    identifiers: list[RawIdentifier] = []

    if contribution_events is not None:
        types = contribution_events.column("author_raw_type").to_pylist()
        values = contribution_events.column("author_raw_value").to_pylist()
        names = contribution_events.column("author_display_name").to_pylist()
        for source_type, source_value, display_name in zip(types, values, names, strict=True):
            identifiers.append(RawIdentifier(source_type, source_value, display_name))

    if review_events is not None:
        reviewer_types = review_events.column("reviewer_raw_type").to_pylist()
        reviewer_values = review_events.column("reviewer_raw_value").to_pylist()
        for source_type, source_value in zip(reviewer_types, reviewer_values, strict=True):
            identifiers.append(RawIdentifier(source_type, source_value))

        author_types = review_events.column("author_raw_type").to_pylist()
        author_values = review_events.column("author_raw_value").to_pylist()
        for source_type, source_value in zip(author_types, author_values, strict=True):
            if source_type is not None and source_value is not None:
                identifiers.append(RawIdentifier(source_type, source_value))

    if issues is not None:
        for column in ("reporter_raw", "assignee_raw"):
            for source_value in issues.column(column).to_pylist():
                if source_value is not None:
                    identifiers.append(RawIdentifier(JIRA_USERNAME, source_value))

    return identifiers


# --- identity_overrides.yaml -------------------------------------------------

_OVERRIDE_REQUIRED_FIELDS = frozenset(
    {"source_type", "source_value", "into_source_type", "into_source_value", "reviewer", "evidence"}
)


def load_overrides(path: str | Path) -> list[ManualOverride]:
    """Load manual merge entries from an `identity_overrides.yaml`-shaped file.

    The file's top-level shape is a plain YAML list (see
    `identity_overrides.yaml` at the repo root) — `[]` when no merges have
    been reviewed yet. Raises `FileNotFoundError` if `path` doesn't exist,
    and `IdentityResolutionError` if an entry is malformed.
    """
    resolved_path = Path(path)
    if not resolved_path.is_file():
        raise FileNotFoundError(f"identity overrides file not found: {resolved_path}")

    raw = yaml.safe_load(resolved_path.read_text())
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise IdentityResolutionError(
            f"{resolved_path}: expected a top-level YAML list of override entries, "
            f"got {type(raw).__name__}"
        )

    overrides: list[ManualOverride] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise IdentityResolutionError(f"{resolved_path}: entry {index} is not a mapping")
        missing = _OVERRIDE_REQUIRED_FIELDS - entry.keys()
        if missing:
            raise IdentityResolutionError(
                f"{resolved_path}: entry {index} missing field(s) {sorted(missing)}"
            )
        overrides.append(
            ManualOverride(
                source_type=entry["source_type"],
                source_value=entry["source_value"],
                into_source_type=entry["into_source_type"],
                into_source_value=entry["into_source_value"],
                reviewer=entry["reviewer"],
                evidence=entry["evidence"],
            )
        )
    return overrides
