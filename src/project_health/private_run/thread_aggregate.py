"""Thread-level and newcomer-treatment aggregation for the private Cassandra
communication run (issue #114; COMMUNITY-HEALTH.md §5.1's floors, §5.2's
formulas).

Consumes `thread_derive.ThreadDerivation` (one per sampled thread, already
computed at a chosen intensity cutoff) and `NewcomerMessage` (one per
classified message whose `directed_at` target was a newcomer at message
time, per §2.3 rule 8) and produces the four thread-level rates plus the two
newcomer response rates from §5.2's table, each weighted by thread inclusion
weight (`runner.py`'s pooling already applies the same weight every
message-level metric in this project uses) with a thread-level nonparametric
bootstrap 95% CI, matching `stats.py`'s own bootstrap convention (resample
whole threads/messages with replacement, never treat within-thread messages
as independent draws for anything already scoped at the thread level).

**§5.2 floors implemented here, verbatim from the table:**

- Escalation rate / constructive resolution rate / thread abandonment rate:
  30 qualifying threads, 5 distinct participants across them.
- Pile-on rate: 30 threads, 5 distinct **targets** (not distinct
  participants) -- the weaponization guard (§7.5 item 7).
- Newcomer rows: 30 newcomer-directed messages, 5 distinct newcomers.

A cell below its floor renders every rate as `None` (`insufficient data`,
never silently omitted -- COMMUNITY-HEALTH.md §5.1), mirroring `aggregate.
py`'s own convention for the message-level metrics.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Callable

from project_health.private_run.newcomer import DEFAULT_NEWCOMER_N  # noqa: F401  (re-exported)
from project_health.private_run.stats import percentile_interval
from project_health.private_run.thread_derive import (
    OUTCOME_ABANDONED,
    OUTCOME_RESOLVED,
    ThreadDerivation,
)

DEFAULT_BOOTSTRAP_ITERATIONS = 1000

# §5.2, verbatim.
THREAD_MIN_QUALIFYING = 30
THREAD_MIN_DISTINCT_PARTICIPANTS = 5
NEWCOMER_MIN_MESSAGES = 30
NEWCOMER_MIN_DISTINCT_NEWCOMERS = 5


@dataclass(frozen=True)
class NewcomerMessage:
    """One classified message whose `directed_at` target was a newcomer
    (fewer than N prior messages in this venue, across the *whole* Phase-1
    metadata, as of this message's `posted_at` -- COMMUNITY-HEALTH.md §2.3
    rule 8) at message time. `target_author`/`responder_author` are raw,
    in-memory-only author strings (never persisted; see `identity.py`)."""

    target_author: str
    responder_author: str
    tier: int


def _weighted_predicate_ratio(
    threads: list[ThreadDerivation],
    numerator: Callable[[ThreadDerivation], bool],
    denominator: Callable[[ThreadDerivation], bool],
) -> float | None:
    denom_weight = sum(t.weight for t in threads if denominator(t))
    if denom_weight <= 0:
        return None
    numer_weight = sum(t.weight for t in threads if numerator(t) and denominator(t))
    return numer_weight / denom_weight


def _bootstrap_ratio_ci(
    threads: list[ThreadDerivation],
    numerator: Callable[[ThreadDerivation], bool],
    denominator: Callable[[ThreadDerivation], bool],
    *,
    seed: int,
    iterations: int,
    alpha: float = 0.05,
) -> tuple[float, float] | None:
    n = len(threads)
    if n == 0:
        return None
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(iterations):
        resampled = [threads[rng.randrange(n)] for _ in range(n)]
        rate = _weighted_predicate_ratio(resampled, numerator, denominator)
        if rate is not None:
            samples.append(rate)
    if not samples:
        return None
    return percentile_interval(samples, alpha)


def _rate_entry(
    threads: list[ThreadDerivation],
    numerator: Callable[[ThreadDerivation], bool],
    denominator: Callable[[ThreadDerivation], bool],
    *,
    seed: int,
    iterations: int,
    min_qualifying: int,
    min_distinct: int,
) -> dict[str, Any]:
    qualifying = [t for t in threads if denominator(t)]
    distinct_participants: set[str] = set()
    for t in qualifying:
        distinct_participants |= t.participant_authors
    insufficient = len(qualifying) < min_qualifying or len(distinct_participants) < min_distinct

    entry: dict[str, Any] = {
        "qualifying_threads": len(qualifying),
        "distinct_participants": len(distinct_participants),
        "insufficient_data": insufficient,
        "rate": None,
        "ci95": None,
    }
    if insufficient:
        return entry

    point = _weighted_predicate_ratio(threads, numerator, denominator)
    ci = _bootstrap_ratio_ci(threads, numerator, denominator, seed=seed, iterations=iterations)
    entry["rate"] = point
    entry["ci95"] = list(ci) if ci is not None else None
    return entry


def _pile_on_entry(
    threads: list[ThreadDerivation], *, seed: int, iterations: int
) -> dict[str, Any]:
    distinct_targets: set[str] = set()
    for t in threads:
        distinct_targets.update(t.pile_on_target_authors)
    insufficient = len(threads) < THREAD_MIN_QUALIFYING or len(
        distinct_targets
    ) < THREAD_MIN_DISTINCT_PARTICIPANTS

    entry: dict[str, Any] = {
        "threads_total": len(threads),
        "distinct_targets": len(distinct_targets),
        "insufficient_data": insufficient,
        "per_100_threads": None,
        "ci95": None,
    }
    if insufficient:
        return entry

    def _point(items: list[ThreadDerivation]) -> float | None:
        denom = sum(t.weight for t in items)
        if denom <= 0:
            return None
        numer = sum(t.weight * len(t.pile_on_target_authors) for t in items)
        return (numer / denom) * 100.0

    entry["per_100_threads"] = _point(threads)

    n = len(threads)
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(iterations):
        resampled = [threads[rng.randrange(n)] for _ in range(n)]
        rate = _point(resampled)
        if rate is not None:
            samples.append(rate)
    entry["ci95"] = list(percentile_interval(samples, 0.05)) if samples else None
    return entry


def aggregate_thread_metrics(
    threads: list[ThreadDerivation],
    *,
    seed: int,
    cell_key: str,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
) -> dict[str, Any]:
    """§5.2's four thread-level rates for one (venue, period) cell of
    already-derived threads, each independently gated by its own floor."""

    def _seed_for(label: str) -> int:
        # Same "deterministic per-(cell, metric) seed" convention as
        # `aggregate._cell_seed`, re-derived here rather than imported to
        # keep this module's bootstrap self-contained.
        import hashlib

        digest = hashlib.sha256(f"{seed}:{cell_key}:{label}".encode("utf-8")).hexdigest()
        return int(digest[:8], 16)

    return {
        "threads_total": len(threads),
        "escalation_rate": _rate_entry(
            threads,
            lambda t: t.escalation,
            lambda t: t.has_technical_disagreement,
            seed=_seed_for("escalation_rate"),
            iterations=bootstrap_iterations,
            min_qualifying=THREAD_MIN_QUALIFYING,
            min_distinct=THREAD_MIN_DISTINCT_PARTICIPANTS,
        ),
        "constructive_resolution_rate": _rate_entry(
            threads,
            lambda t: t.outcome == OUTCOME_RESOLVED,
            lambda t: t.has_disagreement_or_friction,
            seed=_seed_for("constructive_resolution_rate"),
            iterations=bootstrap_iterations,
            min_qualifying=THREAD_MIN_QUALIFYING,
            min_distinct=THREAD_MIN_DISTINCT_PARTICIPANTS,
        ),
        "thread_abandonment_rate_post_friction": _rate_entry(
            threads,
            lambda t: t.outcome == OUTCOME_ABANDONED,
            lambda t: t.reached_friction,
            seed=_seed_for("thread_abandonment_rate_post_friction"),
            iterations=bootstrap_iterations,
            min_qualifying=THREAD_MIN_QUALIFYING,
            min_distinct=THREAD_MIN_DISTINCT_PARTICIPANTS,
        ),
        "pile_on_rate": _pile_on_entry(
            threads, seed=_seed_for("pile_on_rate"), iterations=bootstrap_iterations
        ),
    }


def _weighted_newcomer_ratio(
    messages: list[NewcomerMessage], predicate: Callable[[NewcomerMessage], bool]
) -> float | None:
    if not messages:
        return None
    matching = sum(1 for m in messages if predicate(m))
    return matching / len(messages)


def _bootstrap_newcomer_ci(
    messages: list[NewcomerMessage],
    predicate: Callable[[NewcomerMessage], bool],
    *,
    seed: int,
    iterations: int,
) -> tuple[float, float] | None:
    n = len(messages)
    if n == 0:
        return None
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(iterations):
        resampled = [messages[rng.randrange(n)] for _ in range(n)]
        samples.append(sum(1 for m in resampled if predicate(m)) / n)
    return percentile_interval(samples, 0.05)


def aggregate_newcomer_rows(
    messages: list[NewcomerMessage],
    *,
    seed: int,
    cell_key: str,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
) -> dict[str, Any]:
    """§5.2's two newcomer response rates ("constructive" and "dismissive/
    hostile"), reported separately and never netted (§5.1's no-composite
    rule, restated explicitly for this pair in §5.2's own Notes column)."""
    distinct_newcomers = {m.target_author for m in messages}
    insufficient = (
        len(messages) < NEWCOMER_MIN_MESSAGES
        or len(distinct_newcomers) < NEWCOMER_MIN_DISTINCT_NEWCOMERS
    )

    entry: dict[str, Any] = {
        "messages_directed_at_newcomers": len(messages),
        "distinct_newcomers": len(distinct_newcomers),
        "insufficient_data": insufficient,
        "constructive_response_rate": None,
        "constructive_response_rate_ci95": None,
        "dismissive_hostile_response_rate": None,
        "dismissive_hostile_response_rate_ci95": None,
    }
    if insufficient:
        return entry

    import hashlib

    def _seed_for(label: str) -> int:
        digest = hashlib.sha256(f"{seed}:{cell_key}:{label}".encode("utf-8")).hexdigest()
        return int(digest[:8], 16)

    constructive = lambda m: m.tier <= -1  # noqa: E731
    hostile = lambda m: m.tier >= 2  # noqa: E731

    entry["constructive_response_rate"] = _weighted_newcomer_ratio(messages, constructive)
    ci = _bootstrap_newcomer_ci(
        messages, constructive, seed=_seed_for("constructive"), iterations=bootstrap_iterations
    )
    entry["constructive_response_rate_ci95"] = list(ci) if ci is not None else None

    entry["dismissive_hostile_response_rate"] = _weighted_newcomer_ratio(messages, hostile)
    ci = _bootstrap_newcomer_ci(
        messages, hostile, seed=_seed_for("hostile"), iterations=bootstrap_iterations
    )
    entry["dismissive_hostile_response_rate_ci95"] = list(ci) if ci is not None else None
    return entry
