"""Thread-level derivation for the private Cassandra communication run
(issue #114; COMMUNITY-HEALTH.md §2.2's intensity tiers, §2.3's deterministic
derivation rules).

Operates purely on `(order_index, author_raw, parent_call_id, posted_at,
tier, labels_present)` tuples already assembled by `runner.py` from the
sampled/classified messages of one thread -- no LLM call, matching §1.3's
"thread-level labels are never asked of the model directly" design. `tier`
is computed here from a `ClassificationRecord`'s label probabilities via the
fixed §2.2 lookup table; every other rule below (escalation, de-escalation,
pile-on, resolution, abandonment) is a pure function of the ordered tuple
sequence.

**Documented ambiguity resolutions** (COMMUNITY-HEALTH.md §2.3 is written in
prose, not pseudocode; issue #114 asks that ambiguities be resolved with the
simplest reading and documented here and in the report):

1. **`directed_at`.** §2.1's schema defines it as "parent author plus any
   explicit @-mentions". This module implements only the parent-author half
   -- @-mention extraction from message text is not implemented (it would
   require re-parsing raw message bodies downstream of the point they are
   discarded, D18). A message's `directed_at` target is therefore always
   either its parent's author or `None` (thread root, or a parent that was
   filtered out / not classified -- see #2).
2. **Parent resolution is scoped to the thread's own survivors, not the
   full raw stream.** For dev@, a message's parent is its `In-Reply-To`
   target *only if that target survived this run's automated-sender filter
   and per-thread cap* -- not any raw ancestor. For JIRA, a comment's parent
   is simply the previous **surviving** comment in order (not JIRA's raw
   previous comment, which may have been filtered as automated) -- JIRA
   comment streams have no reply-to concept of their own, so "the previous
   comment on the issue" is already this project's established parent rule
   (`classify/text_fetch.py`'s `resolve_parent`); restricting it to
   survivors keeps a message's `directed_at` always pointing at another
   *classified* participant, which is what every downstream rule needs.
3. **Escalation's "two different author_refs".** Read as: at least two
   distinct authors must each have authored a message that struck a new
   running-maximum tier during the climb (not merely "two authors posted
   somewhere in the thread") -- this is what makes a single participant's
   multi-message rant *not* count as thread escalation, per §2.3 rule 3's
   own parenthetical. The thread's first message establishes the initial
   running maximum rather than counting as a climb itself (there is no
   prior tier for it to "strictly increase" relative to), so a root message
   alone never credits its author toward the two-distinct-authors
   requirement.
4. **De-escalation's "first subsequent message... provided no later message
   returns to tier >= 3".** Read as: after the thread's *last* tier >= 3
   message (not necessarily the first escalation peak -- a thread can spike
   more than once), does at least one message afterward drop to tier <= 1?
   This is the simplest reading that is mathematically identical to the
   spec's "first qualifying message, provided nothing later undoes it" for
   a thread with exactly one spike, and generalizes cleanly to threads with
   more than one.
5. **Resolution's "last k messages"** gates *both* the closing evidence
   (`resolution_marker`) and the `compromise_offer` -> `acknowledgment` pair
   to originate within the tail window itself, not merely be corroborated
   by something in the tail -- the simplest reading of "look at the last k
   messages... if any carries...".
6. **Abandonment's target ("highest-tier message" in §2.3 rule 7 vs. "last
   high-intensity message" in §1.2's label 17).** Reconciled as: the
   thread's temporally *last* message with tier >= 2 (friction or worse) --
   satisfies both phrasings for the common case (the last friction message
   is usually also the peak), and is unambiguous when they'd otherwise
   disagree.
7. **Only thread-scoped abandonment is computed.** §2.3 rule 7's
   project-scoped upgrade ("upgraded to `abandoned (project-scoped)`... only
   after joining against that participant's project-wide activity... a
   nightly job finalizes it") describes a standing, time-deferred nightly
   process this project does not yet have; `private-run` is a point-in-time
   batch job, not that nightly job. This module computes `abandoned`
   (thread-scoped) only, and the report labels it as such.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# §2.2's fixed intensity-tier lookup table. "Highest tier wins if multiple
# apply" (the table's own words) means a message's tier is simply the max
# over every triggered entry below -- which already reproduces the table's
# extra "(without a tier >= 2 label)" qualifiers on tier 1 and tier -1 for
# free, since those would always lose to a co-occurring tier >= 2 label
# under a max anyway.
LABEL_TIER: dict[str, int] = {
    "resolution_marker": -2,
    "acknowledgment": -1,
    "compromise_offer": -1,
    "technical_disagreement": 1,
    "dismissiveness": 2,
    "sarcasm": 2,
    "gatekeeping": 2,
    "status_authority_invocation": 2,
    "hostility": 3,
    "personal_attack": 4,
}

# §2.3 rule 6's default tail window.
DEFAULT_RESOLUTION_WINDOW_K = 3

OUTCOME_RESOLVED = "resolved"
OUTCOME_ABANDONED = "abandoned"
OUTCOME_INDETERMINATE = "indeterminate"


def intensity_tier(labels_present: frozenset[str] | set[str]) -> int:
    """§2.2's tier for one message, given the set of its labels whose
    probability cleared the cutoff already applied by the caller. `0`
    (neutral) if no label present maps to a tier -- covers both "no labels
    present" and "only `evidence_based_argument`/`constructive_
    counterargument` present", neither of which appears in `LABEL_TIER`."""
    triggered = [LABEL_TIER[label] for label in labels_present if label in LABEL_TIER]
    return max(triggered) if triggered else 0


