"""`/methodology/` page context builder (issue #160).

Replaces the footer's old "Methodology" link, which pointed straight at
`docs/spec/` on GitHub, with a real, generated, on-site page. Every
section below is short and scannable (reviewer feedback on the pre-#160
site: "too verbose") and every number on it is read from this project's
own code/config at generation time, never typed from memory:

- the pinned classifier model id (`classify.questions.EXPECTED_MODEL`),
- the thread-sampling K and seed (`private_run.sample.DEFAULT_K`/
  `DEFAULT_SEED`),
- the peer-project list (`projects/peers.yaml`, via `peers.config.
  load_peers`),
- the metric catalogue (`metrics_meta.metric_catalogue`, built from the
  same `M0_METRICS`/`GOVERNANCE_METRICS`/`PR_BACKLOG_METRICS` maps the
  rest of the site renders from -- never a second, hand-maintained list,
  so #158's user@ metrics and #159's contributor-pipeline metrics appear
  here automatically once they're registered there).

This page publishes no scores or verdicts (D25, D29) -- it describes what
is measured and how, not whether any number is good or bad.
"""

from __future__ import annotations

import re
from pathlib import Path

from project_health.classify.questions import EXPECTED_MODEL
from project_health.peers.config import load_peers
from project_health.private_run.sample import DEFAULT_K, DEFAULT_SEED
from project_health.site.metrics_meta import CatalogueEntry, metric_catalogue

REPO_URL = "https://github.com/pmcfadin/cassandra-project-health"
DECISIONS_URL = f"{REPO_URL}/blob/main/docs/spec/DECISIONS.md"
METRICS_SPEC_URL = f"{REPO_URL}/blob/main/docs/spec/METRICS.md"
COMMUNITY_HEALTH_SPEC_URL = f"{REPO_URL}/blob/main/docs/spec/COMMUNITY-HEALTH.md"
DATA_SOURCES_SPEC_URL = f"{REPO_URL}/blob/main/docs/spec/DATA-SOURCES.md"
ARCHITECTURE_SPEC_URL = f"{REPO_URL}/blob/main/docs/spec/ARCHITECTURE.md"
BENCHMARK_PUBLIC_URL = f"{REPO_URL}/blob/main/docs/benchmark/public-v1.md"
DATA_BRANCH_URL = f"{REPO_URL}/tree/data"
IDENTITY_OVERRIDES_URL = f"{REPO_URL}/blob/main/identity_overrides.yaml"
PEERS_CONFIG_URL = f"{REPO_URL}/blob/main/projects/peers.yaml"

# `src/project_health/site/methodology_page.py` -> repo root is 3 parents up
# (site -> project_health -> src -> root), the same pattern `governance.
# overrides.DEFAULT_OVERRIDES_PATH` already uses for a repo-root config file.
DEFAULT_PEERS_PATH = Path(__file__).resolve().parents[3] / "projects" / "peers.yaml"


def _github_slug(heading: str) -> str:
    """Reproduce GitHub's Markdown heading-anchor algorithm (lowercase,
    strip everything but word characters/hyphen/space, spaces -> hyphens).

    Validated against every `DECISIONS.md` anchor this codebase already
    hand-verified live and hard-coded elsewhere (`site/generate.py`'s
    `DECISIONS_D14_ANCHOR`/`DECISIONS_D15_ANCHOR`/`DECISIONS_D19_ANCHOR`,
    `leaderboard_page.py`'s `DECISIONS_D26_ANCHOR`, `thread_explorer_page.
    py`'s `DECISIONS_D27_ANCHOR`) before being reused here to derive the
    several more D-numbered anchors this page cites (D1, D18, D22-D30) --
    see `tests/test_site_sections.py`'s methodology tests for the
    cross-check against those known-good constants.
    """
    slug = heading.lower()
    slug = re.sub(r"[^\w\- ]", "", slug)
    return slug.replace(" ", "-")


