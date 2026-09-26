"""Live-test helper for `ci-cassandra.apache.org`'s Jenkins JSON API (issue #36).

`pre-commit-ci-evidence.v2_upgrade_condition` (governance-policy.yaml) asks
issue #36 to live-test the `ci-cassandra.apache.org` post-commit Jenkins JSON
API and report whether it's reliable enough to justify a v2 upgrade
(`fail_allowed: true` and/or a new evidence source) — **this module reports
findings only; it never changes the shipped v1 policy or scores any check**.
`checks.score_pre_commit_ci_evidence` never calls this module.

Three questions, matching the issue's ask:

1. **Availability** — is the JSON API live and responsive?
2. **History depth** — how far back does a job's build list actually reach?
3. **SHA joinability** — does a build record the exact commit SHA it built,
   in a form that can be matched against this project's own git history?

`probe_job` answers all three for one Jenkins job (e.g. `Cassandra-trunk`).
See this issue's task report for the actual live findings (run
2026-09-25 against `https://ci-cassandra.apache.org`) — this module is the
reusable probe, not a one-off script; running it again later will get a
different (job list changes, retention rolls forward) but comparably-shaped
result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

DEFAULT_BASE_URL = "https://ci-cassandra.apache.org"
DEFAULT_TIMEOUT = 30.0

# `hudson.plugins.git.util.BuildData` is the Jenkins Git plugin's per-build
# action recording the SHA it actually built, per configured remote —
# apache/cassandra builds carry (at least) two: one for
# `https://github.com/apache/cassandra` (the SHA that matters here) and one
# for `https://github.com/apache/cassandra-dtest`.
_GIT_BUILD_DATA_CLASS = "hudson.plugins.git.util.BuildData"
_CASSANDRA_REMOTE = "https://github.com/apache/cassandra"


@dataclass(frozen=True)
class BuildProbe:
    number: int
    result: str | None
    timestamp: datetime | None
    url: str
    cassandra_sha: str | None  # None if no BuildData action names the apache/cassandra remote


@dataclass(frozen=True)
class JobProbeResult:
    job_name: str
    job_url: str
    reachable: bool
    first_build_number: int | None
    last_build_number: int | None
    retained_build_count: int
    oldest_retained_at: datetime | None
    newest_retained_at: datetime | None
    builds_with_joinable_sha: int
    sample_builds: tuple[BuildProbe, ...] = field(default_factory=tuple)
    error: str | None = None


def _build_data_sha(actions: list[dict], remote: str) -> str | None:
    for action in actions:
        if action.get("_class") != _GIT_BUILD_DATA_CLASS:
            continue
        remote_urls = action.get("remoteUrls") or []
        if any(remote in url for url in remote_urls):
            revision = action.get("lastBuiltRevision") or {}
            sha = revision.get("SHA1")
            if sha:
                return sha
    return None


def probe_job(
    job_name: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    client: httpx.Client | None = None,
    sample_size: int = 5,
    timeout: float = DEFAULT_TIMEOUT,
) -> JobProbeResult:
    """Live-probe one Jenkins job's availability, history depth, and
    SHA-joinability.

    `sample_size` builds (most recent first) are fetched in full to check
    for a `BuildData` action naming the `apache/cassandra` remote.
    """
    owns_client = client is None
    client = client or httpx.Client(base_url=base_url, timeout=timeout)
    job_url = f"{base_url.rstrip('/')}/job/{job_name}/"
    try:
        response = client.get(
            f"/job/{job_name}/api/json",
            params={"tree": "firstBuild[number],lastBuild[number],builds[number,timestamp,url]"},
        )
        if response.status_code != 200:
            return JobProbeResult(
                job_name=job_name,
                job_url=job_url,
                reachable=False,
                first_build_number=None,
                last_build_number=None,
                retained_build_count=0,
                oldest_retained_at=None,
                newest_retained_at=None,
                builds_with_joinable_sha=0,
                error=f"HTTP {response.status_code}",
            )
        payload = response.json()
        builds = payload.get("builds", [])
        first_build = (payload.get("firstBuild") or {}).get("number")
        last_build = (payload.get("lastBuild") or {}).get("number")

        def _ts(build: dict) -> datetime | None:
            raw = build.get("timestamp")
            if raw is None:
                return None
            return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)

        oldest_at = _ts(builds[-1]) if builds else None
        newest_at = _ts(builds[0]) if builds else None

        samples: list[BuildProbe] = []
        joinable_count = 0
        for build in builds[:sample_size]:
            detail_tree = (
                "number,result,timestamp,url,"
                "actions[_class,lastBuiltRevision[SHA1],remoteUrls]"
            )
            detail = client.get(
                f"/job/{job_name}/{build['number']}/api/json",
                params={"tree": detail_tree},
            )
            if detail.status_code != 200:
                continue
            detail_payload = detail.json()
            sha = _build_data_sha(detail_payload.get("actions", []), _CASSANDRA_REMOTE)
            if sha:
                joinable_count += 1
            samples.append(
                BuildProbe(
                    number=detail_payload["number"],
                    result=detail_payload.get("result"),
                    timestamp=_ts(detail_payload),
                    url=detail_payload.get("url", ""),
                    cassandra_sha=sha,
                )
            )

        return JobProbeResult(
            job_name=job_name,
            job_url=job_url,
            reachable=True,
            first_build_number=first_build,
            last_build_number=last_build,
            retained_build_count=len(builds),
            oldest_retained_at=oldest_at,
            newest_retained_at=newest_at,
            builds_with_joinable_sha=joinable_count,
            sample_builds=tuple(samples),
        )
    except httpx.HTTPError as exc:
        return JobProbeResult(
            job_name=job_name,
            job_url=job_url,
            reachable=False,
            first_build_number=None,
            last_build_number=None,
            retained_build_count=0,
            oldest_retained_at=None,
            newest_retained_at=None,
            builds_with_joinable_sha=0,
            error=str(exc),
        )
    finally:
        if owns_client:
            client.close()