def labels_present_at_cutoff(probabilities: dict[str, float], cutoff: float) -> frozenset[str]:
    """The subset of `probabilities` (label_id -> raw Jev probability)
    whose probability is `>= cutoff` -- the code-side "present" decision
    §4.5 describes, applied here at a fixed, uncalibrated cutoff (this
    project's D23-pending-calibration convention; see `aggregate.py`'s own
    `CUTOFFS`)."""
    return frozenset(label for label, probability in probabilities.items() if probability >= cutoff)


@dataclass(frozen=True)
class ThreadMessage:
    """One classified message within a thread, ordered and tiered. `author_raw`
    is an in-memory-only raw sender string (never persisted; see this
    module's docstring and `identity.py`)."""

    call_id: str
    order_index: int
    author_raw: str
    posted_at: str
    parent_call_id: str | None
    tier: int
    labels_present: frozenset[str]


@dataclass(frozen=True)
class ThreadDerivation:
    """§2.3's full derived block for one thread, at one intensity cutoff."""

    thread_key: str
    weight: float
    n_messages: int
    participant_authors: frozenset[str]
    has_technical_disagreement: bool
    has_disagreement_or_friction: bool  # >=1 message at tier >= 1
    reached_friction: bool  # >=1 message at tier >= 2
    escalation: bool
    deescalation: bool
    outcome: str  # "resolved" | "abandoned" | "indeterminate"
    abandoned_target_author: str | None
    pile_on_target_authors: tuple[str, ...] = field(default_factory=tuple)
    # Issue #122 (D27): the thread's peak §2.2 intensity tier across every
    # ordered message -- `max(m.tier for m in ordered)`, distinct from
    # `_derive_escalation`'s own `highest_reached` (which is only ever
    # computed/used internally to decide *whether* the thread escalates).
    # `0` for an empty thread (never constructed in practice -- `derive_
    # thread` always receives >=1 message -- but a safe default all the
    # same, matching `intensity_tier`'s own "no label present" -> `0`).
    peak_tier: int = 0


def _directed_at(
    message: ThreadMessage, by_call_id: dict[str, ThreadMessage]
) -> str | None:
    if message.parent_call_id is None:
        return None
    parent = by_call_id.get(message.parent_call_id)
    return parent.author_raw if parent is not None else None


def _derive_escalation(messages: list[ThreadMessage]) -> tuple[bool, int]:
    if not messages:
        return False, 0
    # The thread's first message establishes the baseline running maximum,
    # not a "strict increase" -- there is no prior tier for it to increase
    # relative to, so its author is never credited as a climber on its own
    # (otherwise a thread root would always count toward the "two different
    # author_refs" requirement regardless of whether the thread actually
    # climbs afterward).
    running_max = messages[0].tier
    highest_reached = messages[0].tier
    climbers: set[str] = set()
    for message in messages[1:]:
        if message.tier > highest_reached:
            highest_reached = message.tier
        if message.tier > running_max:
            climbers.add(message.author_raw)
            running_max = message.tier
    escalation = len(climbers) >= 2 and highest_reached >= 3
    return escalation, highest_reached


