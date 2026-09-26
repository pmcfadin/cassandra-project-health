"""ASF Whimsy roster collector (ARCHITECTURE.md §2.2 `RosterAdapter`).

Collects Cassandra committer and PMC membership from Whimsy public JSON endpoints:
- ``committee-info.json``: PMC roster with join dates (ground truth, 49 entries with dates)
- ``public_ldap_projects.json``: Full Cassandra project roster (101 members, 49 owners/PMC)

Emits one normalized table (ARCHITECTURE.md §3, `schema/tables.py`):

- ``roster_entry`` — one row per committer/PMC member, with ``asf_id`` (raw),
  ``effective_from`` (join date if available, null for committers without dates),
  ``role`` ('pmc' for PMC members from committee-info, 'committer' for others),
  and ``identity_id`` left ``null`` for identity resolution (#6) to fill in later
  via the exact @apache.org email → ASF id mapping or explicit overrides.

Per issue #50 and DATA-SOURCES.md §6:
- Collect BOTH PMC (with join dates from committee-info.json) and committers (from
  public_ldap_projects.json, without join dates).
- Map ASF id to identity ONLY through exact ``<id>@apache.org`` email or explicit
  ``identity_overrides.yaml`` entry (never guess).
- Record join dates where they exist (PMC from committee-info); use null for committers.
- Ground truth: Whimsy committee-info.json and public_ldap_projects.json rosters are
  already resolved identity data; no identity ambiguity for this specific population
  (METRICS.md `pmc_joins_quarterly`).

Per D2.6 (nothing changes silently), raw join date strings are preserved in
``effective_from_raw`` for audit trail.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import httpx
import pyarrow as pa

from project_health.config import ProjectConfig
from project_health.schema import get_schema, validate

# --- Tuning constants (issue #50, DATA-SOURCES.md §6) ----

DEFAULT_MAX_RETRIES = 3
DEFAULT_TIMEOUT = 30.0
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 10.0


class CollectionError(Exception):
    """Raised when a Whimsy request fails after exhausting all retry attempts."""


@dataclass(frozen=True)
class RosterCollectionResult:
    """Output of one `AsfRosterCollector.collect()` run."""

    roster_entries: pa.Table
    entry_count: int


# --- Timestamp / date helpers -------------------------------------------------


def _parse_iso_date(date_string: str) -> int | None:
    """Parse ISO date string (YYYY-MM-DD) to pa.date32() integer.

    Returns None if the string is empty, None, or unparseable.
    """
    if not date_string:
        return None
    try:
        dt = datetime.strptime(date_string, "%Y-%m-%d")
        # Convert to days since Unix epoch (1970-01-01)
        epoch = datetime(1970, 1, 1)
        return (dt.date() - epoch.date()).days
    except (ValueError, AttributeError):
        return None


# --- Retry logic (same pattern as jira.py) ----------------------------------


def _exponential_backoff(attempt: int) -> float:
    """Exponential backoff with jitter, capped at `_BACKOFF_CAP` seconds."""
    import random

    exp = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (attempt - 1)))
    return exp + random.uniform(0, exp * 0.25)


# --- Normalization ----------------------------------------------------------


def _normalize_roster_entry(
    asf_id: str,
    name: str,
    join_date_str: str | None,
    role: str,
    source_snapshot_id: str,
) -> dict:
    """Build one `roster_entry` table row from Whimsy roster data.

    Per issue #50: if join date is not available, effective_from is null.
    Raw date string is preserved for audit trail (D2.6).

    Args:
        asf_id: ASF username (e.g., 'user001')
        name: Display name of the roster member
        join_date_str: ISO date string (YYYY-MM-DD) or None if not available
        role: 'pmc' for PMC members (from committee-info) or 'committer' for others
        source_snapshot_id: Snapshot ID for provenance tracking
    """
    parsed_date = _parse_iso_date(join_date_str) if join_date_str else None

    return {
        "entry_id": str(uuid.uuid4()),
        "identity_id": None,  # Filled in by identity resolution (#6)
        "asf_id": asf_id,
        "display_name": name,
        "role": role,
        "project": "cassandra",
        "effective_from": parsed_date,
        "effective_from_raw": join_date_str,
        "source_snapshot_id": source_snapshot_id,
    }


def _rows_to_table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    if not rows:
        return schema.empty_table()
    return pa.Table.from_pylist(rows, schema=schema)


# --- Collector ----------------------------------------------------------


class AsfRosterCollector:
    """ASF Whimsy roster collector (RosterAdapter per ARCHITECTURE.md §2.2).

    Reads from Whimsy public JSON endpoints (DATA-SOURCES.md §6):
    - ``committee-info.json`` for PMC roster with join dates (49 entries for Cassandra)
    - ``public_ldap_projects.json`` for full project roster with members and owners
      (101 committers for Cassandra, 49 of which are also PMC/owners)

    Emits both PMC (with effective_from dates from committee-info.json) and committers
    (without effective_from dates, role='committer').

    Anonymous, unauthenticated access; no auth secrets needed (DATA-SOURCES.md §13).
    """

    source_id = "asf_roster"

    def __init__(
        self,
        config: ProjectConfig,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep_fn: Callable[[float], None] = time.sleep,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        roster_config = config.roster
        committee_info_url = (
            getattr(roster_config, "committee_info_url", None) if roster_config else None
        )
        public_ldap_projects_url = (
            getattr(roster_config, "public_ldap_projects_url", None) if roster_config else None
        )
        if not committee_info_url:
            raise ValueError(
                "config.roster.committee_info_url is required for AsfRosterCollector"
            )
        if not public_ldap_projects_url:
            raise ValueError(
                "config.roster.public_ldap_projects_url is required for AsfRosterCollector"
            )

        self._committee_info_url = committee_info_url
        self._public_ldap_projects_url = public_ldap_projects_url
        self._max_retries = max_retries
        self._sleep_fn = sleep_fn

        self._client = httpx.Client(transport=transport, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AsfRosterCollector":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get_with_retry(self, url: str) -> httpx.Response:
        """Fetch URL with exponential backoff on transient failures and 429/5xx."""
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.get(url)
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"Whimsy request to {url} timed out after {attempt} attempt(s): {exc}"
                    ) from exc
                self._sleep_fn(_exponential_backoff(attempt))
                continue
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"Whimsy request to {url} failed after {attempt} attempt(s): {exc}"
                    ) from exc
                self._sleep_fn(_exponential_backoff(attempt))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"Whimsy request to {url} failed after {attempt} attempt(s): "
                        f"HTTP {response.status_code}"
                    )
                self._sleep_fn(_exponential_backoff(attempt))
                continue

            response.raise_for_status()
            return response

    def fetch_roster(self) -> tuple[list[dict], str]:
        """Fetch Cassandra PMC roster and full roster from both sources.

        Fetches from committee-info.json (PMC) and public_ldap_projects.json (all members).
        Returns tuple of (list of normalized roster_entry dicts, snapshot_id).
        Includes both PMC members (with join dates) and committers (without dates).
        """
        # Generate snapshot_id once per collect() call, shared across both sources
        snapshot_id = str(uuid.uuid4())

        entries = []

        # 1. Fetch PMC from committee-info.json
        committee_response = self._get_with_retry(self._committee_info_url)
        committee_payload = committee_response.json()
        cassandra_committee = committee_payload.get("committees", {}).get("cassandra", {})
        pmc_roster = cassandra_committee.get("roster", {})

        pmc_ids_seen = set()
        for asf_id, entry_data in pmc_roster.items():
            name = entry_data.get("name", "")
            date_str = entry_data.get("date")  # ISO format: YYYY-MM-DD

            normalized = _normalize_roster_entry(
                asf_id, name, date_str, role="pmc", source_snapshot_id=snapshot_id
            )
            entries.append(normalized)
            pmc_ids_seen.add(asf_id)

        # 2. Fetch full project roster from public_ldap_projects.json
        ldap_response = self._get_with_retry(self._public_ldap_projects_url)
        ldap_payload = ldap_response.json()
        cassandra_project = ldap_payload.get("projects", {}).get("cassandra", {})
        members = cassandra_project.get("members", [])

        # Committers are those members NOT already in the PMC list.
        # Members in public_ldap_projects.json are ID strings, not dicts.
        for member in members:
            asf_id = member if isinstance(member, str) else member.get("id")
            if asf_id and asf_id not in pmc_ids_seen:
                # Committers don't have a join date in public_ldap_projects.json
                normalized = _normalize_roster_entry(
                    asf_id, "", None, role="committer", source_snapshot_id=snapshot_id
                )
                entries.append(normalized)

        return entries, snapshot_id

    def collect(self, snapshot_id: str | None = None) -> RosterCollectionResult:
        """Fetch roster and return validated `roster_entry` table plus metadata.

        Fetches both PMC (with join dates from committee-info.json) and committers
        (from public_ldap_projects.json, without join dates).

        `snapshot_id` parameter is accepted but ignored; fetch_roster() generates
        its own snapshot_id to ensure all rows from both sources share the same
        provenance marker. This keeps the method signature compatible with other
        collectors while maintaining the guarantee that one collect() call produces
        one cohesive snapshot.
        """
        entries, generated_snapshot_id = self.fetch_roster()

        # Validate against schema
        roster_table = validate(
            "roster_entry", _rows_to_table(entries, get_schema("roster_entry"))
        )

        return RosterCollectionResult(
            roster_entries=roster_table,
            entry_count=len(entries),
        )