# Headings transcribed verbatim from `docs/spec/DECISIONS.md` (never
# paraphrased -- a wrong transcription would silently produce a wrong, but
# plausible-looking, anchor).
_DECISIONS_HEADINGS: dict[str, str] = {
    "d1": "D1. Scope is phased",
    "d18": "D18. Phase 2a Jev pilot",
    "d22": "D22. No LLM at runtime",
    "d23": "D23. Public labeled datasets are the primary classifier benchmark (amends D18)",
    "d24": "D24. Governance checks encode only the project's own published rules",
    "d25": (
        "D25. The site is informational; it publishes no policy and no verdicts "
        "(amends D14, D15, D24)"
    ),
    "d26": "D26. Publish Conversation patterns page (preliminary, classified aggregates)",
    "d27": "D27. Public thread-level conversation explorer",
    "d28": (
        "D28. Owner-only local per-account review (amends COMMUNITY-HEALTH.md "
        "§7.1, §7.3 for the owner's private use only)"
    ),
    "d29": (
        "D29. Remove composite score; landing page leads with CHAOSS Starter "
        "Project Health model (metrics only)"
    ),
    "d30": "D30. Peer context is facts; no targets; peers chosen by owner (issue #145)",
}

DECISIONS_ANCHORS: dict[str, str] = {
    key: f"{DECISIONS_URL}#{_github_slug(heading)}"
    for key, heading in _DECISIONS_HEADINGS.items()
}


# --- Data sources table (issue #160 item 2) ---------------------------------
#
# What's collected / what's explicitly NOT collected, verified against the
# actual collector code (`src/project_health/collectors/*.py`) rather than
# the earlier, aspirational `docs/spec/DATA-SOURCES.md` research note --
# e.g. `collectors/git.py::_resolve_ref` walks only the configured
# `default_branch` (trunk), never release branches, and `collectors/
# github.py` never reads a PR's base-branch field at all (grep-verified,
# issue #160). Cadence is transcribed from `.github/workflows/nightly.yml`
# and `.github/workflows/peers.yml`'s own `cron:` lines, not memorized.
DATA_SOURCES: tuple[dict[str, str], ...] = (
    {
        "name": "Git (apache/cassandra, trunk)",
        "collected": (
            "Commit SHA, author/committer name and email, timestamp, message "
            "(including reviewer trailers), per-file line stats."
        ),
        "not_collected": (
            "Release branches (only the configured default branch, trunk, is "
            "walked) and any other apache/cassandra-* repo."
        ),
        "cadence": "Nightly (06:17 UTC)",
    },
    {
        "name": "GitHub (apache/cassandra PRs, reviews, comments, check runs)",
        "collected": "PR metadata, review events, review/issue comments, check runs.",
        "not_collected": (
            "A PR's base branch; GitHub Issues/Discussions (disabled on this "
            "repo -- JIRA is the issue tracker)."
        ),
        "cadence": "Nightly (06:17 UTC)",
    },
    {
        "name": "ASF JIRA (CASSANDRA project)",
        "collected": (
            "Issue metadata, status-change history, the Reviewer/Reviewers custom "
            "fields, comment metadata."
        ),
        "not_collected": (
            "Comment bodies are fetched transiently for classification (Phase 2a) "
            "and never committed to the data branch."
        ),
        "cadence": "Nightly (06:17 UTC)",
    },
    {
        "name": "Pony Mail (dev@/user@cassandra.apache.org)",
        "collected": (
            "Sender, timestamp, thread structure (Message-ID/In-Reply-To) -- "
            "metadata only in Phase 1."
        ),
        "not_collected": (
            "Message bodies -- fetched transiently for classification (Phase 2a) "
            "and never committed to the data branch or the public repo."
        ),
        "cadence": "Nightly (06:17 UTC)",
    },
    {
        "name": "ASF roster (Whimsy committee-info.json, public_ldap_people.json)",
        "collected": "PMC/committer ASF ids, display names, PMC join dates.",
        "not_collected": "A clean public committer-only (non-PMC) roster with join dates.",
        "cadence": "Nightly (06:17 UTC)",
    },
    {
        "name": "Releases (git tags; archive.apache.org as an existence cross-check)",
        "collected": (
            "GA release tags and dates (the git tag's own creatordate is "
            "authoritative); archive.apache.org directory existence as a "
            "cross-check, never for dating."
        ),
        "not_collected": "GitHub Releases (apache/cassandra publishes none).",
        "cadence": "Nightly (06:17 UTC)",
    },
    {
        "name": "Releases tab (ASF JIRA versions; Jenkins; Butler CI trends)",
        "collected": (
            "JIRA fixVersions (blocker-ladder placeholder descriptions, quoted "
            "verbatim, and their open issues) for each active release line; "
            "each line's Jenkins job (ci-cassandra.apache.org) last-build color/"
            "result; Butler's (butler.cassandra.apache.org) per-build test-"
            "failure/run/duration trend history."
        ),
        "not_collected": (
            "Butler's per-test failure detail (its own per-suite endpoint times "
            "out); Butler history before roughly June 2026."
        ),
        "cadence": "Nightly (06:17 UTC)",
    },
    {
        "name": "Security (OpenSSF Scorecard, published advisories)",
        "collected": "Per-check Scorecard scores and reasons; published CVE/advisory records.",
        "not_collected": "",
        "cadence": "Nightly (06:17 UTC)",
    },
    {
        "name": "Peer projects (apache/kafka, spark, flink, pulsar, datafusion)",
        "collected": (
            "Git commits (bounded lookback), GitHub PRs/reviews, GA releases -- the "
            "same code this project uses for Cassandra's own collection, for the "
            "metrics shown on /peers/."
        ),
        "not_collected": (
            "Issue-tracker metrics (JIRA and GitHub Issues aren't comparable across "
            "these six projects without more normalization work than this covers)."
        ),
        "cadence": "Weekly, Sundays (20:00 UTC)",
    },
    {
        "name": "ASF Slack",
        "collected": "Nothing.",
        "not_collected": (
            "Not collected at all -- gated behind PMC consensus on dev@ and ASF "
            "Infra approval of a read-only bot on public channels (Phase 2b)."
        ),
        "cadence": "Not running",
    },
)


