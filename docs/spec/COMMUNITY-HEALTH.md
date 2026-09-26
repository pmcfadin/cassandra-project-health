# COMMUNITY-HEALTH.md — Communication and Interaction Health Model

Deliverable #3 of the research phase (`docs/research.md`). This document proposes the
communication taxonomy, thread-level model, disagreement-vs-toxicity framing, classifier
architecture, aggregate metrics, validation strategy, and ethical rules for analyzing
human communication in the Apache Cassandra community. It must be read together with
`docs/spec/DECISIONS.md` ("D#" references below point to that log) and does not restate
deterministic-metrics design, data-source access details, or scoring/aggregation-of-
dimensions design — those belong to `METRICS.md`, `DATA-SOURCES.md`, and `SCORING.md`.

**Status of everything in this document: proposed design for Phase 2a/2b, not yet built,
not yet validated.** Nothing described here may publish a classified metric until it
clears the validation gates in §6 and the governance steps in §7.

---

## 0. Scope and phasing (recap of D1, binding)

- **Phase 2a — classification.** Sources: `dev@` and `user@` mailing lists, JIRA
  comments, GitHub PR comments on `apache/cassandra` (D7). Begins only after a frozen
  human-labeled benchmark corpus exists and the classifier clears the gates in §6.
- **Phase 2b — ASF Slack.** Requires PMC consensus on `dev@` and ASF Infra approval of a
  read-only bot on public channels (`#cassandra`, `#cassandra-dev`). Raw Slack text is
  **never** committed to the public repo or data branch; it is classified in-run and only
  aggregate counts persist. No quotes, no links, no individual attribution (D1).
- Every rule in this document that says "never store raw text" or "never attribute to an
  individual" applies to **all** sources, not just Slack. Slack's rules are strictly
  stricter (no raw-text persistence at all, not even off-`main`), not a different regime.
- This document assumes the Phase 1 deterministic layer (identity resolution, contributor
  roles, participation history) already exists and is queryable, since several derived
  thread-level and newcomer metrics join classifier output against it. See `METRICS.md`
  for that layer's design.

Everything classified in this document is tier `classified` in the sense of D2.1: it
carries more uncertainty than `established`/`proxy`/`experimental` deterministic metrics
and must be labeled as such everywhere it is displayed.

---

## 1. Communication taxonomy

### 1.1 Design principle

The literature is consistent on one point: general-purpose sentiment and toxicity tools
built for product reviews or web comments perform poorly on software engineering text and
disagree with each other enough to change study conclusions (Jongeling et al., 2017;
Novielli, Girardi & Lanubile, 2018). Off-the-shelf toxicity classifiers likewise misfire on
SE-specific vocabulary — flagging words like "kill", "dump", or "junk" as toxic when they
are ordinary technical usage (Sarker, Turzo & Bosu, 2020). The response in this project is
not to build a better sentiment score; it is to replace "sentiment" with a fixed set of
**operationally defined, observable phenomena**, each with a precise definition and worked
examples, so that two people (or a model and a person) applying the definition to the same
message should usually agree. Definitions below are written to be applied without
knowledge of who the participants are — the classifier sees message text and immediate
thread context only, never author identity, tenure, or affiliation (see §4).

All examples are synthetic (invented for this document), not quotes from real Cassandra
contributors, mailing-list posts, or JIRA/GitHub comments, per the "quotations: default
none" rule in §7.

### 1.2 Label reference table

Two units of analysis exist:

- **message** — the smallest classified unit: one email, one JIRA comment, one GitHub PR
  review comment, or (Phase 2b) one Slack message.