def _derive_deescalation(messages: list[ThreadMessage], escalation: bool) -> bool:
    if not escalation:
        return False
    high_indices = [i for i, m in enumerate(messages) if m.tier >= 3]
    if not high_indices:
        return False
    last_high = high_indices[-1]
    return any(m.tier <= 1 for m in messages[last_high + 1 :])


def _derive_pile_on_targets(
    messages: list[ThreadMessage], directed_at: list[str | None]
) -> tuple[str, ...]:
    by_target: dict[str, set[str]] = {}
    for message, target in zip(messages, directed_at):
        if message.tier >= 2 and target is not None:
            by_target.setdefault(target, set()).add(message.author_raw)
    return tuple(target for target, authors in by_target.items() if len(authors) >= 3)


def _find_resolution_index(
    messages: list[ThreadMessage], k: int
) -> int | None:
    n = len(messages)
    tail_start = max(0, n - k)
    for i in range(tail_start, n):
        if "resolution_marker" in messages[i].labels_present:
            return i
    for i in range(tail_start, n):
        if "compromise_offer" not in messages[i].labels_present:
            continue
        for j in range(i + 1, n):
            if (
                "acknowledgment" in messages[j].labels_present
                and messages[j].author_raw != messages[i].author_raw
            ):
                return j
    return None


def _derive_outcome(
    messages: list[ThreadMessage], directed_at: list[str | None], k: int
) -> tuple[str, str | None]:
    n = len(messages)
    if n == 0:
        return OUTCOME_INDETERMINATE, None

    resolution_index = _find_resolution_index(messages, k)
    if resolution_index is not None and all(m.tier < 3 for m in messages[resolution_index + 1 :]):
        return OUTCOME_RESOLVED, None

    friction_indices = [i for i, m in enumerate(messages) if m.tier >= 2]
    if not friction_indices:
        return OUTCOME_INDETERMINATE, None

    last_friction_idx = friction_indices[-1]
    target = directed_at[last_friction_idx]
    if target is None:
        return OUTCOME_INDETERMINATE, None

    posted_again = any(m.author_raw == target for m in messages[last_friction_idx + 1 :])
    if posted_again:
        return OUTCOME_INDETERMINATE, None
    return OUTCOME_ABANDONED, target


def derive_thread(
    thread_key: str,
    weight: float,
    messages: list[ThreadMessage],
    *,
    resolution_window_k: int = DEFAULT_RESOLUTION_WINDOW_K,
) -> ThreadDerivation:
    """§2.3 rules 1-7 applied to one thread's already-ordered, already-tiered
    `messages` (ordering is the caller's responsibility -- `runner.py` sorts
    by `order_index` before calling this, per rule 1's "build the reply
    tree... break ties or fill gaps with posted_at, then order_index")."""
    ordered = sorted(messages, key=lambda m: m.order_index)
    by_call_id = {m.call_id: m for m in ordered}
    directed_at = [_directed_at(m, by_call_id) for m in ordered]

    has_technical_disagreement = any(
        "technical_disagreement" in m.labels_present for m in ordered
    )
    has_disagreement_or_friction = any(m.tier >= 1 for m in ordered)
    reached_friction = any(m.tier >= 2 for m in ordered)

    escalation, _peak = _derive_escalation(ordered)
    deescalation = _derive_deescalation(ordered, escalation)
    pile_on_targets = _derive_pile_on_targets(ordered, directed_at)
    outcome, abandoned_target = _derive_outcome(ordered, directed_at, resolution_window_k)
    peak_tier = max((m.tier for m in ordered), default=0)

    return ThreadDerivation(
        thread_key=thread_key,
        weight=weight,
        n_messages=len(ordered),
        participant_authors=frozenset(m.author_raw for m in ordered),
        has_technical_disagreement=has_technical_disagreement,
        has_disagreement_or_friction=has_disagreement_or_friction,
        reached_friction=reached_friction,
        escalation=escalation,
        deescalation=deescalation,
        outcome=outcome,
        abandoned_target_author=abandoned_target,
        pile_on_target_authors=pile_on_targets,
        peak_tier=peak_tier,
    )