def _load_peer_names() -> tuple[str, ...]:
    try:
        config = load_peers(DEFAULT_PEERS_PATH)
    except FileNotFoundError:
        return ()
    return tuple(peer.display_name for peer in config.peers)


def build_methodology_context() -> dict:
    """Everything `templates/methodology.html` needs. Pure config/code
    facts -- no `data_dir`/snapshot read, so this page always renders the
    same way regardless of whether a run has happened yet."""
    return {
        # `decisions_url` and `community_health_spec_url` are already part of
        # every page's shared context (`generate.py::_common_page_context`,
        # same literal values) -- not repeated here to avoid a duplicate-
        # keyword-argument clash when this context is splatted into the
        # same `render()` call alongside it.
        "metrics_spec_url": METRICS_SPEC_URL,
        "data_sources_spec_url": DATA_SOURCES_SPEC_URL,
        "architecture_spec_url": ARCHITECTURE_SPEC_URL,
        "benchmark_public_url": BENCHMARK_PUBLIC_URL,
        "data_branch_url": DATA_BRANCH_URL,
        "identity_overrides_url": IDENTITY_OVERRIDES_URL,
        "peers_config_url": PEERS_CONFIG_URL,
        "d": DECISIONS_ANCHORS,
        "data_sources": DATA_SOURCES,
        "catalogue": metric_catalogue(),
        "model_id": EXPECTED_MODEL,
        "sample_k": DEFAULT_K,
        "sample_seed": DEFAULT_SEED,
        "peer_names": _load_peer_names(),
    }


__all__ = ["CatalogueEntry", "COMMUNITY_HEALTH_SPEC_URL", "build_methodology_context"]