- **thread** — an ordered, reply-linked set of messages (an email thread, a JIRA issue's
  comment stream, a PR's review-comment stream, a Slack thread). Thread-level phenomena
  are never asked of the classifier directly (see §1.3); they are **derived
  deterministically** from message-level labels plus the reply graph and timestamps.

| # | Label | Unit | Origin | Definition |
|---|---|---|---|---|
| 1 | `technical_disagreement` | message | LLM-classified | The message disputes a technical claim, approach, or decision on its merits — a competing design, a correctness concern, a performance claim, a different reading of a spec. Presence of disagreement alone says nothing about health; see §3. |
| 2 | `constructive_counterargument` | message | LLM-classified | The message disagrees (co-occurs with #1 in most but not all cases) while directly engaging the prior argument's substance: it responds to the specific claim, offers a reason, or proposes an alternative, rather than dismissing the person or the topic wholesale. |
| 3 | `evidence_based_argument` | message | LLM-classified | The message grounds its claim in something checkable: a benchmark number, a reproduction case, a spec citation, a code reference, a linked prior discussion, test results. Distinguished from #2 by the presence of a citable/checkable artifact, not just engagement. |
| 4 | `compromise_offer` | message | LLM-classified | The message proposes a middle position, a scoped-down version of a prior proposal, a conditional acceptance ("I'll agree to X if we also do Y"), or explicitly cedes a point while holding another. |
| 5 | `acknowledgment` | message | LLM-classified | The message recognizes another participant's point, correction, effort, or contribution without necessarily agreeing with the underlying position ("Good catch, I hadn't considered that" / "Thanks for digging into this"). |
| 6 | `personal_attack` | message | LLM-classified | The message targets a participant's competence, character, intelligence, or worth as a person rather than their argument or code ("you clearly don't understand how this works", name-calling, insults). This is the most reputationally loaded label; see the precision floor in §6.4. |
| 7 | `hostility` | message | LLM-classified | The message expresses anger, contempt, or aggression toward a participant or the discussion itself, without necessarily naming a personal deficiency (shouting via caps/punctuation, "this is absurd, stop wasting everyone's time," threats to escalate socially rather than technically). Distinguished from #6: hostility can be directed at an idea or situation; a personal attack is specifically about the person. |
| 8 | `dismissiveness` | message | LLM-classified | The message declines to engage with a point's substance while signaling it isn't worth engaging with — "no," "not going to happen," "this has been discussed, move on" with no reasoning given, ignoring a direct question repeatedly. Distinguished from disagreement: dismissiveness is disagreement *without* argument. |
| 9 | `sarcasm` | message | LLM-classified | The message uses irony or a mocking tone where literal and intended meaning diverge, typically to belittle a point or person ("Oh sure, because that's worked so well every other time"). |
| 10 | `gatekeeping` | message | LLM-classified | The message asserts that a participant lacks standing to contribute, comment, or be taken seriously — based on tenure, role, prior contributions, or "who they are" rather than the content of what they said ("this isn't a decision for people who haven't touched the storage engine," "you need to have committed here before you get an opinion on this"). |
| 11 | `status_authority_invocation` | message | LLM-classified | The message settles or attempts to settle a point by reference to the speaker's own role, seniority, or position ("as the person who wrote this originally, I'm saying no") rather than by argument. Distinguished from gatekeeping: authority invocation is about the *speaker's* standing; gatekeeping is about denying the *other party's* standing. Both can be present in the same message. |
| 12 | `resolution_marker` | message | LLM-classified | The message explicitly closes the disagreement — an agreement reached, a decision announced, a "LGTM"/"+1, committing this," a "let's go with your approach." |
| 13 | `escalation` | thread | derived (§2.3) | A trajectory pattern: message-level intensity (§2.2) increases over the course of the thread, reaching a high-intensity label. |
| 14 | `de-escalation` | thread | derived (§2.3) | A trajectory pattern: after an escalation, intensity returns to low/neutral or a positive label appears, and does not re-escalate before the thread ends. |
| 15 | `pile-on` | thread | derived (§2.3) | Three or more distinct participants each direct a high-intensity message at the same target participant within the same thread. |
| 16 | `resolution` | thread | derived (§2.3) | The thread's terminal state contains a resolution marker or a compromise-then-acknowledgment pair, with no high-intensity message afterward. |
| 17 | `thread_abandonment` | thread | derived (§2.3) | The thread ends without resolution, and the participant on the receiving end of the thread's last high-intensity message does not post again in the thread nor return to the project within a configured window. |
| 18 | `newcomer_treatment` | thread/rollup | derived (§2.3, joined against Phase-1 identity data) | Not a single label but a distribution: for messages directed at a participant classified as a newcomer at message time (Phase 1 role data), the rates of constructive vs. dismissive/hostile responses they receive. |

An optional, **non-gating** auxiliary field, `sentiment_polarity` (negative / neutral /
positive / mixed), may be recorded per message for observability and future research, but
per the core design principle (research.md, "Do not equate negative sentiment with
unhealthy communication") it is never used in any published aggregate, gate, or dashboard
metric. It exists only so that, if a future analysis wants to check whether the taxonomy
labels correlate with raw sentiment (they should not, strongly — see §3), the data is
available without a re-run.

### 1.3 Why the thread-level labels are never asked of the model directly

Labels 13–18 could, in principle, be asked of an LLM directly ("does this thread show
escalation?"). This design deliberately does not do that, for three reasons that follow
directly from the research brief's classification architecture ("prefer structured
classification... The scoring system should convert classifications into metrics rather
than allowing the model to determine the final project score directly"):

1. **Auditability.** A thread-level judgment from an LLM is a black box with no decision
   trail. A thread-level label computed by a fixed, versioned function of message-level
   labels and the reply graph can be inspected, re-run by hand, and disputed with
   reference to the exact messages and rule that produced it.
2. **Determinism under model change.** If "escalation" were an LLM judgment, changing the
   model or prompt would change past escalation calls in ways nobody could explain. If it
   is a deterministic function of message-level labels, a model change only changes the
   (auditable, gated) message-level labels feeding into an unchanged function — isolating
   what actually moved.
3. **Context economy.** Message-level labeling can be done per-message with a bounded
   context window (the message plus immediate parent), which is what keeps each
   classifier call's state small and cheap to run concurrently (§4.4). Thread-level
   judgments would require re-reading entire threads per classification, with no
   corresponding accuracy benefit once message-level labels are reliable — and a
   full-thread context window would run directly into the documented weakness that
   unrelated or oversized context degrades a classifier's answers (§4.4).

### 1.4 Worked examples

Two examples per message-level label, kept short and clearly synthetic.

| Label | Positive example (label applies) | Negative example (label does not apply, included to mark the boundary) |
|---|---|---|
| `technical_disagreement` | "I don't think a single global lock is going to hold up under the write pattern we see in production; the compaction path needs its own lock." | "Thanks, this patch looks reasonable to me." (no dispute raised) |
| `constructive_counterargument` | "The benchmark you linked used a single node; under a 3-node RF=3 setup the tail latency more than doubles, so I don't think the conclusion generalizes — can we re-run with that topology?" | "That's wrong." (disagreement with no engagement — this is `dismissiveness`, not `constructive_counterargument`) |
| `evidence_based_argument` | "Here's a flame graph from a 500-node soak test showing 40% of time in this codepath (link); the proposed change removes that allocation entirely." | "I'm pretty sure this will be faster." (claim asserted, nothing checkable offered) |
| `compromise_offer` | "I'll drop the API rename if we keep the internal restructuring — that's the part that actually matters for CASSANDRA-XXXXX." | "Fine, whatever, do it your way." (concession without a held position — closer to `dismissiveness` of one's own point, not a negotiated middle) |
| `acknowledgment` | "Good catch — I missed that this path is also hit during streaming, not just compaction." | "Noted." with no other content, in a context where a substantive rebuttal was expected — borderline; classifiers should mark low confidence rather than force a call (see §4.5). |
| `personal_attack` | "You clearly have no idea how the storage engine works — maybe read the code before commenting." | "This patch has a correctness bug in the flush path." (criticism of the code, not the person) |
| `hostility` | "I am done explaining this basic point over and over. This is a waste of everyone's time." | "I disagree, and here's why: ..." (firm disagreement, no aggression) |
| `dismissiveness` | "No. Not doing this." with no reasoning, in response to a substantive technical question. | "No — this was already rejected in CASSANDRA-YYYYY for the reasons discussed there; see that thread for the tradeoffs." (a "no" with a reason and a pointer is not dismissiveness, even though it is terse) |
| `sarcasm` | "Oh great, another rewrite of the same subsystem. That's *definitely* going to be the one that sticks." | "I'm skeptical this rewrite will hold up better than the last one — what's different this time?" (same skepticism, stated directly) |
| `gatekeeping` | "This isn't really a call for someone who hasn't worked on the storage engine before." | "This touches the storage engine pretty deeply — pulling in [committer] for a review before we merge." (routing a review by expertise, not denying standing to speak) |
| `status_authority_invocation` | "I designed this subsystem. It stays as-is." offered as the entire justification. | "I designed this subsystem, and the reason it's structured this way is X — happy to be argued out of it." (authority is mentioned, but an argument is also given; classifiers should flag `status_authority_invocation` present at low confidence and let §2 combine it with the argument labels rather than forcing an either/or.) |
| `resolution_marker` | "Agreed — going with your approach, committing this today." | "I'll think about it." (no closure) |

---

## 2. Thread-level model

### 2.1 Thread data schema

A thread is the append-only structural record for one conversation. It never stores raw
message text inline — text lives in the normalized immutable conversation store (§4.1),
referenced here by hash — so the thread schema itself is safe to keep even for Slack
(Phase 2b), where raw text must never be persisted at all.

```json
{
  "thread_id": "string — stable id, source-prefixed (e.g. ml:dev:2026-09:abcd1234)",
  "project": "string — e.g. apache/cassandra",
  "source": "mailing_list | jira_comment | github_pr_comment | slack",
  "external_ref": {
    "list": "dev@cassandra.apache.org (mailing_list only)",
    "jira_issue": "CASSANDRA-NNNNN (jira_comment only)",
    "pr_number": "int (github_pr_comment only)",
    "channel": "#cassandra-dev (slack only; not retained if Phase 2b storage rules forbid it — see §7.4)"
  },
  "topic_hint": "string, optional — subject line / issue title / PR title, deterministic metadata, not a classifier output",
  "participants": [
    {
      "participant_ref": "opaque, salted, resolved-identity reference — never a raw email or username; join key into the Phase-1 identity/role store",
      "role_at_thread_start": "newcomer | regular | committer | pmc | unknown",
      "first_message_id": "string"
    }
  ],
  "messages": [
    {
      "message_id": "string, stable",
      "parent_message_id": "string or null — reply-to edge; null = thread root",
      "author_ref": "participant_ref",
      "posted_at": "ISO 8601 timestamp",
      "order_index": "int — deterministic fallback ordering when reply metadata is missing/ambiguous",
      "content_hash": "sha256 of normalized message text — the join key into classification records (§4.1); never the text itself",
      "directed_at": ["participant_ref, ...] — deterministic: parent author plus any explicit @-mentions; not inferred by the LLM"
    }
  ],
  "derived": {
    "escalation_events": [ {"from_message_id": "...", "to_message_id": "...", "rule_version": "..."} ],
    "deescalation_events": [ "..." ],
    "pile_on_events": [ {"target_participant_ref": "...", "message_ids": ["...", "...", "..."], "window_start": "...", "window_end": "..."} ],
    "outcome": "resolved | abandoned | ongoing | indeterminate",
    "outcome_evidence": ["message_id, ...] — the specific messages the outcome rule keyed on",
    "outcome_rule_version": "string — the deterministic-derivation config version (§2.3), independent of classifier_version"
  }
}
```

Every `derived` field carries an explicit evidence pointer back to the message IDs and the
rule version that produced it — this is what makes a thread-level call reproducible: given
the same message-level classification records and the same `outcome_rule_version`, a third
party gets the same `derived` block.

### 2.2 Intensity tiers (the deterministic backbone of trajectory derivation)

Message-level labels are mapped to an **intensity tier** by a fixed, versioned lookup
table — not learned, not inferred, just configuration:

| Tier | Meaning | Labels that trigger it (any present → this tier; highest tier wins if multiple apply) |
|---|---|---|
| −2 | Closing/positive | `resolution_marker` |
| −1 | Constructive/positive | `acknowledgment`, `compromise_offer` (without co-occurring tier ≥2 label) |
| 0 | Neutral | No labels present, or only `evidence_based_argument`/`constructive_counterargument` alone |
| 1 | Substantive disagreement | `technical_disagreement` (without a tier ≥2 label) |
| 2 | Non-substantive friction | `dismissiveness`, `sarcasm`, `gatekeeping`, `status_authority_invocation` |
| 3 | Hostile | `hostility` |
| 4 | Attack | `personal_attack` |

This table is itself versioned (`intensity_map_version`) alongside `outcome_rule_version`
and is expected to be revisited during the Phase 1 pilot; a change to it, like a change to
the classifier, triggers a full-history recompute (D2.6) and a changelog entry.

### 2.3 Deterministic derivation rules

All rules below operate purely on `(message_id, author_ref, directed_at, posted_at,
parent_message_id, intensity_tier)` tuples plus, for #6, Phase-1 role/participation data.
No step re-invokes the LLM.

1. **Order the thread.** Build the reply tree from `parent_message_id`; break ties or fill
   gaps with `posted_at`, then `order_index`.
2. **Tier every message** using the table in §2.2 from its classification record's labels.
3. **Escalation.** Walk the ordered sequence. An escalation event is recorded starting at
   the first message where tier strictly increases relative to the running maximum tier
   seen so far in the thread, across at least two different `author_ref`s (a single
   participant venting alone is not, by this definition, thread escalation — it is a
   candidate `hostility`/`personal_attack` label on that person's message, but escalation
   requires the *thread* to be climbing), and the running maximum reaches tier ≥ 3 at some
   point in the walk.
4. **De-escalation.** After an escalation event reaches its peak tier, a de-escalation
   event is recorded at the first subsequent message whose tier ≤ 1 or which carries tier
   −1/−2 labels, provided no later message in the thread returns to tier ≥ 3.
5. **Pile-on.** For each message with tier ≥ 2, look at its `directed_at` target. If three
   or more **distinct** `author_ref`s each produce a tier ≥ 2 message directed at the same
   target within a configurable window (default: the same thread, no time cap, since
   mailing-list threads can span days — a time cap is a tunable parameter for Phase 2b
   Slack, where a window measured in hours is more appropriate), a pile-on event is
   recorded with all contributing message IDs as evidence.
6. **Resolution.** Look at the last `k` messages in the thread (default `k=3`). If any
   carries `resolution_marker`, or if a `compromise_offer` is followed later by an
   `acknowledgment` from a different participant, **and** no message after that point in
   the thread has tier ≥ 3, the thread's outcome is `resolved`. Where available (JIRA
   issue transitions to `Resolved`/`Fixed`, PR merged), that deterministic, non-classified
   signal may corroborate but never substitutes for the message-level evidence — a merged
   PR with an unresolved hostile subthread is not `resolved` by this definition.
7. **Abandonment.** If the thread is not `resolved`, and the participant who was the
   target (`directed_at`) of the thread's highest-tier message (tier ≥ 2) does not post
   again in that thread, the thread outcome is `abandoned (thread-scoped)`. This is
   upgraded to `abandoned (project-scoped)` — the stronger, more reportable signal — only
   after joining against that participant's project-wide activity: they post nothing else
   in the project (any source) for a configurable window `W` (default 30 days) following
   the thread's last message. Because this check requires waiting out the window, a
   thread's abandonment outcome is provisional (`possible_abandonment`) until `W` has
   elapsed, at which point a nightly job finalizes it to `abandoned` or reverts it to
   `resolved`/`ongoing` if the participant did return. Threads with neither a resolution
   nor an abandonment signal are `indeterminate` — this is an expected, not an error,
   state (many technical threads simply trail off inconclusively without any hostility at
   all; `indeterminate` is not itself a negative signal).
8. **Newcomer treatment.** Not a per-thread boolean. For a rolling reporting window, take
   every message where `directed_at` includes a participant whose `role_at_thread_start`
   (or role at the time of that specific reply, if it changed mid-window) was `newcomer`.
   Report the tier distribution of those messages, and specifically the rate of tier
   ≥2 messages vs. tier ≤ −1 messages, as the two-sided metric defined in §5 — never
   compressed into one score (see the no-composite-score rule in §5.1).

### 2.4 Distinguishing trajectories (the example from research.md)

The two trajectories the research brief calls out —

```
neutral → disagreement → strong disagreement → technical resolution
```

vs.

```
neutral → disagreement → dismissal → personal attack → pile-on → abandonment
```

— are distinguished purely by the tier sequence and the derived events above: the first
is `technical_disagreement` (tier 1) messages climbing at most to `hostility` (tier 3, if
"strong disagreement" reads as heated but not personal) followed by a `resolution_marker`
(tier −2) with no pile-on event; the second is a tier climb through `dismissiveness`
(tier 2) to `personal_attack` (tier 4), a recorded pile-on event, and an `abandoned`
outcome. No part of this distinction requires the classifier to make a holistic judgment
about "how did this thread go" — it falls out of the per-message labels plus §2.3's
deterministic rules.

---

## 3. Disagreement vs. toxicity model

**Sentiment is not health, and intensity is not toxicity.** This is the load-bearing
design constraint from the research brief, and it is grounded in the literature, not just
asserted:

- Miller, Cohen, Klug, Vasilescu & Kästner (2022), *"Did you miss my comment or
  what?": understanding toxicity in open source discussions*, ICSE 2022 (Distinguished
  Paper), found that toxicity in OSS issue discussions looks qualitatively different from
  toxicity on general social platforms — entitlement, dismissiveness of others' effort,
  and demanding/insulting tone toward maintainers dominate, rather than the slurs and
  identity-based abuse that generic toxicity detectors (trained on Wikipedia/Twitter data)
  are tuned to catch. A generic toxicity score would both miss most of what actually hurts
  OSS communities and flag content that isn't toxic in context.
- Raman, Cao, Tsvetkov, Kästner & Vasilescu (2020), *Stress and burnout in open source:
  toward finding, understanding, and mitigating unhealthy interactions*, ICSE-NIER 2020,
  frame the target phenomenon explicitly as "unhealthy interactions" that produce
  stress/burnout — a different, narrower, and more actionable target than "negative
  sentiment," and one this taxonomy tracks directly via `hostility`, `dismissiveness`,
  `gatekeeping`, and the derived `escalation`/`pile-on`/`abandonment` events, rather than
  via a polarity score.
- Ferreira, Cheng & Adams (2021), *"The 'Shut the f**k up' Phenomenon": Characterizing
  Incivility in Open Source Code Review Discussions*, PACMHCI 5(CSCW2), analyzed 1,545
  emails from Linux Kernel Mailing List threads tied to rejected patches and found
  frustration, name-calling, and impatience were the dominant uncivil features — again,
  a small set of concrete, definable behaviors, not a diffuse sentiment gradient, and
  notably arising in exactly the kind of high-stakes technical rejection context Cassandra
  mailing-list and JIRA threads also contain.
- Sarker, Turzo & Bosu (2020), *A Benchmark Study of the Contemporary Toxicity Detectors
  on Software Engineering Interactions*, APSEC 2020, and the follow-on ToxiCR tool
  (Sarker, Turzo, Dong & Bosu, *Automated Identification of Toxic Code Reviews Using
  ToxiCR*, arXiv:2202.13056 / ASE 2023) both make the same point from the tooling side:
  general-purpose classifiers misfire on SE vocabulary in both directions (flagging
  ordinary technical language as toxic, and missing SE-specific put-downs), which is the
  direct justification for domain-specific, operationally defined labels (§1) rather than
  a borrowed sentiment or toxicity API.
- Jongeling, Sarkar, Datta & Serebrenik (2017), *On negative results when using sentiment
  analysis tools for software engineering research*, Empirical Software Engineering, and
  Novielli, Girardi & Lanubile (2018), *A Benchmark Study on Sentiment Analysis for
  Software Engineering Research*, MSR 2018, independently show that different SE-tuned
  sentiment tools disagree with each other often enough to flip a study's conclusions.
  This project does not build a sentiment tool at all — `sentiment_polarity` is recorded
  only as an optional, non-gating observability field (§1.2) and is explicitly barred from
  feeding any published metric, precisely to avoid inheriting this instability.

**Operational consequence.** An intense, high-`technical_disagreement`,
high-`evidence_based_argument` thread that ends in a `resolution_marker` scores *well* on
every metric in §5 — it should. A quiet thread with no disagreement at all but one
`dismissiveness`-tagged reply to a newcomer's question scores worse on
`newcomer_interaction_quality` than the heated thread scores on `escalation_rate`. The
metrics in §5 are built so that argument intensity (tiers 1 and the presence of
`evidence_based_argument`/`constructive_counterargument`) is never, by itself, a negative
input to any published metric. Only tiers 2–4 (non-substantive friction, hostility,
attack) and the derived `pile-on`/`abandonment` events are negative inputs. This is why
§1's definitions insist on distinguishing "disagreement" (tier 1, neutral) from
"dismissiveness" (tier 2, disagreement *without* argument) — the taxonomy is built at the
label level to keep disagreement itself out of the toxicity side of the ledger.

---

## 4. Classifier architecture

### 4.1 Pipeline

```
source messages (mailing list / JIRA / GitHub PR / Slack)
      ↓
normalization  (strip quoting, boilerplate, signatures; content_hash computed; PII beyond
                 what the source already made public is not added; author identity is
                 resolved to participant_ref by the Phase-1 identity pipeline, never
                 passed to the classifier as free text)
      ↓
immutable normalized conversation store
   (Parquet, off-`main`, per D3 — mailing list / JIRA / GitHub PR text; for Slack, Phase 2b
    holds NO raw text here at all — see §4.6 and §7.4)
      ↓
versioned classifier  (pinned model_id + question_set_version; §4.2–4.5)
      ↓
structured classification records  (§4.3, one per message, with full provenance)
      ↓
deterministic thread derivation  (§2.3, versioned separately from the classifier)
      ↓
deterministic aggregation  (§5, versioned metric definitions per D2.1/D3)
      ↓
dashboard + downloadable JSON/CSV per D8, gated by §6/§7
```

The classifier is a pure function: `(normalized_message_text, minimal_parent_context) →
structured_labels`. It never receives author identity, role, organization, or prior
history — that information lives only in the deterministic layers before and after
classification, so it cannot leak into (or bias) the labeling of message content itself.
It also never receives, and never emits, a numeric health/toxicity score — only the
discrete labels in §1.2, each with a raw probability in [0,1] (§4.4/§4.5); present/absent
is a code-side decision applied downstream via the per-label thresholds in §6.4, never
returned by the classifier itself.

### 4.2 Provider-agnostic classifier interface

The classifier is specified as an interface, not tied to a vendor SDK, so that a future
model-version comparison (§6.6) or a provider swap does not require re-architecting the
pipeline:

```
interface CommunityHealthClassifier:
    classifier_version: str          # semver for the (question set + schema + post-processing) bundle
    question_set_version: str        # version of the versioned question-set file specifically (§4.4)
    model_id: str                    # pinned provider model id requested; overwritten per-record with
                                      # whatever model id the provider's response actually reports (§4.4)

    def classify(message: NormalizedMessage,
                 context: ParentContext) -> ClassificationRecord
```

`ClassificationRecord` (§4.3) is the interface's only output type and is provider-neutral
JSON. Any implementation — TypeSafe, a different vendor, or a future fine-tuned
open-weight model — plugs in behind this interface as long as it emits that schema. This
interface did not change when the provider changed from the originally-assumed Anthropic
API to TypeSafe Jev (D17) — only §4.4's implementation behind it did, which is the point
of specifying it as an interface at all. `classifier_version` is bumped on *any* change
that could change output: a new model, a new or edited question in the question set, a
new schema field, a new post-processing rule (like the intensity map in §2.2, though that
is versioned separately since it operates downstream of the classifier on already-
published labels).

### 4.3 Classification record schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "CommunityHealthClassificationRecord",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "record_id", "message_id", "thread_id", "source",
    "classifier_version", "question_set_version", "model_id",
    "input_hash", "classified_at", "labels", "usage"
  ],
  "properties": {
    "record_id":              { "type": "string", "format": "uuid" },
    "message_id":              { "type": "string" },
    "thread_id":                { "type": "string" },
    "source": { "enum": ["mailing_list", "jira_comment", "github_pr_comment", "slack"] },
    "classifier_version":       { "type": "string" },
    "question_set_version":     { "type": "string", "description": "Version of the versioned question-set file (e.g. `questions_v1.yaml`'s `version: 1`) used for this call. D17." },
    "model_id":                 { "type": "string", "description": "The `model` field from the provider's response for this call (e.g. `jev-1.13.0`), not just the pinned value requested -- so a provider-side model change is auditable even under a fixed request." },
    "input_hash":               { "type": "string", "description": "sha256 of the exact normalized text + context window sent to the model" },
    "classified_at":            { "type": "string", "format": "date-time" },
    "usage": {
      "type": "object",
      "description": "Token accounting from the provider's response, tracked against D10's owner-funded monthly cost cap.",
      "additionalProperties": false,
      "required": ["input_tokens", "output_tokens"],
      "properties": {
        "input_tokens":  { "type": "integer", "minimum": 0 },
        "output_tokens": { "type": "integer", "minimum": 0 }
      }
    },
    "labels": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "technical_disagreement":       { "$ref": "#/$defs/label" },
        "constructive_counterargument": { "$ref": "#/$defs/label" },
        "evidence_based_argument":      { "$ref": "#/$defs/label" },
        "compromise_offer":             { "$ref": "#/$defs/label" },
        "acknowledgment":               { "$ref": "#/$defs/label" },
        "personal_attack":              { "$ref": "#/$defs/label" },
        "hostility":                    { "$ref": "#/$defs/label" },
        "dismissiveness":               { "$ref": "#/$defs/label" },
        "sarcasm":                      { "$ref": "#/$defs/label" },
        "gatekeeping":                  { "$ref": "#/$defs/label" },
        "status_authority_invocation":  { "$ref": "#/$defs/label" },
        "resolution_marker":            { "$ref": "#/$defs/label" }
      }
    },
    "tone_intensity": {
      "type": "object",
      "description": "Descriptive-only Score output (questions_v1.yaml's `tone_intensity`). Never a health or toxicity judgment and never feeds a published gate or metric on its own (§5.1's no-composite-score rule) -- the intensity tiers actually used downstream (escalation/de-escalation/pile-on/resolution, §2.2/§2.3) are computed deterministically in code from `labels`, not from this field.",
      "additionalProperties": false,
      "properties": {
        "score":         { "type": "number", "description": "Probability-weighted position across the tone_intensity levels; may land between levels." },
        "confidence":    { "type": "number", "minimum": 0, "maximum": 1 },
        "probabilities": { "type": "object", "description": "Per-level probability distribution, keyed by level index as a string." }
      }
    },
    "sentiment_polarity": {
      "type": "object",
      "description": "Optional, observability-only. Never used in any gate or published metric.",
      "properties": {
        "value": { "enum": ["negative", "neutral", "positive", "mixed"] },
        "confidence": { "type": "number", "minimum": 0, "maximum": 1 }
      }
    },
    "human_reviewed":  { "type": "boolean", "default": false },
    "human_label_id":  { "type": "string", "description": "Set only if this record is (or has been superseded by) a benchmark-corpus human label; see §6." },
    "superseded_by":   { "type": "string", "description": "record_id of a correction, if any (§7.6). The original record is never deleted." }
  },
  "$defs": {
    "label": {
      "type": "object",
      "additionalProperties": false,
      "required": ["probability"],
      "properties": {
        "probability": { "type": "number", "minimum": 0, "maximum": 1, "description": "Raw Noul probability from the classifier, stored as-is. Thresholds (per label, per §6.4's gate groups) are applied in code, never baked into this record -- D17: re-thresholding after calibration against the benchmark (issue #47) therefore never requires re-inference." }
      }
    }
  }
}
```

Notes:

- The model is instructed and schema-constrained to emit **only** this structure — no
  free-text project judgment, no numeric health/toxicity score, no recommendation. This
  directly implements the research brief's instruction to "prefer structured
  classification over asking an LLM to produce arbitrary numerical toxicity scores."
- `labels.<name>` is a single `probability` in [0,1], not a `{present, confidence}` pair.
  This is a direct consequence of D17: each message-level label is asked as a Noul, whose
  single returned number "is the answer and the certainty in one" (TypeSafe's own framing
  of the primitive) — there is no separate confidence to carry alongside it. `present`/
  `absent` is a code-side decision made by applying the label's threshold (§6.4, currently
  `null` pending calibration in #47), not a field this record stores.
- `input_hash`, not the raw text, is what gets committed to the data cache for
  provenance. For mailing list / JIRA / GitHub PR sources, the raw text is separately
  cached under D3's normal rules (it's already public, archived data). For Slack, no raw
  text is retained anywhere, including here — `input_hash` on a Slack record is still
  safe to keep (a hash does not reveal content) and is useful for idempotent
  re-classification, but nothing lets that hash be turned back into the message (§7.4).
- Records are immutable and additive. A correction (§7.6) is a new record with
  `human_reviewed: true` and a `superseded_by` back-reference on the original — nothing is
  ever deleted or edited in place, matching D2.6/D3's "nothing changes silently" rule.

### 4.4 Jev-specific implementation notes (one concrete implementation behind the interface)

The reference implementation targets TypeSafe's Jev System One model via the
`typesafe-sdk` Python package (D17), replacing the Anthropic-based design this
subsection previously described. Everything below is implementation detail behind the
interface in §4.2 — swapping providers again would change only this subsection.

- **Pinned model, response-reported model recorded.** Requests pin a versioned model id
  (`jev-1.13.0`), never `jev-latest`. The `model` field TypeSafe's response actually
  reports is what gets stored as `model_id` on every classification record (§4.3) — so a
  provider-side model change is auditable even though the request asked for a fixed
  version. A model change is a `classifier_version` bump, validated against the frozen
  benchmark before use (D2.6, §6.6).
- **One `system_one` call per message, all questions together.** Each call's state is the
  narrow, named-field object §4.1 already requires (`message.text`, `parent.text`,
  `message.source` — see `questions_v1.yaml`'s `state_schema`), and the *questions* are
  the full versioned set from `src/project_health/classify/questions_v1.yaml`: one Noul
  per §1.2 message-level label, plus one `tone_intensity` Score. All 13 are asked in a
  single request per message, not one request per question, per TypeSafe's own guidance
  that independent questions over the same state "run in parallel and cannot see one
  another's answers," and that asking related Nouls together in one request is the
  documented pattern for checklist-style evaluations (`primitives/noul`,
  `concepts/how-to-build-with-system-one`).
- **Structured output by construction, not by prompting.** Noul and Score are typed
  primitives with a fixed response shape (`{"noul": <probability>}` /
  `{"score", "probabilities", "confidence"}`) — there is no free-text parsing step and no
  risk of a malformed label object, which is what §4.3's schema-constrained design has
  always required; TypeSafe's primitives satisfy it structurally rather than via a
  provider-specific JSON-schema/tool-use flag.
- **No documented provider batch-job API (unlike the prior Anthropic design).**
  TypeSafe's documented interface is a synchronous `system_one` request over one state;
  as of the docs read for this design there is no message-batching endpoint comparable to
  the Anthropic Message Batches API the previous version of this section relied on.
  Nightly/backfill runs instead achieve throughput with many concurrent `system_one`
  calls via the SDK's async client, bounded by a concurrency limit and the same
  retry/backoff policy as the other collectors (`ARCHITECTURE.md` §7.2). There is
  therefore no `batch_id` field in §4.3's schema; if TypeSafe later documents a batch
  endpoint, this subsection and §4.3 should be revisited together.
- **Context window per call, unchanged in spirit.** Each call's state still carries one
  message plus, where useful for disambiguating pronouns/references, its immediate
  parent message — never the full thread — keeping per-call state small and the
  classifier's job narrow (per-message labeling, not thread-level judgment, per §1.3).
  This is now doubly motivated: it was already this document's design (bias avoidance,
  cost) and it is also TypeSafe's own documented mitigation for large-irrelevant-context
  jaggedness in Jev (`model-jaggedness/jev-1.13`: "unrelated detail acts as a distractor,
  and a large state makes it harder to tell which part of the input produced a wrong
  answer... [f]ilter data before passing it to the model").
- **No author identity in state.** As in §4.1, the classifier never sees who wrote a
  message, their role, or their history — the same field-narrowing that avoids bias also
  removes a distractor field per the jaggedness guidance just cited.
- **Adversarial-text mitigation is the question design itself.** TypeSafe documents that
  Jev "doesn't treat data as potentially hostile by default" and recommends "explicit
  criteria and thoroughly test[ing] your integration" (`model-jaggedness/jev-1.13`) as the
  mitigation. `questions_v1.yaml` follows this directly: every label's `criteria.false`
  spells out the boundary against its nearest confusable sibling (e.g.
  `dismissiveness` vs. a terse-but-reasoned "no"; `personal_attack` vs. blunt criticism of
  code; `hostility` vs. firm disagreement) rather than leaving the model to infer intent
  from tone alone, and §6's benchmark/gate process is what actually tests it before any
  label publishes.

### 4.5 Confidence, probability, and abstention

Noul and Score report uncertainty differently, and this document's design leans on both:

- **Each message-level label (a Noul) returns one probability**, not a separate
  `{present, confidence}` pair. As TypeSafe's own documentation puts it, the returned
  number "is the answer and the certainty in one" — a value near 0.5 means the model
  found the label about as likely present as absent, not "medium intensity." The
  question set is written so that genuinely ambiguous cases (§1.4's borderline examples —
  a bare "Noted.", an authority mention that also carries an argument) are instructed to
  land near 0.5 rather than being forced toward a confident 0 or 1.
- **`present`/`absent` is a code-side decision**, made by applying each label's threshold
  from §6.4's gate groups to the stored `probability` — never baked into the
  classification record itself. Every label's threshold is `null` in `questions_v1.yaml`
  v1 pending calibration against the frozen benchmark (issue #47); re-thresholding after
  calibration is therefore a pure code change, never a re-inference.
- **`tone_intensity` (a Score) additionally reports a `confidence`** derived from how
  concentrated its level-probability distribution is — concentrated means confident,
  spread out means uncertain (`confidence`) — but per §4.4/this field's own schema note
  (§4.3), it is descriptive only and is never used as a per-record filtering threshold on
  the public dashboard, for the same "an unaudited, un-versioned cutoff" reason the
  original design gave: if a confidence threshold is adopted later for any field, it
  becomes part of `classifier_version` like everything else.
- Aggregation (§5) treats a label whose thresholded decision is "present" as the raw
  count for prevalence purposes; §6's precision/recall gates are what actually bound how
  much a given threshold's true positives can be trusted.

### 4.6 Slack (Phase 2b) pipeline variant

Phase 2b's pipeline differs only in what persists:

```
Slack message (fetched in-run via a read-only bot, ASF Infra-approved, per D1)
      ↓
