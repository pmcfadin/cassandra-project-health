"""OpenSSF Scorecard + CVE/advisory collector (issue #55, D21 item 3).

Collects two independent, anonymous, unauthenticated feeds for the
Governance page's Security section:

- **OpenSSF Scorecard** (`api.securityscorecards.dev`): per-check security
  posture results for `github.com/apache/cassandra`. Verified live
  2026-09-25: `score=4.6`, 14 checks returned, `Code-Review` scoring 0
  ("Found 0/30 approved changesets") -- direct confirmation of
  `docs/spec/RESEARCH.md` §6.2's point that Scorecard's `Code-Review` check
  only counts GitHub-native PR approvals and is blind to Cassandra's actual
  commit-trailer/JIRA review process. Every run's per-check rows are
  written as their own partition (`storage.write_partition`'s append-only
  guarantee) -- this is deliberately how "history from now on" (the issue's
  own wording) is implemented: no separate history table, just the same
  raw-partition accumulation every other source already uses.

- **NVD** (`services.nvd.nist.gov/rest/json/cves/2.0`), queried by CPE
  (`virtualMatchString=cpe:2.3:a:apache:cassandra:*...`) rather than a
  free-text keyword search: verified live 2026-09-25 to return 16
  authoritative CVEs (2015-2026) against Cassandra's own CPE dictionary
  entry, versus 25 results (9 of them unrelated false positives from other
  projects' text mentioning "cassandra") for a keyword search on the same
  date. Metadata only (CVE id, published/modified dates, CVSS, affected/
  fixed version ranges, advisory URL) -- no exploit or PoC content is
  collected or stored.

**Sources tried and not used for automated collection** (documented here
per the issue's "verify sources live, record which work" instruction):
- `cassandra.apache.org/_/cve.html`, `.../security.html`: both 404 --
  Cassandra has no dedicated, machine-readable security-advisory page of
  its own; ASF security disclosures for Cassandra flow through CVE
  assignment (`cve.org`/NVD) and dev@/announce@ threads instead.
- `cve.org`'s own search API (`cveawg.mitre.org/api/cve`) requires a
  `CVE-API-ORG` auth header for bulk search (verified live: `400
  BAD_REQUEST` without it) -- NVD's public, unauthenticated CPE search
  covers the same CVE population without needing ASF-internal credentials.
  A single CVE's own `cveawg.mitre.org/api/cve/<id>` record fetch *does*
  work anonymously (verified live), but is redundant with what NVD already
  returns for the same id.
- `lists.apache.org` announce@ archive (`list.html?announce@apache.org`):
  200, live, but plain HTML with no structured per-advisory API -- reliably
  extracting per-CVE structured fields from it would need bespoke, fragile
  scraping; deferred rather than attempted this round.

Both collectors are unauthenticated (no secrets needed, `DATA-SOURCES.md`
§13 pattern) and each collect() call issues exactly two outbound requests
total (one per feed) -- "budgeted and polite" per the issue.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
import pyarrow as pa

from project_health.config import ProjectConfig
from project_health.schema import get_schema, validate

# --- Tuning constants (same retry/backoff pattern as asf_roster.py) --------

DEFAULT_MAX_RETRIES = 3
DEFAULT_TIMEOUT = 30.0
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 10.0

# A short, polite pause between the two feeds' requests -- they're two
# different services, so there's no shared rate limit to respect, but a
# collector making back-to-back external calls with no gap at all is the
# kind of thing that looks like a burst to a passive monitor.
_INTER_REQUEST_PAUSE_SECONDS = 0.5

DEFAULT_SCORECARD_URL = "https://api.securityscorecards.dev/projects/github.com/apache/cassandra"
DEFAULT_NVD_CVE_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
DEFAULT_CPE_MATCH_STRING = "cpe:2.3:a:apache:cassandra:*:*:*:*:*:*:*:*"
DEFAULT_NVD_RESULTS_PER_PAGE = 200

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


class CollectionError(Exception):
    """Raised when a security-feed request fails after exhausting retries."""


@dataclass(frozen=True)
class SecurityCollectionResult:
    """Output of one `SecurityCollector.collect()` run."""

    scorecard_checks: pa.Table
    advisories: pa.Table
    scorecard_check_count: int
    advisory_count: int


# --- Date helpers -------------------------------------------------------


def _parse_date_only(value: str | None) -> int | None:
    """Parse a leading `YYYY-MM-DD` (from either a bare date or an ISO
    datetime string) to `pa.date32()`'s day-count-since-epoch integer.

    Returns `None` for `None`/empty/unparseable input -- a missing or
    malformed date is `unknown`, never a crash or an invented value.
    """
    if not value:
        return None
    match = _ISO_DATE_RE.match(value)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        from datetime import date as _date

        epoch = _date(1970, 1, 1)
        return (_date(year, month, day) - epoch).days
    except ValueError:
        return None


# --- Retry logic (same pattern as jira.py / asf_roster.py) -----------------


def _exponential_backoff(attempt: int) -> float:
    import random

    exp = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (attempt - 1)))
    return exp + random.uniform(0, exp * 0.25)


def _rows_to_table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    if not rows:
        return schema.empty_table()
    return pa.Table.from_pylist(rows, schema=schema)


# --- Scorecard normalization -------------------------------------------


def _normalize_scorecard_checks(payload: dict[str, Any], snapshot_id: str) -> list[dict]:
    """One row per `checks[]` entry in the Scorecard API response."""
    scorecard_date = _parse_date_only(payload.get("date"))
    overall_score = payload.get("score")
    scorecard_version = (payload.get("scorecard") or {}).get("version")
    repo = (payload.get("repo") or {}).get("name")
    collected_at = datetime.now(timezone.utc)

    rows = []
    for check in payload.get("checks", []):
        details = check.get("details") or []
        details_summary = "; ".join(str(d) for d in details)[:2000] if details else None
        rows.append(
            {
                "check_id": str(uuid.uuid4()),
                "repo": repo,
                "scorecard_date": scorecard_date,
                "scorecard_version": scorecard_version,
                "overall_score": overall_score,
                "check_name": check.get("name"),
                "check_score": check.get("score"),
                "check_reason": check.get("reason"),
                "check_details_summary": details_summary,
                "source_snapshot_id": snapshot_id,
                "collected_at": collected_at,
            }
        )
    return rows


# --- NVD advisory normalization -----------------------------------------


def _version_sort_key(version: str) -> tuple:
    """Sort key that compares dotted-numeric version segments numerically
    (e.g. "1.2.9" < "1.2.19"), falling back to plain string comparison for
    any non-numeric segment -- good enough for summarizing an *enumerated*
    CPE version list, not a general semver parser."""
    parts = re.split(r"[.\-]", version)
    key: list[tuple[int, Any]] = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return tuple(key)


def _english_description(descriptions: list[dict]) -> str | None:
    for entry in descriptions:
        if entry.get("lang") == "en":
            value = entry.get("value", "")
            return value[:1000]
    return None


def _extract_cvss(metrics: dict[str, Any]) -> tuple[float | None, str | None, str | None]:
    """Prefer the newest CVSS version NVD provides. Returns
    (score, version, severity)."""
    for key, version in (
        ("cvssMetricV31", "3.1"),
        ("cvssMetricV30", "3.0"),
        ("cvssMetricV2", "2.0"),
    ):
        entries = metrics.get(key)
        if entries:
            entry = entries[0]
            cvss_data = entry.get("cvssData", {})
            score = cvss_data.get("baseScore")
            severity = entry.get("baseSeverity") or cvss_data.get("baseSeverity")
            return score, version, severity
    return None, None, None


def _summarize_versions(configurations: list[dict]) -> tuple[str | None, str | None]:
    """Build a human-readable `(affected_versions, fixed_versions)` pair
    from NVD's `configurations[].nodes[].cpeMatch[]` entries, restricted to
    `apache:cassandra` CPE lines (a CVE's configuration can name other
    products too, e.g. a bundled JRE -- CVE-2016-3427).

    NVD uses two different shapes for "affected": a start/end version
    range on a wildcard CPE (`versionStartIncluding`/`versionEndExcluding`),
    or a list of individually enumerated exact-version CPEs with no range
    fields at all (older CVEs, e.g. CVE-2015-0225). Both are summarized;
    `fixed_versions` is only ever populated from an explicit
    `versionEndExcluding` (the version NVD states the fix landed in) --
    never guessed from an enumerated list, since NVD doesn't state a fix
    version there.
    """
    ranges: list[str] = []
    fixed: set[str] = set()
    enumerated: list[str] = []

    for config in configurations:
        for node in config.get("nodes", []):
            for match in node.get("cpeMatch", []):
                criteria = match.get("criteria", "")
                if ":apache:cassandra:" not in criteria:
                    continue
                start = match.get("versionStartIncluding") or match.get("versionStartExcluding")
                end = match.get("versionEndExcluding") or match.get("versionEndIncluding")
                if start or end:
                    ranges.append(f"{start or '0'}–{end or 'latest'}")
                    if match.get("versionEndExcluding"):
                        fixed.add(match["versionEndExcluding"])
                else:
                    parts = criteria.split(":")
                    if len(parts) > 5 and parts[5] not in ("*", "-"):
                        enumerated.append(parts[5])

    affected_parts = list(dict.fromkeys(ranges))  # de-dupe, keep order
    if enumerated:
        distinct = sorted(set(enumerated), key=_version_sort_key)
        affected_parts.append(f"{distinct[0]}–{distinct[-1]} ({len(distinct)} versions)")

    affected = "; ".join(affected_parts) if affected_parts else None
    fixed_versions = ", ".join(sorted(fixed, key=_version_sort_key)) if fixed else None
    return affected, fixed_versions


def _normalize_advisories(payload: dict[str, Any], snapshot_id: str) -> list[dict]:
    collected_at = datetime.now(timezone.utc)
    rows = []
    for item in payload.get("vulnerabilities", []):
        cve = item.get("cve", {})
        cve_id = cve.get("id")
        if not cve_id:
            continue
        cvss_score, cvss_version, severity = _extract_cvss(cve.get("metrics", {}))
        affected, fixed = _summarize_versions(cve.get("configurations", []))
        rows.append(
            {
                "advisory_id": str(uuid.uuid4()),
                "cve_id": cve_id,
                "published_date": _parse_date_only(cve.get("published")),
                "last_modified_date": _parse_date_only(cve.get("lastModified")),
                "severity": severity,
                "cvss_score": cvss_score,
                "cvss_version": cvss_version,
                "summary": _english_description(cve.get("descriptions", [])),
                "affected_versions": affected,
                "fixed_versions": fixed,
                "advisory_url": f"https://nvd.nist.gov/vuln/detail/{cve_id}",
                "source": "nvd",
                "source_snapshot_id": snapshot_id,
                "collected_at": collected_at,
            }
        )
    return rows


# --- Collector ------------------------------------------------------------


class SecurityCollector:
    """OpenSSF Scorecard + NVD advisory collector (issue #55, D21 item 3).

    Anonymous, unauthenticated access to both feeds; no secrets required.
    """

    source_id = "security"

    def __init__(
        self,
        config: ProjectConfig,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep_fn: Callable[[float], None] = time.sleep,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        security_config = getattr(config, "security", None)
        self._scorecard_url = (
            getattr(security_config, "scorecard_url", None) or DEFAULT_SCORECARD_URL
        )
        self._nvd_url = getattr(security_config, "nvd_cve_url", None) or DEFAULT_NVD_CVE_URL
        self._cpe_match_string = (
            getattr(security_config, "cpe_match_string", None) or DEFAULT_CPE_MATCH_STRING
        )
        self._max_retries = max_retries
        self._sleep_fn = sleep_fn
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SecurityCollector":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get_with_retry(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.get(url, params=params)
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"request to {url} timed out after {attempt} attempt(s): {exc}"
                    ) from exc
                self._sleep_fn(_exponential_backoff(attempt))
                continue
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"request to {url} failed after {attempt} attempt(s): {exc}"
                    ) from exc
                self._sleep_fn(_exponential_backoff(attempt))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise CollectionError(
                        f"request to {url} failed after {attempt} attempt(s): "
                        f"HTTP {response.status_code}"
                    )
                self._sleep_fn(_exponential_backoff(attempt))
                continue

            response.raise_for_status()
            return response

    def fetch_scorecard(self, snapshot_id: str) -> list[dict]:
        response = self._get_with_retry(self._scorecard_url)
        return _normalize_scorecard_checks(response.json(), snapshot_id)

    def fetch_advisories(self, snapshot_id: str) -> list[dict]:
        params = {
            "virtualMatchString": self._cpe_match_string,
            "resultsPerPage": DEFAULT_NVD_RESULTS_PER_PAGE,
        }
        response = self._get_with_retry(self._nvd_url, params=params)
        return _normalize_advisories(response.json(), snapshot_id)

    def collect(self) -> SecurityCollectionResult:
        """Fetch both feeds and return their validated tables.

        A failure in either feed raises `CollectionError` (via
        `_get_with_retry`), and the pipeline (`_collect_security`) treats
        the whole `security` source as failed for this run -- same
        granularity as `AsfRosterCollector`'s two-call `fetch_roster`.
        """
        snapshot_id = str(uuid.uuid4())
        scorecard_rows = self.fetch_scorecard(snapshot_id)
        self._sleep_fn(_INTER_REQUEST_PAUSE_SECONDS)
        advisory_rows = self.fetch_advisories(snapshot_id)

        scorecard_table = validate(
            "scorecard_check", _rows_to_table(scorecard_rows, get_schema("scorecard_check"))
        )
        advisory_table = validate(
            "security_advisory", _rows_to_table(advisory_rows, get_schema("security_advisory"))
        )
        return SecurityCollectionResult(
            scorecard_checks=scorecard_table,
            advisories=advisory_table,
            scorecard_check_count=len(scorecard_rows),
            advisory_count=len(advisory_rows),
        )