normalization (in-memory / ephemeral store only)
      ↓
classifier call  (same interface, same schema — model never sees author identity here either)
      ↓
classification record persisted  (labels + provenance, no message_id that maps back to a
                                   retrievable Slack permalink, no channel-message pointer
                                   beyond the channel name itself)
      ↓
raw text discarded immediately after classification — never written to disk, never
committed to any branch or Parquet cache
      ↓
same deterministic thread derivation (§2.3) and aggregation (§5), with the Slack-specific
   minimum-N and no-quotation rules from §7 applied at aggregation time
```

`content_hash`/`input_hash` may still be kept even for Slack records (a hash is not
reversible to content) for run idempotency, but no other pointer back to the original
Slack message is retained, so that even someone with our data store and no Slack access
cannot reconstruct which specific message a label was about.

---

## 5. Aggregate metrics

### 5.1 Ground rules

- Every metric below is `classified` tier and must display that tier wherever shown (D2.1).
- **No composite scores.** Per D2.7 and the research brief's evidence-before-scores
  principle, this document does not propose combining any of these into a single
  "interaction health" or "disagreement safety" number. Each metric is reported on its
  own, with its own trend line against the project's own history (D2.2), its own
  definition, population, and window shown alongside it (D2.3).
- **Minimum sample sizes.** Every metric below requires both (a) a minimum denominator
  count and (b) a minimum number of **distinct contributing individuals**, in the
  denominator, for the *current reporting period*, before it renders as a number; below
  either threshold it renders as `insufficient data` (D2.5) and the pipeline backs off to
  a wider window (month → quarter → half-year) before giving up and staying
  `insufficient data`. Defaults proposed below (30 messages/threads, 5 distinct
  individuals) are starting points to be tuned once real Cassandra volumes are visible in
  the Phase 1 pilot — see the open questions at the end of this document. The
  distinct-individuals floor exists specifically to prevent small-population
  deanonymization (§7.2), which a message-count floor alone does not prevent.

### 5.2 Formulas

Let *period* be a reporting window (default: calendar month, per D5's monthly-edition
cadence; nightly dashboard values use a trailing window, e.g. trailing 90 days, to avoid
showing a near-empty in-progress month).

| Metric | Formula | Minimum sample | Notes |
|---|---|---|---|
| **Escalation rate** | (# threads in *period* with ≥1 `escalation` event) / (# threads in *period* with ≥1 `technical_disagreement` message) | 30 qualifying threads, 5 distinct participants across them | Denominator restricted to threads that actually contained disagreement, so the rate isn't diluted by the many threads with no conflict at all. |
| **Constructive resolution rate** | (# threads in *period* with outcome `resolved`) / (# threads in *period* with ≥1 message at tier ≥1, i.e. any disagreement or friction) | 30 qualifying threads, 5 distinct participants | Complementary to escalation rate; a thread can score well on both (heated but resolved) or poorly on both (heated and abandoned). |
| **Dismissive interactions per 1,000 messages** | (# messages in *period* with `dismissiveness: true`) / (total classified messages in *period*) × 1000 | 1,000 total classified messages, 10 distinct authors | Modeled directly on the "per 1,000 messages" framing in research.md's ethics section. |
| **Gatekeeping rate** | (# messages with `gatekeeping: true`) / (total classified messages) × 1000 | Same as above | |
| **Authority-invocation rate** | (# messages with `status_authority_invocation: true`) / (total classified messages) × 1000 | Same as above | Reported, not judged — invoking authority is sometimes appropriate (a maintainer stating a final call after a full discussion); the metric tracks frequency and trend, not a verdict. |
| **Pile-on rate** | (# pile-on events in *period*) / (# threads in *period*) × 100 | 30 threads, 5 distinct targets (i.e., not dominated by repeated pile-ons on one person) | The distinct-targets condition doubles as a weaponization guard (§7.5). |
| **Personal-attack rate** | (# messages with `personal_attack: true`) / (total classified messages) × 1000 | 1,000 total classified messages, 10 distinct authors *and* 10 distinct targets | Highest-stakes metric; also the one with the strictest precision gate (§6.4). Never broken down below this level (no per-thread, per-list-section, or per-org drill-down finer than the monthly project-wide number, unless a finer bucket still clears the same N/distinct-individual floor). |
| **Thread abandonment rate (post-friction)** | (# threads in *period* with outcome `abandoned`, restricted to threads that reached tier ≥2 at some point) / (# threads in *period* that reached tier ≥2) | 30 qualifying threads, 5 distinct abandoning participants | Restricted to threads with friction so it measures "abandonment following friction," not baseline no-reply abandonment (which is a Phase 1 deterministic participation metric, not a communication-health one — see `METRICS.md`). |
| **Newcomer constructive-response rate** | (# messages directed at a newcomer with tier ≤ −1, i.e. `acknowledgment`/`compromise_offer`/`resolution_marker`) / (# messages directed at a newcomer, total) | 30 newcomer-directed messages, 5 distinct newcomers | Reported *alongside*, never combined with, the next row. |
| **Newcomer dismissive/hostile-response rate** | (# messages directed at a newcomer with tier ≥2) / (# messages directed at a newcomer, total) | Same as above | Deliberately reported as a separate number from the constructive rate rather than netted into one score (§5.1's no-composite rule) — a project can simultaneously have high constructive engagement with newcomers *and* a nonzero hostile-response tail, and collapsing that into one figure would hide the tail. |

---

## 6. Validation strategy

No label defined in §1 may appear on the public dashboard until it passes every gate in
this section. A label that has not yet passed shows as "not yet validated" in any UI that
would otherwise display it — it is never silently omitted (that would be indistinguishable
from "this project has zero of this behavior," which is a claim, not an absence of one).

### 6.1 Frozen benchmark corpus

- **Size.** A target of 3,000–5,000 human-labeled messages, assembled in two strata that
  are kept separate and never merged for prevalence estimation:
  - **Prevalence stratum** (~2,000 messages): randomly sampled, stratified by source
    (mailing list / JIRA / GitHub PR) in proportion to each source's share of total
    message volume, and by time period across the available history, so that base rates
    measured on it reflect the actual corpus, not a curated one.
  - **Rare-class enrichment stratum** (~1,000–3,000 messages, sized per label as needed):
    `personal_attack`, `gatekeeping`, `status_authority_invocation`, and `pile-on`-eligible
    threads are expected to be rare (this is consistent with the low base rates for
    severe incivility reported by Ferreira et al. and Miller et al.). A lightweight
    keyword/heuristic pre-filter (e.g., a regex/embedding-similarity candidate net, not
    the production classifier itself) surfaces candidate messages for human review so that
    raters see enough positive examples of rare labels to compute meaningful
    precision/recall. This stratum is used **only** to compute per-label precision/recall
    (§6.4), **never** to estimate how common a label is in the wild — that estimate comes
    only from the prevalence stratum. Mixing the two would bias prevalence upward for
    exactly the labels most likely to cause reputational harm if overstated.
- **Freezing.** Once assembled and adjudicated, the corpus is frozen (versioned,
  checksummed) and never silently modified. Corrections from the appeals process (§7.6)
  are appended as a new corpus version, not edited into the old one, so that historical
  gate results remain reproducible against the corpus version that produced them.

### 6.2 Raters

- **At least 3 independent human raters** per item, drawn from people with open-source
  code-review or mailing-list moderation experience. At least one rater pool member should
  have no current Cassandra PMC/committer affiliation, specifically to check whether
  in-group familiarity changes how borderline cases (sarcasm, terse-but-justified "no"s,
  authority invocation in legitimate final-call situations) get labeled.
- **Odd-numbered panels** to avoid unresolved ties on a given label's presence; a fourth
  "adjudicator" rater resolves any case where all three initial raters disagree, and that
  adjudication is recorded with a rationale, not silently taken as ground truth.
- Rater instructions are the §1.2 definitions and §1.4 worked examples verbatim, so that
  the benchmark measures agreement on *this taxonomy*, not on each rater's private notion
  of "toxic."

### 6.3 Inter-rater agreement

- **Krippendorff's alpha**, computed per label (each label is its own binary reliability
  problem, since labels are not mutually exclusive), on the full rater panel.
- **Gate:** α ≥ 0.667 is the minimum to treat a label's human annotations as usable ground
  truth for precision/recall testing at all ("tentative conclusions" territory);
  α ≥ 0.80 is the target before a label's aggregate metric is allowed to publish
  unqualified. A label stuck below 0.667 after a full annotation round is treated as a
  **taxonomy problem, not a rater problem** — its definition in §1.2 gets revised (tighter
  boundary, better examples) and re-benchmarked, rather than being pushed forward with
  weak ground truth.
- Cohen's kappa is reported pairwise between rater pairs as a supplementary diagnostic
  (useful for spotting one consistently-outlier rater), but Krippendorff's alpha over the
  full panel is the gating statistic since it handles the 3+ rater, some-missing-labels
  case cleanly.

### 6.4 Precision / recall / F1 gates

Evaluated on a held-out split of the frozen benchmark that the classifier prompt was never
tuned against (a train/tune split is kept separate from the gating split, refreshed each
time the benchmark corpus version changes).

| `label_group` id (`questions_v1.yaml`) | Label group | Precision floor | Recall floor | F1 floor | Rationale |
|---|---|---|---|---|---|
| `reputational_harm` | `personal_attack`, `hostility`, `gatekeeping` | **≥ 0.85** | ≥ 0.60 | ≥ 0.70 | These are the labels most capable of causing reputational harm in aggregate if over-triggered; precision is weighted above recall — an undercount that misses some real instances is a smaller harm than a label pattern that manufactures false ones. |
| `friction` | `dismissiveness`, `sarcasm`, `status_authority_invocation` | ≥ 0.80 | ≥ 0.60 | ≥ 0.70 | Same asymmetric reasoning, slightly relaxed since these are lower-stakes than a direct attack/hostility/gatekeeping call. |
| `argument` | `technical_disagreement`, `constructive_counterargument`, `evidence_based_argument`, `compromise_offer`, `acknowledgment`, `resolution_marker` | ≥ 0.75 | ≥ 0.75 | ≥ 0.75 | Positive/neutral labels: balanced gate, no asymmetric penalty needed since false positives here aren't reputationally loaded. |

Each message-level label's `label_group` in `src/project_health/classify/questions_v1.yaml`
(D17, issue #42) is one of these three ids, so the calibration work in #47 can join the
YAML directly back to the gate it must clear.

A label that fails its gate does not publish, full stop — it is not published "with a
caveat" or at reduced confidence. §4's structured-output design and §6.1's frozen
corpus exist specifically so this is a testable yes/no per label, not a judgment call at
launch time.

### 6.5 Calibration

Reliability diagrams (predicted confidence bucket vs. empirical precision within that
bucket) are produced per label against the held-out benchmark split. Confidence is **not**
currently used as a publication-time filtering threshold (§4.5) — calibration here is
diagnostic, to catch a classifier that is systematically over- or under-confident, which
would be a signal to revisit the prompt even for a label that otherwise clears its
precision/recall gate.

### 6.6 Drift monitoring and model-version comparison

- **Drift monitoring.** Each nightly/monthly run, the currently-published
  `classifier_version` is also re-run against the full frozen benchmark (or a rotating
  subset if cost requires it) as a canary, independent of production classification
  traffic. If any published label's measured precision/recall/F1 on that canary run drops
  below its §6.4 gate, that is an alert, and the affected label is unpublished
  (reverted to "not yet validated") until investigated — this catches provider-side model
  behavior drift even with no code change on this project's side.
- **Model-version comparison.** Before any new `classifier_version` (new model, new
  prompt, new schema) is promoted to production, it is run against the **full** frozen
  benchmark side-by-side with the currently-published version. Promotion requires: (a)
  every label still clears its §6.4 gate, and (b) no label's F1 regresses by more than a
  0.03 tolerance relative to the version being replaced without an explicit, logged
  sign-off explaining why the regression is acceptable (e.g., a large cost or capability
  gain elsewhere).
- **Never silently rewrite history (D2.6).** Classification records are immutable and
  keyed by `(message_id, classifier_version)` — promoting a new version does not delete or
  overwrite prior-version records. Promotion triggers a full-history recompute under the
  new version (re-classifying the entire historical corpus, not just new messages going
  forward) so that the "current" view is consistent across all history, exactly as D3
  requires for deterministic metric-definition changes — and this recompute, plus the
  before/after benchmark scores, is written to a machine-readable classifier changelog
  (e.g. `classifier-changelog.json`) that the dashboard links from every classified
  metric.

---

## 7. Ethics and responsible reporting

### 7.1 Purpose statement (governs every rule below)

Per the research brief: the purpose of this system is to understand **community systems
and interactions**, not to produce public rankings of "toxic people." Every rule in this
section exists to make that true in practice, not just in intent. No label in §1
describes a trait of a person; each describes a pattern present in one message. The site's
copy, chart titles, and documentation must preserve that framing everywhere — "hostility
rate" describes a rate of messages, never "how hostile [person] is."

### 7.2 Minimum sample sizes and small-population protection

- Every published aggregate obeys the minimum-N *and* minimum-distinct-individuals floor
  from §5.1/§5.2. The distinct-individuals floor exists because raw message-count floors
  alone can still leak identity by elimination on a small list (a floor of 30 messages
  from only 2 people on a low-traffic list is not anonymous).
- No breakdown finer than the published project-wide monthly/quarterly number is offered
  unless that finer bucket **independently** clears the same floors — e.g., there is no
  by-organization or by-mailing-list-thread-topic drill-down for any classified metric
  unless each such bucket has ≥5 distinct individuals in it, mirroring the org-concentration
  privacy rule already set for affiliation data in D6.

### 7.3 Anonymization and attribution

- **No per-person scores, ever (D2.4).** Nothing in this document's schema, metrics, or
  proposed UI computes, stores, or displays a per-`participant_ref` count of any label.
  Aggregation is defined in §5 exclusively at the project/period level.
- **No deep links from aggregates to raw messages**, even though mailing list, JIRA, and
  GitHub PR content is already public. Making it one click from "dismissive interactions
  per 1,000 messages, September 2026" to a specific flagged email would re-introduce
  individual targeting even without formally violating "no attribution," so the dashboard
  does not provide that link. Anyone who wants to inspect the public archive can do so
  independently, through the archive's own search — this project does not build the index
  that makes a negative label trivially attributable to a person.

### 7.4 Quotations, and public vs. semi-public data

- **Default: no quotations.** The public dashboard and monthly reports never display
  verbatim text from a classified message.
- Mailing lists, JIRA comments, and GitHub PR comments are public ASF/GitHub archives —
  Phase 2a treats them as public data, consistent with D1/D7, and their normalized text
  may be cached (§4.1) subject to the takedown provision in §7.7.
- Slack is semi-public (member-gated visibility, different norms of expected ephemerality
  than a public mailing-list archive) and is held to a strictly stricter standard per D1
  and §4.6: no raw text ever persisted, in-run classification only, aggregate counts only,
  no quotes, no links, no individual attribution — and additionally, per §4.6, no
  message-level pointer back to the original Slack message is retained at all, beyond a
  non-reversible content hash used only for run idempotency.
- An internal-only review surface (accessible to the small set of people running §6's
  validation and §7.6's appeals process, not published) may show verbatim text **only**
  for mailing-list/JIRA/GitHub sources where it is already public archive content, and
  only for calibration/dispute-review purposes — never for Slack, and never republished.

### 7.5 False positives and weaponization resistance

Concrete, enumerated mechanisms (not aspirations):

1. No per-person data anywhere in the schema or output, at any aggregation tier (§7.3).
2. Minimum-N and minimum-distinct-individuals gating on every published number (§7.2).
3. No quotations by default (§7.4).
4. No deep links from aggregate metrics to individual raw messages (§7.3).
5. Monthly/nightly-trend cadence, not a live "who said what today" feed — the design in
   §5 only ever produces period aggregates and their trend against the project's own
   history (D2.2), never a real-time individual-event stream.
6. Asymmetric, precision-weighted gates specifically on the reputationally loaded labels
   (§6.4), so the labels most capable of causing harm if wrong are held to the highest bar
   before they can publish at all.
7. A pile-on rate denominator requiring ≥5 **distinct targets** (§5.2), so a metric cannot
   be dominated by, and therefore cannot be read as being about, one specific person.
8. An appeals/corrections process (§7.6) that can pull a mislabel out of the aggregate.
9. PMC governance sign-off required before either Phase 2a or Phase 2b publishes anything
   (§7.8).
10. An explicit, standing non-goal (D2.7, restated here): this system will never rank
    people, produce a single headline health number, or make a causal claim about a
    specific individual's departure or behavior (see also §8).

### 7.6 Appeals and corrections

- Any contributor, committer, or PMC member may request review of a specific thread's
  contribution to a published aggregate (there is nothing to "take down" for an
  individual, since no output names one — a request is about whether the *aggregate* is
  right, e.g. "message X was labeled `personal_attack` and I believe that's wrong").
- A maintainer of this project reviews the classifier's stored rationale (if retained
  internally; see §4.5) and the message in its source archive, and may issue a
  `human_reviewed: true` correction record (§4.3's `superseded_by` mechanism) —
  immutable append, not edit-in-place.
- A correction triggers recompute of every aggregate the corrected record fed into.
- **Frozen monthly reports (D5) are not silently edited retroactively.** A correction
  affecting a value already published in a frozen monthly report produces a linked
  erratum/correction note attached to that historical report; the *live* nightly
  dashboard reflects the corrected value going forward. This mirrors D2.6's "nothing
  changes silently" rule applied specifically to the monthly-freeze cadence in D5.
- Corrections accumulate into future revisions of the frozen benchmark corpus (§6.1),
  so recurring disputed patterns actually improve future classifier versions rather than
  being handled as one-off overrides forever.

### 7.7 Data retention

- Mailing list / JIRA / GitHub PR normalized text: retained indefinitely in the off-`main`
  Parquet cache (D3), since it mirrors already-public archives. If the ASF or GitHub
  deletes or redacts a source message (moderation action, legal request), this project
  honors that by removing the corresponding cached text and any classification record
  derived from it on request — a delete-on-upstream-takedown mechanism, even though the
  data is technically re-derivable from other public mirrors, to avoid this project being
  a stale copy that outlives a legitimate takedown.
- Slack: zero raw-text retention, ever (§4.6, §7.4). Aggregate counters and provenance
  metadata derived from Slack are retained indefinitely, since they carry no reconstructable
  content.
- Classification records (all sources) are retained indefinitely as the auditable
  provenance trail for every published number (D2.3), superseded-not-deleted per §7.6.

### 7.8 Code of Conduct and engaging the Cassandra PMC before publishing Phase 2

- This system operates entirely within spaces the ASF Code of Conduct already covers —
  mailing lists, issue trackers, and (Phase 2b) Slack are explicitly in its scope
  (apache.org/foundation/policies/conduct.html). Classification of community
  communication is a natural extension of that governance context, not a separate regime,
  and should be presented to the PMC as such.
- **Before Phase 2a publishes anything:** present this document's methodology and §6's
  validated benchmark results to `dev@cassandra.apache.org` for discussion; give the PMC a
  private preview of the dashboard before public launch; seek explicit PMC acknowledgment
  (lazy consensus is the ASF norm, but given the sensitivity here, an affirmative response
  from PMC members, not just silence, is the bar this project sets for itself) before the
  first classified metric goes live publicly. Record who was consulted and when in the
  classifier changelog (§6.6) as a governance log entry, not just a technical one.
- **Before Phase 2b (Slack) publishes anything:** D1's two hard blockers apply literally
  and are treated as blockers, not targets — explicit PMC consensus on `dev@`, and ASF
  Infra's approval of a read-only bot on the specific public channels in question. Neither
  is assumed; both must be documented as obtained before any Slack-derived aggregate
  appears anywhere, including a private preview.
- The PMC (or its designee) should be offered a standing channel to request a review under
  §7.6 or to request that a specific metric be paused pending investigation — this project
  does not treat PMC oversight as a one-time launch gate but as ongoing.

---

## 8. Correlation vs. causation guidance for funnel analyses

The research brief's example — `P(return | constructive interaction)` vs. `P(return |
hostile interaction)` — is exactly the kind of analysis this section constrains. This
guidance applies to any funnel-style analysis that joins §5's classified interaction data
against the Phase 1 contributor funnel (`METRICS.md`).

### 8.1 Why these numbers are associational, not causal, by construction

- **Self-selection / reverse causality.** A contributor who already intends to keep
  contributing may both behave more diplomatically under disagreement and be more likely
  to receive constructive replies (established contributors get more benefit of the
  doubt); the "constructive interaction" may be a *symptom* of an already-forming
  intent to stay, not its cause.
- **Confounding by topic.** Architecturally contentious topics (e.g., a storage-engine
  redesign) may structurally produce both more friction *and* lower resolution rates
  regardless of anyone's tone, while simultaneously being topics where return behavior
  differs for unrelated reasons (harder topics attract more committed contributors).
  Comparing return rates across interaction types without controlling for topic difficulty
  conflates the two.
- **Confounding by tenure/reputation.** More senior contributors are treated more
  constructively on average *and* are more likely to return regardless of any single
  thread's tone — tenure is a common cause of both the treatment and the outcome, which
  is the textbook confounding structure this kind of number is vulnerable to.
- **Survivorship bias.** A classifier that only sees text a person actually posted cannot
  see a newcomer who never posted a follow-up at all after a bad first experience with a
  *deterministic* (not communication-classified) signal like a long response latency or a
  closed-without-comment PR — a pure text classifier will systematically miss the most
  discouraged newcomers, who leave no thread for the classifier to score. Any funnel
  analysis using §5's data must be explicitly scoped as "among contributors who had at
  least one classified interaction" and must say so, rather than implying it covers all
  attrition.

### 8.2 Rules for presenting any such analysis

1. Any funnel number of this shape is tier `experimental` at best (D2.1), reported with
   confidence intervals, and always labeled as an association, with adjacent text stating
   plainly that it is not evidence of a causal effect.
2. Comparisons are stratified at minimum by contributor tenure/prior-contribution-count
   before comparing return rates across interaction types, to blunt the most obvious
   tenure confound; topic-matched or cohort-matched comparisons are a further refinement
   to pursue once volume supports it, not a Phase 2 requirement.
3. The same minimum-N and minimum-distinct-individuals floors from §5.1/§7.2 apply to
   every cell of any such comparison.
4. **Never** construct a per-person "risk of leaving" figure from this kind of analysis —
   doing so would combine two things this project rules out independently: a per-person
   score (D2.4/§7.3) and a causal claim about an individual (this section). This
   combination is explicitly named as a non-goal.
5. Headline copy anywhere on the site may not state a funnel association as "X% more
   likely to return" without the accompanying interval and caveat in the same visual
   unit (not a footnote reachable only by clicking through) — the goal is that the
   association and its limits are seen together or not at all.
6. Any such analysis is a hypothesis-generating input to PMC/community discussion, never a
   basis for action against, or public characterization of, any specific individual or
   thread.

---

## 9. Non-goals (restated for this document specifically)

Carried forward from D2.7 and made explicit for communication analysis:

- No per-person toxicity, hostility, or "difficult to work with" score, ever, at any tier
  of aggregation.
- No single composite "interaction health" number (§5.1).
- No causal claims connecting a specific interaction to a specific person's departure or
  behavior (§8).
- No public "toxic people" ranking or list, and no site copy that describes a *person* as
  hostile/dismissive/toxic rather than describing a *rate of messages*.

---

## Open questions for the project owner / PMC

1. **Numeric defaults.** The minimum-N (30 messages/threads), minimum-distinct-individuals
   (5–10), and abandonment window (`W = 30 days`) values in §5/§7 are reasonable starting
   points, not derived from Cassandra-specific volume data (which requires the Phase 1
   pilot to observe). They should be revisited once real `dev@`/JIRA/PR volumes are
   measured.
2. **Rater sourcing.** §6.2 proposes at least one rater pool member with no current
   Cassandra PMC/committer affiliation, to check for in-group labeling bias. Recruiting
   qualified outside raters (OSS code-review/moderation experience) is a logistics
   question this document doesn't resolve — worth deciding before Phase 2a benchmark work
   starts, since it is on the critical path.
3. **Resolution corroboration signals.** §2.3 allows JIRA-status/PR-merge signals to
   *corroborate* (never substitute for) a message-level `resolved` determination. Whether
   to wire that corroboration in for the Phase 2a launch or defer it to a later iteration
   is an implementation-sequencing call, not a design one.
4. **Rare-class pre-filter risk.** §6.1's keyword/heuristic pre-filter for the enrichment
   stratum should itself be reviewed before use, since a poorly chosen filter could bias
   *which* rare instances raters ever see (though not the prevalence estimate, which is
   kept in the separate stratum specifically to avoid this).
5. **PMC engagement timing.** §7.8 recommends presenting methodology and benchmark results
   to `dev@` before Phase 2a launch, as a stronger bar than the D1-mandated Phase 2b
   consensus. Confirm the PMC is comfortable being approached this early, versus after a
   working prototype exists.

---

## References

- Raman, N., Cao, M., Tsvetkov, Y., Kästner, C., & Vasilescu, B. (2020). *Stress and
  burnout in open source: toward finding, understanding, and mitigating unhealthy
  interactions.* ICSE-NIER 2020. https://dl.acm.org/doi/abs/10.1145/3377816.3381732
- Miller, C., Cohen, S., Klug, D., Vasilescu, B., & Kästner, C. (2022). *"Did you miss my
  comment or what?": understanding toxicity in open source discussions.* ICSE 2022
  (Distinguished Paper). https://dl.acm.org/doi/10.1145/3510003.3510111
- Ferreira, I., Cheng, J., & Adams, B. (2021). *The "Shut the f**k up" Phenomenon:
  Characterizing Incivility in Open Source Code Review Discussions.* Proceedings of the
  ACM on Human-Computer Interaction, 5(CSCW2), Article 353. Preprint:
  https://arxiv.org/abs/2108.09905
- Sarker, J., Turzo, A. K., & Bosu, A. (2020). *A Benchmark Study of the Contemporary
  Toxicity Detectors on Software Engineering Interactions.* APSEC 2020, pp. 218–227.
  https://www.researchgate.net/publication/344418180
- Sarker, J., Turzo, A. K., Dong, M., & Bosu, A. *Automated Identification of Toxic Code
  Reviews Using ToxiCR.* arXiv:2202.13056. https://arxiv.org/abs/2202.13056 — tool and
  dataset: https://github.com/WSU-SEAL/ToxiCR
- Jongeling, R., Sarkar, P., Datta, S., & Serebrenik, A. (2017). *On negative results when
  using sentiment analysis tools for software engineering research.* Empirical Software
  Engineering. https://link.springer.com/article/10.1007/s10664-016-9493-x
- Novielli, N., Girardi, D., & Lanubile, F. (2018). *A Benchmark Study on Sentiment
  Analysis for Software Engineering Research.* MSR 2018. https://arxiv.org/abs/1803.06525
- Steinmacher, I., Conte, T., Gerosa, M. A., & Redmiles, D. (2014). *Barriers Faced by
  Newcomers to Open Source Projects: A Systematic Review.* OSS 2014.
  https://www.ime.usp.br/~gerosa/papers/Steinmacher2014_Chapter_BarriersFacedByNewcomersToOpen.pdf —
  cited for the newcomer-treatment framing in §1/§5; a fuller newcomer-research review
  belongs in `docs/RESEARCH.md` and is not duplicated here.
- CHAOSS Project (Linux Foundation). Community Health Analytics in Open Source Software.
  https://chaoss.community/ — cited for the general practice of aggregate,
  interaction-level community metrics; CHAOSS's specific metric definitions are reviewed
  in `docs/RESEARCH.md`, not here.
- Apache Software Foundation. *Code of Conduct.*
  https://www.apache.org/foundation/policies/conduct.html

**Flagged as unverified / needs confirmation before relying on it:** none of the citations
above were fabricated — each was confirmed against a live search result with a URL/DOI
during the writing of this document. If any citation above is later found to not match
what it's cited for on closer reading of the full paper (as opposed to the abstract/search
summary used here), that should be corrected before Phase 2a's methodology is presented to
the PMC per §7.8.
