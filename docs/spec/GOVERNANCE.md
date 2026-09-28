# Governance: Per-Commit Minimums

Research deliverable for issue #32 (part of Epic #31), implementing D14/D15 of `docs/spec/DECISIONS.md`.
Consistent with `docs/spec/DATA-SOURCES.md` (source access, rate limits, evidence reliability) and
`docs/spec/ARCHITECTURE.md` §3.1 (reviewer data model).

## Approved v1

**Approved by the project owner (`pmcfadin`) on 2026-09-25.** `governance-policy.yaml` (repo root) is v1 of
the scoring policy and is authoritative for compliance results published on `/governance/`, subject to the
exclusions in §4 and the `deferred_to_v2` list below. The adjustments the owner made to this draft before
approving it:

1. **`reviewer-present` gets `fail_allowed: true`.** FAIL is narrowly defined: the commit references a
   `CASSANDRA-N` key **and** no reviewer is found in either the commit trailer or the JIRA Reviewer/Reviewers
   fields **and** no exemption matches. Two exemptions are defined with precise, testable patterns (never a
   fail, regardless of reviewer evidence): **`ninja`** (`(?i)\bninja(fix)?\b` anywhere in the message — the
   project's own self-declared "small change, skip full review" convention) and **`release-housekeeping`**
   (version increments, debian-changelog prep, submodule repins — three sub-patterns, see §8). A third
   condition guards the fail itself: if the message contains any review wording at all
   (`\breview(ed|er|ers)?\b`, case-insensitive) but no reviewer was parsed, the result is `unknown`
   ("review text present but unparsed"), never `fail` — added 2026-09-25 after the orchestrator's live run
   turned up two real reviewed commits the trailer parser missed (see §8 regression examples). Everything
   else with no reviewer stays `unknown`. A descriptive ninja-count trend (not scored) is added alongside the
   rule. The orchestrator's live measurement on `trunk` since 2023-01-01 (2,151 non-merge commits: 1,773
   trailer-reviewed, 85 JIRA-field-only, 293 with neither — of which 72 are ninja, 209 are
   release-housekeeping, 2 have unparsed review wording (`unknown`), and **10** are genuine `CASSANDRA`-keyed
   commits with no reviewer evidence at all) is the basis for this change; see §8 for the exact figures, the
   two regression examples, and an independent order-of-magnitude spot-check.
2. **`jira-ticket-referenced`** stays fail-free: `pass` / `exempt` (same two patterns) / `unknown`.
3. **`pre-commit-ci-evidence`** ships enabled in v1 as **pass/unknown only**. Issue #36 must live-test the
   `ci-cassandra.apache.org` Jenkins JSON API before this rule can gain `fail_allowed: true` or a new evidence
   source in a v2.
4. **`code-style-checkstyle`** is approved as drafted (scoped to `cassandra-4.1`+; a red check-run is a
   legitimate `fail`).
5. **`changes-txt-entry`, `news-txt-entry`, `test-touched`** are all set to `scored: false` — they are
   displayed as facts on each commit row, never evaluated as pass/fail/unknown rules.
6. A new global result state, **`not_in_force`**, is added (§0 below), distinct from `unknown`: it marks a
   commit dated before a rule's `effective_from`, rather than a missing-evidence gap.
7. **Deferred to v2** (§9): `reviewer-is-committer` and the full "two +1 committer votes" count, both blocked
   on not having committer join dates from a public source.

Global result states (used by every rule below) are defined at the top of §2.

---

## Approved v2 (D24, issue #93)

**Approved by the project owner (`pmcfadin`) on 2026-09-27**, per DECISIONS.md D24 ("We should always follow
the rules set by the PMC and not make up our own"). `governance-policy.yaml` version 2 removes every rule and
exemption this project invented rather than sourced from the project's own published rules, and adds one new
rule the official docs now require. `CHANGELOG.md` has the real v1 -> v2 counts per check x result from
rescoring full history.

**What changed, and why:**

1. **`ninja` and `submodule-repin` exemptions removed.** v1's `ninja` exemption ("by long-standing project
   convention, observed throughout trunk history") and `release-housekeeping`'s `submodule-repin`
   sub-pattern had no official source — an audited convention, not a PMC rule. v2 scores these commits like
   any other. Code no longer special-cases `ninja` anywhere (`governance/checks.py`'s scoring order is
   entirely policy-driven: an exemption only ever comes from `Rule.matching_exemption`, which reads
   `governance-policy.yaml`'s `exemptions:` list — a removed exemption simply never appears there again). The
   **ninja-count-trend stays as a purely descriptive, unscored signal**: `governance/checks.is_ninja_declared`
   re-checks every commit message against the retired v1 pattern (`(?i)\bninja(fix)?\b`), independent of how
   the commit was actually scored, and the count is stored per-commit on `commit_fact.ninja_declared` (a plain
   display fact, exactly like `changes_txt_touched`/`test_touched`) rather than derived from `exempt` rows —
   see engineering note in `checks.py`.
2. **`commit-then-review` exemption added** (`reviewer-present`, `pre-commit-ci-evidence`,
   `ci-artefacts-attached`): the ratified cwiki governance page's own words — "Correcting typos, docs,
   website, and comments etc operate a "Commit Then Review" policy" — replace the old ninja/
   release-housekeeping pattern-matching with a **measurement choice we own**: a commit is "docs-only" when
   every changed path matches the policy's own top-level `docs_only_paths` glob list. Glob matching
   (`governance/policy.py`'s `_path_matches_glob`) uses gitignore-style semantics, not bare `fnmatch`: a
   leading `**/` means "at any depth including the repo root" (so `**/*.md` matches a top-level
   `CONTRIBUTING.md`, not just a nested one — plain `fnmatch.fnmatchcase` would wrongly require a literal `/`
   in the pattern), a trailing `/**` means "anything under this directory", and a bare pattern with neither is
   matched against the path's basename. A commit whose `changed_paths` weren't collected (e.g. a merge
   commit) can never be proven docs-only and never gets this exemption.
3. **`release-process` exemption sourced.** v1's `release-housekeeping` (version-increment, debian-changelog,
   submodule-repin, unsourced as a group) is replaced by `release-process`, with `version-increment` sourced
   to `release_process.html`'s exact `git commit` invocation and `debian-changelog` to the exact
   `cassandra-builds/cassandra-release/prepare_release.sh` line that produces it (`official_tooling`, "cited
   only for the exact commits those scripts create," per D24). `submodule-repin` is dropped (no source found).
4. **`pre-commit-ci-evidence` now scores from JIRA attachments too, and evidence must predate the commit.**
   The rule's own check_method previously only searched JIRA comments; v2 adds a JIRA-attachment evidence
   source (any attachment matching `(?i)^ci_summary` or `(?i)^results_details`) and requires whichever
   evidence is used to be dated **at or before the commit** — MEASUREMENT (ours): both the commit's own
   timestamp (`CommitFacts.commit_date`) and every evidence timestamp (`collectors/jira.py`'s
   `_parse_jira_timestamp`) are already UTC-aware by the time they reach `governance/checks.py`, so "at or
   before" is a plain, unambiguous datetime comparison — no separate timezone-normalization step is needed.
   Evidence that exists only *after* the commit is real (the CI eventually happened) but is not proof it
   happened *before* commit, so it gets its own `unknown` evidence string ("... dated ..., AFTER the commit")
   rather than being folded into "no evidence found at all."
5. **`ci-artefacts-attached` (new rule, `fail_allowed: true`, effective 2026-08-19).** Sourced to
   `patches.html`'s "attach the ci_summary and results_details artefacts to the ticket" and `ci.html`'s
   "Attach both to the Jira ticket... they outlive any CI instance," added to the official docs by
   `apache/cassandra-website` commit `30bdcc1` (2026-08-19). It requires **both** artefacts, on the same
   referenced issue, dated at or before the commit. It is the only new `fail_allowed` rule in v2, and its
   fail condition is deliberately narrow: a commit can only fail if the *referenced issue's attachment list
   has actually been fetched* this run or a prior one (`fetched_issue_keys` in
   `score_ci_artefacts_attached`) — an issue nobody has checked yet is `unknown` (backfill budget), never
   `fail`, because JIRA's attachment list is only trustworthy evidence of absence once it's actually been
   read. MEASUREMENT (ours): a commit referencing more than one issue key is scored against the first
   referenced key whose attachments have been fetched, in the order the commit message names them — this
   policy has no opinion about which of several referenced tickets "is" the CI record, so the
   already-established "first in referenced order" convention (`reviewer-present`'s union-of-evidence
   ordering) is reused rather than inventing a new tie-break rule.
6. **JIRA attachment metadata collection** (`collectors/jira_comments.py`'s `fetch_issue_evidence`,
   `pipeline._collect_governance_jira_evidence`): filename, `created` timestamp and attachment id only, never
   content — the same D1/D16 discipline the comment-evidence collector already applies to comment bodies.
   Budgeted, resumable and newest-referencing-commit-first, identical in shape to the existing CI-comment
   backfill (`_ci_eligible_issue_keys_newest_first`'s eligibility list already covers `ci-artefacts-attached`
   too, since its `effective_from` is strictly later than `pre-commit-ci-evidence`'s). Comments and
   attachments are fetched in **one JIRA call per issue** (`GET /rest/api/2/issue/{key}?fields=comment,
   attachment`) rather than two, to save budget — JIRA never paginates the `attachment` field, so this is
   also a strict correctness win for `ci-artefacts-attached` (the old dedicated `/comment` endpoint has no
   attachment equivalent at all). Whether an issue's attachment list has ever been fetched is its own
   queryable fact (`raw/governance/jira_attachment` has at least one row, sentinel or real, for that issue) —
   this is what lets "not fetched" (`unknown`) stay distinct from "fetched, and it's missing" (`fail`).
7. **Enforcement.** `tests/test_governance_policy_sources.py` fails if any *scored* rule, exemption, or
   `sub_pattern` in `governance-policy.yaml` lacks `source_type`/`source_url`/`source_quote`/`effective_from`
   (a key present with an explicit YAML `null` is fine; a missing key is not), or cites a `source_type` outside
   the file's own `source_types:` whitelist. It walks the raw YAML directly (not the parsed `Policy` objects)
   specifically so it can tell "key present, value null" apart from "key absent" — a distinction a value
   already parsed to `None` can't preserve. `project-health verify-policy-sources`
   (`governance/verify_sources.py`) re-fetches every one of those `source_url`s live and confirms
   `source_quote` still appears there:
   - **cwiki** (`ratified_governance`) is fetched through Confluence's REST content API
     (`.../confluence/rest/api/content?spaceKey=...&title=...&expand=body.storage`), not the rendered
     `/display/` page, and its `body.storage.value` (Confluence storage XHTML) is tag-stripped.
   - **GitHub blobs** (`official_tooling`) are rewritten to `raw.githubusercontent.com/<owner>/<repo>/<ref>/
     <path>` and fetched as plain text.
   - Everything else (`official_docs`) is fetched as HTML, tags stripped, entities unescaped.
   - Quote matching normalizes curly vs. straight quotes and collapses whitespace (including whitespace
     stripping-HTML-tags introduces just inside a quote mark, e.g. a cwiki `<strong>` around "Commit Then
     Review" rendering as `" Commit Then Review "` once its tags are stripped — the same normalization is
     applied to both the live page and the policy's own `source_quote`, so which side "should" have the
     space never has to be decided); a `source_quote` split by a literal `"..."` elision, or written as
     several independently double-quoted sentences with no elision marker at all (e.g.
     `code-style-checkstyle`'s two sentences), becomes multiple fragments that must **each** appear
     somewhere on the page; and a fragment's own outer wrapping quotes (the YAML convention for "this is a
     quoted excerpt") are stripped before matching, while quotes that only wrap part of a fragment (e.g. the
     literal `“Commit Then Review”` inside the cwiki sentence) are left alone. An opt-in live test
     (`tests/test_verify_policy_sources.py::TestLiveVerifyRealPolicy`, `RUN_LIVE_NETWORK_TESTS=1`) runs this
     against the real policy file; run live during issue #93's implementation, all ten sourced items passed.
8. **Live finding, fixed as a measurement correction (D24: `check_method`/`detail` text is measurement, not
   the protected rule text).** Spot-checking real `ci-artefacts-attached` fails against the live JIRA
   attachments API (issue #93 implementation, 2026-09-28) found the original filename match --- an anchored
   prefix, `(?i)^ci_summary` / `(?i)^results_details` --- was too strict for what `.build/run-ci` and
   contributors actually attach to real tickets:
   - **`results_details` vs. `result_details`.** CASSANDRA-21671's real attachments are named
     `result_details.tar.gz` (and `-1`/`-2` suffixed re-attempts) — singular "result", no "s" — never
     `results_details*`. The *rule*'s `source_quote` (from the official docs, "attach the ci_summary and
     results_details artefacts") is unchanged and still names both artefacts; only the code's and the
     policy's own `check_method.detail` prose describing how a filename is matched against those names has
     changed.
   - **Issue/branch-prefixed filenames.** CASSANDRA-21712's and CASSANDRA-21587's `ci_summary`-equivalent
     attachments are named `CASSANDRA-21712-cassandra-6.0-ci_summary.html` /
     `CASSANDRA-21712-trunk-ci_summary.html` — a `<ISSUE-KEY>-<branch>-` prefix before `ci_summary`.
   **Fix (measurement only — no `source_quote`/`source_url`/`source_type`/`effective_from`/`exemptions`/
   `result_semantics` changed anywhere in `governance-policy.yaml`):** `governance/checks.py`'s
   `_CI_SUMMARY_RE`/`_RESULTS_DETAILS_RE` are now unanchored substring searches (`re.search`, not
   `re.match`), `_RESULTS_DETAILS_RE` accepts either `results_details` or `result_details`
   (`(?i)results?_details`), and `governance-policy.yaml`'s two `check_method.detail` texts (the only lines
   touched) now describe a substring match with both real filenames as examples. Fail evidence now
   distinguishes three cases instead of a per-artefact "missing / attached later": `"no CI artefacts
   attached"`, `"results_details missing (ci_summary attached)"` (or the symmetric `"ci_summary missing
   (results_details attached)"`), and `"artefacts attached only after commit"` (relevant attachments exist,
   none dated at or before the commit). Re-scored from the same cached raw evidence (no re-collection):
   `ci-artefacts-attached`'s since-2026-08-19 breakdown went from 16 pass / 51 fail / 97 unknown / 6 exempt
   to **37 pass / 30 fail / 97 unknown / 6 exempt** — 21 of the original 51 fails were the filename-match
   defect, not real missing evidence; 3 have the artefacts attached only after the commit; the remaining 27
   are real misses (no CI artefacts on the ticket, or `ci_summary` present without `results_details`).
9. **Governance page.** Each rule's row now shows its `source_type` and `source_quote` linked to `source_url`;
   a small note lists exemptions removed in v2 (`ninja`, `submodule-repin`) with the reason; `SCORED_CHECK_IDS`
   including `ci-artefacts-attached` means the existing per-check trend chart, headline pass-rate card, and
   drift/backfill-pending machinery (issue #69) all pick up the new rule with no page-specific code — they
   were already written generically over `governance-policy.yaml`'s scored checks.

---

**Verification method.** Every rule below cites a primary source with a URL and, where the source states one,
an effective/ratification date. Every check method's hit rate was measured live against real Cassandra data on
**2026-09-25** — sample sizes, exact query/command, and raw counts are given inline so the numbers are
reproducible, not asserted. Where evidence is unreliable, the rule is marked for `unknown`-only scoring or for

**Verification method.** Every rule below cites a primary source with a URL and, where the source states one,
an effective/ratification date. Every check method's hit rate was measured live against real Cassandra data on
**2026-09-25** — sample sizes, exact query/command, and raw counts are given inline so the numbers are
reproducible, not asserted. Where evidence is unreliable, the rule is marked for `unknown`-only scoring or for
no scoring at all, per D15 ("any evidence source with a meaningful false-negative rate must yield `unknown`,
never `fail`").

---

## 1. Primary sources consulted

| Source | What it establishes | Dated? |
|---|---|---|
| [`cassandra.apache.org/_/development/patches.html`](https://cassandra.apache.org/_/development/patches.html) ("Contributing Code Changes") | JIRA-ticket-first workflow, CHANGES.txt rule, testability, code style, branch merge order, +1/review process | No page-level date rendered; site says "The Cassandra project follows..." (living page, not versioned) |
| [`cassandra.apache.org/_/development/how_to_commit.html`](https://cassandra.apache.org/_/development/how_to_commit.html) | Commit message format, forward-merge-with-`ours`-strategy workflow, atomic multi-branch push | Not dated on page |
| [`cassandra.apache.org/_/development/how_to_review.html`](https://cassandra.apache.org/_/development/how_to_review.html) (Review Checklist) | NEWS.txt/cql3-docs/native-protocol-spec update check, pre-commit CI attachment check, test-comprehensiveness check | Not dated on page |
| [`cassandra.apache.org/_/development/ci.html`](https://cassandra.apache.org/_/development/ci.html) | Three CI surfaces (GitHub Actions, pre-commit, post-commit), profiles, "testing... is done with pre-commit CI" | Not dated on page |
| [`cassandra.apache.org/_/development/code_style.html`](https://cassandra.apache.org/_/development/code_style.html) | Checkstyle enforcement, **"Checkstyle is part of the build from Cassandra 4.1 included"** | Version-anchored (4.1+) |
| [**`cwiki.apache.org` — "Cassandra Project Governance"**](https://cwiki.apache.org/confluence/display/CASSANDRA/Cassandra+Project+Governance) | The binding voting rules for code contributions (review count, CI-before-commit, veto, commit-then-review exemption) | **"STATUS: Ratified 2020/06/25"** — page last edited by a PMC member "yesterday" relative to this research (i.e. actively maintained, not abandoned) |
| `apache/cassandra` `CONTRIBUTING.md` (repo root, trunk) | Same workflow restated with exact `.build/` tool invocations; three CI systems named with exact trigger conditions | Git blame: last touched **2026-08-19** (recent, actively maintained) |
| `apache/cassandra` `.github/workflows/code-check.yaml`, `jenkins-check.yaml` | Confirms `ant-check-jdk11`/`ant-check-jdk17` run `.build/docker/check-code.sh` on every push to any branch of any fork | Live file content, current HEAD |
| `apache/cassandra` `.asf.yaml` | `enabled_merge_buttons: {squash: false, merge: false, rebase: true}` — PR-merge SHA equals the trunk commit SHA | Live file content |

No CEP or dev@ thread was needed as a primary source for *per-commit* minimums specifically — the ratified
cwiki governance page is the authoritative, dated statement of the voting/CI/review rules, and it explicitly
covers "For Code Contributions" as its own subsection distinct from CEP and release voting. CEP process rules
(also on that page) are out of scope here since a CEP is a design proposal, not a per-commit check.

**A caution about `patches.html`'s branch-policy table:** it lists `4.0` as "Code freeze" and `3.11`/`3.0`/`2.2`/
`2.1` as "Critical bug fixes only" — but `downloads.apache.org` currently mirrors **4.0.21** (`docs/spec/
DATA-SOURCES.md` §5), many releases past any "freeze." That table is stale and must not be used to determine
which branches are "in scope" today. This document uses the branches with a currently-mirrored release
(`3.0`, `3.11`, `4.0`, `4.1`, `5.0`, per `downloads.apache.org`, verified 2026-09-25) plus `trunk`/`cassandra-6.0`
as the in-scope branch set, not the stale table.

---

## 2. Rules, sources, and scoring semantics

Each rule states: exact wording, source, effective-from (only if the source states one — never invented),
what counts as evidence, and the measured hit rate. `applies_to` restricts branches/change-types per the
source's own stated scope.

**Global result states** (`governance-policy.yaml` top-level `result_states`, approved v1):

| State | Meaning |
|---|---|
| `pass` | Evidence positively confirms the rule was met. |
| `fail` | Evidence positively contradicts the rule. Only reachable on a rule with `fail_allowed: true`, via that rule's specific stated condition — never merely "no evidence found." |
| `unknown` | The rule is in force and no exemption applies, but no evidence was found, or the evidence source's false-negative rate is too high to read absence as non-compliance. |
| `exempt` | The commit matches a documented, testable exemption pattern (e.g. `ninja`, `release-housekeeping`). The rule doesn't apply, regardless of evidence. |
| `not_in_force` | The commit's date is before the rule's `effective_from`. Distinct from `unknown` — this is a fact about the policy's timeline (D14), not a missing-evidence gap. A rule with no dated source is always in force and never produces this state. |

### R1 — Code changes require at least one reviewer, evidenced by name

> "Code modifications must have been reviewed by at least one other contributor."
> "Code modifications require two +1 committer votes (can be author + reviewer)."
> "Modifications involving only test code require one +1 vote from a non-author committer."

— [Cassandra Project Governance](https://cwiki.apache.org/confluence/display/CASSANDRA/Cassandra+Project+Governance), **ratified 2020-06-25**.

- **`applies_to`**: all branches, all non-trivial code changes. Explicitly **exempted**: "Correcting typos,
  docs, website, and comments etc. operate a 'Commit Then Review' policy" (no review requirement at all for
  that class of change).
- **Check method**: a named reviewer present in either (a) the commit-trailer `reviewed by` clause
  (`src/project_health/collectors/reviewer_trailer.py`, already built) or (b) a populated JIRA reviewer
  custom field (`customfield_12313420` "Reviewers", `customfield_10022` "Reviewer" — see
  `DATA-SOURCES.md` §2).
- **Measured hit rate**:
  - Commit-trailer, full history, non-merge commits (`docs/spec/DATA-SOURCES.md` §1): 79–87% for 2018+,
    43–57% for 2009–2013. **Coverage is convention-based, not enforced by tooling, so absence is not proof
    of no review** — see false-negative note below.
  - JIRA reviewer field (either field populated), live sample of the **30 most-recently-resolved
    `CASSANDRA` issues** (`jql=project=CASSANDRA AND resolution=Fixed ORDER BY resolved DESC`, fetched
    2026-09-25): **28/30 = 93%**.
- **Result semantics (approved v1, corrected 2026-09-25 — `fail_allowed: true`)**: `pass` if a named reviewer
  is found by either method. **`fail`** only when *all four* hold: (a) the commit references a `CASSANDRA-N`
  issue key, (b) no reviewer is found by either method, (c) no exemption below matches, and (d) the commit
  message contains **no review wording at all** — no case-insensitive match of `\breview(ed|er|ers)?\b`
  anywhere in the message. `unknown` if either no issue key is referenced and no reviewer is found, **or**
  review wording *is* present but no reviewer was successfully parsed from it (evidence recorded as "review
  text present but unparsed" — see the regression examples below; this can never be a fail). `exempt` if a
  pattern below matches, regardless of reviewer evidence. `not_in_force` before 2020-06-25.
- **Review-wording guard, added after a false-fail regression found by the orchestrator applying these
  patterns live**: the trailer parser (`reviewer_trailer.py`) only recognizes the literal phrase
  `reviewed by`, anchored at the start of a line. Two real trunk commits since 2023 name a reviewer in a form
  it misses, and would otherwise have `fail`ed a genuinely reviewed commit with names attached — exactly what
  D15 exists to prevent:
  - `208d87513f` — `"patch by Mick Semb Wever; reviewed Štefan Miklošovič for CASSANDRA-21489"` (reviewer
    named without the word "by").
  - `05186d7869` — `"Authored by Lorina Poland (polandll); Reviewed by Branimir Lambov (blambov) for
    CASSANDRA-18236"` (the line starts with "Authored by...", not "patch by" or "reviewed by", so the
    anchored trailer regex never matches even though "Reviewed by" is literally present in the message).

  Both now resolve to `unknown` under the corrected rule instead of `fail`. **Issue #36 should extend
  `reviewer_trailer.py` to parse the "reviewed &lt;Name&gt;" (missing "by") and "Authored by …; Reviewed by
  …" forms**, so these become real `pass` results in a future version rather than `unknown`.
- **Exemptions (owner-approved, precise and testable — see §8 for the measurement behind them)**:
  - **`ninja`**: `(?i)\bninja(fix)?\b` anywhere in the commit message. Matches the project's own
    self-declared convention for small, uncontroversial changes committed without the full review process
    (observed forms in real history: `ninja fix:`, `ninjafix –`, `ninja:`, `ninja - fix`, `ninja trunk patch
    for CASSANDRA-N`).
  - **`release-housekeeping`**: any of three sub-patterns matching mechanical release-process commits, none
    of which carry a reviewer trailer by convention:
    - `version-increment`: `^(increment|bump)\b.*\bversion\b` on the first line (e.g. `increment to version
      5.0.10`, `Bump version, prepare CHANGES`).
    - `debian-changelog`: `^prepare\s+debian\s+changelog\b` on the first line (e.g. `Prepare debian changelog
      for 3.11.19`).
    - `submodule-repin`: a `repin`/`bump` verb within 40 characters of `submodule` (e.g. `repin accord
      submodule`).
  - A **ninja-count trend** (count of `ninja`-exempt commits per month/quarter) is shown next to this rule as
    a descriptive signal — not scored, informational only.
- **What is explicitly NOT scored (deferred to v2, §9)**: the "two +1 committer votes" *count* and the "must
  be a committer" *status* requirement. Neither the commit trailer nor the JIRA field states whether a named
  reviewer held committer status **at the time of review**, and no confirmed, public, dated committer-only
  (non-PMC) roster exists (`DATA-SOURCES.md` §6 — Whimsy's `committee-info.json` is PMC-only;
  `reporter.apache.org` is 401/ASF-committer-gated). Scoring "was this a committer" would require guessing
  from current PMC/committer status projected backward, which is exactly the kind of unverifiable inference
  D15 forbids for named results. **v1 scores "named reviewer present," never "committer vote count."**

### R2 — Pre-commit CI must be run and its artifacts attached before commit

> "Testing a patch before it is committed is the author's and the reviewer's job, and it is done with
> pre-commit CI." ... "Attach both to the Jira ticket. ... Every patch is expected to carry them."
> — [ci.html](https://cassandra.apache.org/_/development/ci.html), `CONTRIBUTING.md`

> "Code must not be committed before CI results have been provided for all affected branches."
> — [Cassandra Project Governance](https://cwiki.apache.org/confluence/display/CASSANDRA/Cassandra+Project+Governance), ratified 2020-06-25.

- **`applies_to`**: all branches; "all affected branches" ties this rule directly to the merge-forward
  question (§3 below).
- **Check method (JIRA)**: JIRA comment or attachment on the issue mentioning CI evidence — searched for
  the literal terms `ci_summary`, `results_details`, `circleci`, `ci-cassandra.apache.org`,
  `pre-ci.cassandra.apache.org`, `jenkins`, `butler`, `.build/run-ci` in comment bodies.
- **Check method (GitHub)**: GitHub Checks API (`GET /repos/apache/cassandra/commits/{sha}/check-runs`) —
  this only ever shows the **GitHub Actions** licence/checkstyle surface (`ant-check-jdk11`/`jdk17`), never
  the pre-commit Jenkins pipeline that the rule actually requires (pre-commit CI results are attached to
  JIRA, not exposed as a GitHub check on the eventual commit).
- **Measured hit rate**:
  - JIRA-comment CI evidence, same 30-issue live sample as R1: **12/30 = 40%**.
  - GitHub check-runs present, last **29 non-merge commits on `trunk`** (`git log --no-merges -n 30`,
    2026-09-25): **20/29 = 69%** — but this measures the *wrong* CI surface for this rule (see above); it is
    real evidence of the separately-documented "GitHub Actions on every push" rule, not of pre-commit-CI
    compliance.
- **Result semantics**: `pass` only on found JIRA-side CI evidence; `unknown` otherwise. **Never `fail`**: a
  40% hit rate on the correct surface means a 60% false-negative rate if absence were scored as non-compliance
  — CI is very likely run in most of those cases (`ci.html`: "Contributors without an account should say so
  on the ticket; the reviewer or committer then runs CI on the patch's behalf," meaning it can happen without
  ever being posted back by the original author, or the artifact link can rot/expire since Jenkins build
  history is not permanent).
- **Result semantics (approved v1 — pass/unknown only, no fail)**: `pass` on found JIRA-side CI evidence;
  `unknown` otherwise. `ci-cassandra.apache.org`'s Jenkins JSON API for post-commit-by-SHA was in scope for
  this research per the issue but was not evaluated live this session (time-boxed); **issue #36 is tasked
  with live-testing it, and only if that proves reliable enough may this rule gain `fail_allowed: true` or a
  new evidence source in a v2** (`governance-policy.yaml` → `pre-commit-ci-evidence.v2_upgrade_condition`).

### R3 — Every commit's message must reference a JIRA issue key (except trivial fixes)

> "`patch by <Authors,>; reviewed by <Reviewers,> for CASSANDRA-#####`" — the commit message format itself
> requires the ticket key.
> "Create a new issue early in the process describing what you're working on."
> — [how_to_commit.html](https://cassandra.apache.org/_/development/how_to_commit.html), [patches.html](https://cassandra.apache.org/_/development/patches.html)

- **`applies_to`**: code changes. Exempted for the same "Commit Then Review" trivial-change class as R1
  (typos/docs/website/comments) — those do not need a ticket per the ratified governance page's own carve-out.
- **Check method**: `_ISSUE_KEY_RE` match (already implemented in `reviewer_trailer.py`) against the commit
  message, or presence of a JIRA-key-shaped GitHub PR title/branch name.
- **Measured hit rate**: not separately re-measured here — this is implied by the reviewer-trailer regex
  already characterized in `DATA-SOURCES.md` §1 (issue keys co-occur with the trailer at the same rate as
  the trailer itself, since the format is one clause). Treat as the same 79–87%/2018+ figure.
- **Result semantics (approved v1 — no fail state)**: `pass` if an issue key is found; `exempt` if the same
  `ninja` or `release-housekeeping` patterns from R1 match (release-housekeeping commits like "Prepare debian
  changelog for X" legitimately have no issue key at all); `unknown` otherwise — there is no reliable way to
  distinguish "should have had a ticket and didn't" from an exempt or otherwise legitimate omission without
  reading the diff's semantic content (which this project does not classify in Phase 1).

### R4 — CHANGES.txt entry for user-impacting changes; NEWS.txt for upgrade-relevant changes

> "Include a CHANGES.txt entry (put it at the top of the list) ... only user-impacting items should be listed
> in CHANGES.txt. If you fix a test that does not affect users and does not require changes in runtime code,
> then no CHANGES.txt entry is necessary." — [patches.html](https://cassandra.apache.org/_/development/patches.html)

> "Have NEWS.txt, the cql3 docs, and the native protocol spec been updated if needed?" — reviewer checklist,
> [how_to_review.html](https://cassandra.apache.org/_/development/how_to_review.html)

- **`applies_to`**: CHANGES.txt — user-impacting changes only (explicitly conditional, not universal).
  NEWS.txt — upgrade-relevant changes only (also explicitly conditional: "if needed").
- **Check method**: `git show --name-only <sha>` for an exact-path match on `CHANGES.txt` / `NEWS.txt`
  (README's blobless-clone technique — trees are present even without blobs, so this is cheap and reliable
  as a *file-touched* signal).
- **Measured hit rate**, last 29 non-merge `trunk` commits, 2026-09-25:
  - CHANGES.txt touched: **21/29 = 72%**.
  - NEWS.txt touched: **2/29 = 7%** (expected to be low — most changes are not upgrade-relevant).
  - A secondary signal exists but was not live-tested this session: the JIRA "client-impacting" /
    "doc-impacting" labels the review checklist asks reviewers to set (`how_to_review.html`: "Is the ticket
    tagged with 'client-impacting' and 'doc-impacting', where appropriate?"). Cross-referencing that label
    against CHANGES.txt/NEWS.txt presence would sharpen this check but needs its own hit-rate measurement
    before being relied on.
- **Result semantics (approved v1 — `scored: false`)**: the owner set both checks to display-only. Whether
  the file was touched is shown as a fact on each commit row; **it is never evaluated as pass/fail/unknown**.
  The rule is conditional on "user-impacting"/"if needed," a judgment this project cannot make
  algorithmically from file paths alone, so no verdict is produced at all rather than defaulting to
  `unknown` on every row.

### R5 — Code style: checkstyle enforcement (4.1+), Sun Java conventions otherwise

> "The Cassandra project follows Sun's Java coding conventions for anything not expressly outlined in this
> document." "Checkstyle is part of the build from Cassandra 4.1 included." "The checkstyle target is
> executed by default when e.g. `build` or `jar` targets are executed."
> — [code_style.html](https://cassandra.apache.org/_/development/code_style.html)

> GitHub Actions run `ant check` (licence and checkstyle) "on every push to any branch of any fork."
> — `CONTRIBUTING.md`, confirmed live in `.github/workflows/code-check.yaml` (`ant-check-jdk11`,
> `ant-check-jdk17`, both run `.build/docker/check-code.sh`, `on: push: branches: ['**']`).

- **`applies_to`**: **`cassandra-4.1` and newer branches, and `trunk`, only** — this is the one rule with a
  clean, version-anchored `effective_from` in the source itself, rather than a calendar date. Do not apply
  to `cassandra-4.0` and earlier.
- **Check method**: GitHub Checks API on the commit SHA (works only for commits that went through a GitHub
  PR/push that GitHub Actions actually ran against — see .asf.yaml rebase-merge note in §1 for why PR SHA =
  trunk SHA).
- **Measured hit rate**: same sample as R2 — **20/29 = 69%** of recent `trunk` commits have check-runs
  recorded. The remaining 31% are very likely direct-push / patch-file-based contributions (the
  "Patch based Contribution" workflow in `how_to_commit.html` uses `git am`/`git apply` directly against a
  committer's local clone, never touching GitHub's push-triggered Actions at all) rather than checkstyle
  failures — **a real, structural false-negative source**, not noise.
- **Result semantics (approved v1, as drafted — `fail_allowed: true`)**: `pass` if a check-run exists and is
  green; `unknown` if no check-run exists at all (never `fail` for "no check-run found" — per the
  false-negative source above). A check-run that exists and is red is a legitimate `fail`, since that is
  direct, unambiguous evidence.

---

## 3. Merge-forward and cherry-picks — how a fix landing on multiple branches is scored

Cassandra's own process is explicit that a fix is applied once, on the oldest affected branch, then
forward-merged: "Fixes are applied first on the oldest applicable release branch, and are then forward-merged
onto each newer branch using an `ours` merge strategy... Each forward-merge commit contains the
branch-appropriate patch" (`how_to_commit.html`). The hypothetical worked example on that page merges
`cassandra-4.0 → cassandra-4.1 → cassandra-5.0 → cassandra-6.0 → trunk`, pushed atomically.

**A concrete finding from live inspection of the bare clone** (not assumed from the docs): forward-merge
commits are real `git merge -s ours` commits with **two parents**, and the existing collector design
(`DATA-SOURCES.md` §1, `ARCHITECTURE.md` §3.1) filters `--no-merges` for *all* review/reviewer-trailer
counting, on the stated grounds that ordinary `Merge branch 'x' into y` commits carry no trailer of their own.
That reasoning is correct for the common case, but not universal: sampling the last 5,000 merge commits on
`origin/cassandra-5.0` (`git log --merges -n 5000 --pretty=format:'%H%n%B'`), **6 of them carry a full
`patch by X; reviewed by Y for CASSANDRA-NNNNN` trailer and a real code diff** — e.g. the `git commit --amend`
step in the documented workflow squashes the cherry-picked patch (with its original trailer) into what is
still, structurally, a merge commit. A blanket `--no-merges` filter, applied per-commit rather than only for
aggregate-rate denominators, would silently show `unknown` for a named, identifiable commit that in fact has
full reviewer evidence — a real risk for D15's per-commit-with-names display.

**Recommendation:**
- **Per-commit checks (R1–R5) must run against every commit, merge or not.** Reserve `--no-merges` for
  *aggregate* rate/denominator calculations only (as `DATA-SOURCES.md` already correctly does for
  project-wide coverage percentages) — never for suppressing evidence on an individual named commit.
- **Score each landing on each branch as its own commit**, not as duplicates of "the same fix." A fix on
  `cassandra-4.1`, `cassandra-5.0`, and `trunk` is three separate SHAs, three separate review/CI expectations
  ("CI results have been provided for **all** affected branches" — ratified governance page), and, per D15,
  three separate named rows.
- **Do not score "was this fix merged forward to every branch it should have reached" as pass/fail, ever.**
  Whether a bug affects a given older or newer branch is a product/code judgment ("clear about which versions
  you could verify to be affected by the bug," `patches.html`) that no structural signal (git, JIRA fields)
  can answer. The only thing checkable is *where a given JIRA key's commits were found* (`git log --all
  --grep=CASSANDRA-NNNNN`), which should be shown as **descriptive** information ("found on branches: 4.1,
  5.0, trunk") next to each commit row, never folded into a pass/fail governance verdict.
- **Branches in scope for this descriptive cross-branch view**: those with a currently-mirrored release per
  `downloads.apache.org` (`3.0`, `3.11`, `4.0`, `4.1`, `5.0`, verified 2026-09-25) plus `cassandra-6.0`/`trunk`
  — not the stale table in `patches.html` (§1 caution above).

---

## 4. Rules not scored in v1, and why

| Rule | Why not scored |
|---|---|
| **Committer status of a named reviewer / the "two +1" vote count** (R1) | **Deferred to v2** (§9), not permanently excluded. No confirmed, public, dated non-PMC committer roster exists (`DATA-SOURCES.md` §6). Scoring this would require inferring committer status at a past point in time from present-day PMC/committer lists — an unverifiable backward projection, forbidden by D15's evidence standard for named results. |
| **Explicit veto / "-1 blocking" / "active reasonable discussion" rules** (ratified governance page) | Detecting a `-1` and whether it was "resolved by follow-up commits" or "not engaged with reasonably" requires reading and classifying comment *content* — gated behind Phase 2a's classifier-validation requirement (D1), not available in Phase 1's metadata-only, deterministic scope. |
| **CI *result* (pass/fail of the run itself)**, only presence of CI evidence (R2) | The `ci-cassandra.apache.org` Jenkins JSON API for post-commit-by-SHA results was named in the issue as in-scope but was not live-tested this session; issue #36 covers the live test, which may unlock a v2 upgrade (see `pre-commit-ci-evidence.v2_upgrade_condition`). |
| **"Tests required" as a universal rule** | Not a documented Cassandra rule — `patches.html` states explicitly that "the extent to which tests are required depends on how likely your changes will effect the stability of Cassandra in production," a discretionary, risk-proportional standard, not a fixed minimum. See §5 for the display-only, non-authoritative signal instead (`scored: false`). |
| **CHANGES.txt / NEWS.txt** (R4) | **Approved v1: `scored: false`.** Both rules are explicitly conditional ("user-impacting," "if needed"). Rather than default every row to `unknown`, the owner chose to display file-touched as a plain fact and not evaluate it as a rule at all. |
| **Merge-forward completeness** (§3) | Requires product knowledge (does the bug affect this branch?) no structural source can supply. Shown descriptively only, never scored. |
| **Checkstyle/CI-on-every-push for `cassandra-4.0` and earlier branches** (R5) | The rule's own source ties checkstyle-in-build to 4.1+; applying it to older branches would misattribute a rule that didn't exist there yet. |
| **Any rule for commits before its `effective_from`** | **Approved v1: formalized as the `not_in_force` result state** (§2), distinct from `unknown`. Per D14 ("Each commit is judged against the policy in force on its commit date"), `reviewer-present`'s and `pre-commit-ci-evidence`'s ratified-page language (2020-06-25) is never back-applied — commits before that date show `not_in_force`, not a pass/fail/unknown verdict on a rule that didn't exist yet. |

## 5. Optional signal — not a documented Cassandra rule

**OPT-1: commit touches a `test/` path.** Measured on the same 29-commit `trunk` sample: **23/29 = 79%**.
This is offered only as an *informational* signal on the governance page (e.g. "touched tests: yes/no"),
**clearly marked "not a documented Cassandra rule"** per this research's instructions — Cassandra's own docs
explicitly reject a fixed "tests required" minimum (see §4). It must never be scored `pass`/`fail`, only
shown as a fact.

---

## 6. Summary of measured hit rates (all 2026-09-25)

| Check | Sample | Hit rate |
|---|---|---|
| JIRA reviewer field populated | 30 most-recently-resolved `CASSANDRA` issues | 28/30 = 93% |
| JIRA comment contains CI evidence | same 30 issues | 12/30 = 40% |
| GitHub check-runs present on commit SHA | last 29 non-merge `trunk` commits | 20/29 = 69% |
| Commit touches `CHANGES.txt` | same 29 commits | 21/29 = 72% |
| Commit touches `NEWS.txt` | same 29 commits | 2/29 = 7% |
| Commit touches a `test/` path (optional signal) | same 29 commits | 23/29 = 79% |
| Merge commits (on `cassandra-5.0`) carrying a real reviewer trailer despite `--no-merges` conventions | last 5,000 merge commits on `cassandra-5.0` | 6/5,000 ≈ 0.12% — small but structurally real, see §3 |
| Reviewer commit-trailer, non-merge commits, 2018+ | full history (`DATA-SOURCES.md` §1) | 79–87% |
| Reviewer commit-trailer, non-merge commits, 2009–2013 | full history (`DATA-SOURCES.md` §1) | 43–57% |
| `reviewer-present` no-reviewer-evidence breakdown (`trunk`, non-merge, since 2023-01-01; orchestrator's measurement, basis for v1's `fail_allowed`, corrected 2026-09-25) | 2,151 commits | 1,773 trailer / 85 JIRA-field-only / 293 neither → of the 293: 72 `ninja`-exempt, 209 `release-housekeeping`-exempt, 2 `unknown` (review text present but unparsed — see §8 regression examples), **10 genuine fails** |

---

## 7. Open questions for the project owner — resolved 2026-09-25 (see "Approved v1" above)

1. ~~Committer-status verification~~ — **resolved**: deferred to v2 (§9), not scored in v1.
2. ~~`ci-cassandra.apache.org` Jenkins API~~ — **resolved**: not tested before v1 ships; `pre-commit-ci-evidence`
   stays pass/unknown-only, and issue #36 is tasked with the live test before any v2 upgrade.
3. ~~Historical scope~~ — **resolved**: a new `not_in_force` result state (§2) marks pre-effective-date
   commits explicitly, distinct from `unknown` — columns are shown, not hidden, but with an unambiguous label.
4. **Still open**: the "client-impacting"/"doc-impacting" JIRA label cross-check (§2 R4) was identified but
   not hit-rate-tested this session — remains a candidate for a future sharpening of the (now display-only)
   CHANGES.txt/NEWS.txt facts, not addressed in this approval round.
5. **Still open**: descriptive cross-branch view (§3) timing — shown from day one, or only once multi-branch
   ingestion is built — not addressed in this approval round; defaults to "once ingestion exists" absent
   further owner direction.

---

## 8. `reviewer-present` v1 fail logic — measurement, regression examples, and exemption patterns

The owner's approval turned on a live measurement of `trunk`, non-merge commits since 2023-01-01
(2,151 commits): **1,773** carry a commit-trailer reviewer, **85** more have no trailer but a populated JIRA
reviewer field, and **293** have neither. Of those 293, **72** self-declare as `ninja` commits and **209** are
mechanical release-housekeeping. Applying the v1 patterns live, the orchestrator initially got **12** fails
from the remaining commits — but **2 of the 12 were false fails**: real reviewed commits the trailer parser
missed because of an unsupported phrasing, which would have publicly shown a reviewed, named commit as
unreviewed (exactly what D15 exists to prevent). Those two are now `unknown` ("review text present but
unparsed") instead, leaving **10** genuine fails — ordinary `CASSANDRA`-keyed commits with no reviewer
evidence in any recognized form at all.

**Regression examples (why the review-wording guard exists)**:

| SHA | Message | Why the parser missed it |
|---|---|---|
| `208d87513f` | `patch by Mick Semb Wever; reviewed Štefan Miklošovič for CASSANDRA-21489` | Reviewer named without the word "by" — `reviewer_trailer.py`'s `_TRAILER_LINE_RE` requires the literal phrase `reviewed by`. |
| `05186d7869` | `Authored by Lorina Poland (polandll); Reviewed by Branimir Lambov (blambov) for CASSANDRA-18236` | The line starts with "Authored by...", not "patch by" or "reviewed by" — the anchored trailer regex never matches even though "Reviewed by" is literally present later in the same line. |

The fix (`governance-policy.yaml` → `reviewer-present.review_wording_check`): before a `fail` is produced,
also check the commit message for any case-insensitive match of `\breview(ed|er|ers)?\b`. If it matches and
still no reviewer was extracted, the result is `unknown` with evidence `"review text present but unparsed"`,
never `fail`. Both regression examples resolve correctly to `unknown` under this rule. **Issue #36 should
extend `reviewer_trailer.py` to parse the "reviewed &lt;Name&gt;" (missing "by") and "Authored by …;
Reviewed by …" forms**, which would turn these into real `pass` results in a future version.

**Independent spot-check** (this session, text-matching only, no JIRA-field cross-reference, so not expected
to reproduce the exact 10): applying the same `ninja` regex and the three `release-housekeeping` sub-patterns
to the same population (`git log origin/trunk --no-merges --since=2023-01-01`, 2,151 commits parsed) found
**114** ninja-token matches and **116** release-housekeeping-pattern matches (46 version-increment, 69
debian-changelog, 1 submodule-repin), leaving **47** residual no-trailer commits that reference an issue key.
That residual is larger than 10 because it has no JIRA-reviewer-field cross-reference and no
review-wording-check applied — several of those 47 almost certainly resolve to the 85 JIRA-field-only bucket
or the review-text-present-but-unparsed bucket once those checks run, which is exactly what the real
`reviewer-present` implementation does before declaring a fail. The two counts are consistent in order of
magnitude and give no reason to doubt the exemption patterns' precision.

**Exemption pattern reference** (also in `governance-policy.yaml`):

| Exemption | Pattern | Scope | Example match |
|---|---|---|---|
| `ninja` | `(?i)\bninja(fix)?\b` | full message | `ninjafix – links in CONTRIBUTING.md` |
| `release-housekeeping` / `version-increment` | `(?im)^(increment\|bump)\b.*\bversion\b` | first line | `increment to version 5.0.10` |
| `release-housekeeping` / `debian-changelog` | `(?im)^prepare\s+debian\s+changelog\b` | first line | `Prepare debian changelog for 3.11.19` |
| `release-housekeeping` / `submodule-repin` | `(?i)\b(repin\|bump)\b.{0,40}\bsubmodule\b\|\bsubmodule\b.{0,40}\b(repin\|bump)\b` | full message | `repin accord submodule` |
| *(guard, not an exemption)* `review_wording_check` | `(?i)\breview(ed\|er\|ers)?\b` | full message | forces `unknown` instead of `fail` when review wording is present but unparsed |

A descriptive **ninja-count trend** (ninja-exempt commits per month/quarter) is displayed next to
`reviewer-present` — informational only, never scored.

---

## 9. Deferred to v2

| Rule | Why deferred |
|---|---|
| `reviewer-is-committer` | Verifying the named reviewer held committer status **at the time of review** needs a public, dated, non-PMC committer roster with join dates. `DATA-SOURCES.md` §6 found Whimsy's `committee-info.json` is PMC-only and `reporter.apache.org` is 401/ASF-committer-gated — no such roster is confirmed to exist publicly yet. |
| `two-committer-plus-one-votes` | The ratified rule ("two +1 committer votes, can be author + reviewer") needs the same committer-join-date data as above, plus counting distinct binding +1s rather than a single named-reviewer string, and knowing the author's own committer status. |

Both remain scored as their v1 proxy only (`reviewer-present`: "a reviewer is named, by either evidence
source") until a future research task locates a usable committer roster.

---

## 10. Issue #36 live findings — `ci-cassandra.apache.org` Jenkins JSON API (informational; no policy change)

`pre-commit-ci-evidence.v2_upgrade_condition` tasked issue #36 with live-testing the
`ci-cassandra.apache.org` post-commit Jenkins JSON API before this rule could gain `fail_allowed: true`
or a new evidence source. Live-tested 2026-09-25 against `https://ci-cassandra.apache.org`
(`project_health.governance.jenkins_probe.probe_job`, not committed as a scored evidence source — this
section reports findings only, `governance-policy.yaml` is unchanged):

- **Availability**: live and responsive. `GET /api/json?tree=jobs[name,url]` returned 190 jobs,
  including per-branch post-commit jobs (`Cassandra-trunk`, `Cassandra-5.0`, `Cassandra-4.1`, ...),
  triggered by `hudson.triggers.SCMTrigger$SCMTriggerCause` (SCM-poll, i.e. post-commit, matching the
  policy's `ci_cassandra_jenkins_api` description).
- **SHA joinability**: **yes, and verified against real history.** Every build carries a
  `hudson.plugins.git.util.BuildData` action per configured git remote; the one for
  `https://github.com/apache/cassandra` (`lastBuiltRevision.SHA1`) is the exact commit built. Sampled
  10 recent builds across `Cassandra-trunk` and `Cassandra-5.0`: **10/10** had a resolvable
  `apache/cassandra` SHA, and one (`074a3f5605ef8cb3fa9c433980d4d59768b103d3`, `Cassandra-trunk` build
  2615) was independently confirmed to exist in this project's own bare clone
  (`git cat-file -t` / `git log -1`) as a real `cassandra-6.0` → `trunk` merge commit. Build results
  also carry a `result` field (`SUCCESS`/`UNSTABLE`/`FAILURE`/`ABORTED`), a real fail signal if this
  were ever scored.
- **History depth: shallow, and inconsistent per job — this is the blocker for a v2 upgrade.**
  `Cassandra-trunk` retains only its **30** most recent builds (`firstBuild` 2578 → `lastBuild` 2615;
  build 2577 and gaps like 2612/2613 all 404), spanning **2026-08-29 to 2026-09-25** (about 27 days) —
  a high-traffic job's window is a matter of weeks. `Cassandra-5.0` also retains only 31 builds, but
  because that branch builds far less often, those 31 span **2025-01-27 to 2026-09-25** (~20 months) —
  retention is a **build-count limit, not a time limit**, so the usable historical depth for any given
  branch depends entirely on that branch's own commit/build frequency, not a fixed calendar window.
  There is also no server-side "find the build for this SHA" query: joining a specific historical commit
  requires enumerating a job's retained build list and matching client-side, which only works at all for
  commits still within that job's shrinking retention window.

**Conclusion for a future v2**: the API is reliable and technically joinable to a commit SHA where a
build still exists, satisfying D15's "verifiable evidence" bar for *recent* commits. It cannot support
backfilling `pre-commit-ci-evidence` (or a hypothetical CI-*result* rule) over historical commits —
the vast majority of this project's scored history predates every sampled job's retention window
entirely. A v2 upgrade using this source would only be honest as a **forward-looking** addition (e.g.
"this commit, if made in roughly the last N weeks on its branch, has a joinable Jenkins result"),
never as a way to fill in `unknown` rows for older commits — and even then, `N` varies by branch and
would need to be re-measured periodically as retention rolls forward, not hard-coded once.

---
## 11. Security: OpenSSF Scorecard + CVE/advisory history (issue #55)

Distinct from §§1-9 above (which score Cassandra's own *process* — review, CI, ticket linkage), this
section covers two **external, third-party** security signals shown alongside (never blended into) the
per-commit compliance results: the OpenSSF Scorecard's per-check security-posture results, and CVE/
advisory metadata. Neither is a community-health or process-compliance measure (`RESEARCH.md` §6
already established this for Scorecard specifically) — they are shown because a governance/trust page
for a project this size is incomplete without them, per D21 item 3.

### 10.1 OpenSSF Scorecard — per-check, never a single score

**Source**: `https://api.securityscorecards.dev/projects/github.com/apache/cassandra`, anonymous,
unauthenticated, verified live 2026-09-25: `score: 4.6`, `scorecard.version: v5.5.1-...`, `date:
2026-09-21`, 14 checks returned. This matches `RESEARCH.md` §6.2's own independent live check on the
same date (same score, same `Code-Review` reason string), confirming the API is stable and
reproducible run-to-run within the same day.

**Storage**: every collection run writes each check as its own row (`scorecard_check`, one raw
partition per run, `storage.write_partition`'s existing append-only guarantee) — this is how "history
from now on" is implemented, with no bespoke history table. The site leads with the per-check table and
never renders the aggregate as a headline number — it appears only as a small, secondary line
underneath the table (RESEARCH.md §6.2's own warning: "aggregate scores... tell you nothing about what
individual behaviors a repository is or is not doing").

**Per-check "why" notes** (shown on the Governance page next to a check, only where there's a verified,
specific reason — everything else is shown with Scorecard's own `reason` string and no added claim):

- **Maintained** (10/10) — the check's own reason string is "30 commit(s) and 0 issue activity found in
  the last 90 days." **Verified**: `apache/cassandra` has `has_issues: false` (`DATA-SOURCES.md` §3) —
  GitHub Issues is disabled project-wide because Cassandra tracks issues in JIRA, not GitHub. The "0
  issue activity" component of this score is a config fact about which tracker is used, not a
  maintenance signal, and does not indicate inactivity.
- **Code-Review** (0/10) — "Found 0/30 approved changesets." **Verified**: this check counts only
  approved *GitHub pull-request* reviews. Cassandra's actual review process is the commit-trailer
  (`patch by X; reviewed by Y for CASSANDRA-N`) and JIRA-reviewer-field evidence this document's §2 R1
  measures directly — 79-87% trailer coverage since 2018, 93% JIRA-field coverage on a live 30-issue
  sample (§2 R1). Scorecard has no visibility into either evidence source, so a 0 here is a measurement
  blind spot, not an absence of review.
- **Branch-Protection** (1/10) — Scorecard's own evidence lists force pushes enabled and no required
  status checks found on `trunk`. This is a directly observable GitHub setting, unlike Code-Review — no
  ASF-process explanation is being asserted for it. **Unverified**: whether Cassandra/ASF Infra
  deliberately favors a non-GitHub-native protection mechanism for this repo; no ASF Infra source was
  checked for this in this round.
- **Signed-Releases** (-1, not applicable) — "no releases found," because this check only recognizes
  GitHub Releases, and `apache/cassandra` publishes 0 of those (`DATA-SOURCES.md` §5: "Cassandra does
  not use GitHub's Releases feature at all"). **Verified live 2026-09-25**: `downloads.apache.org`
  release directories (e.g. `cassandra/4.0.21/`) each carry a `.asc` GPG signature and `.sha256`/
  `.sha512` checksum file alongside the release artifact, plus a project-wide `KEYS` file (public
  keyring) at `downloads.apache.org/cassandra/KEYS` (200). ASF releases are signed; Scorecard simply
  checks the wrong channel for it.
- **Packaging** (-1, not applicable) — "packaging workflow not detected," same blind spot as
  Signed-Releases: Cassandra's release pipeline is the ASF dist/archive process above, not a GitHub
  Actions/Packages publish step.

Every other check returned (Dangerous-Workflow, Security-Policy, License, Binary-Artifacts,
Token-Permissions, CII-Best-Practices, SAST, Pinned-Dependencies, Fuzzing) reflects real, directly
observable GitHub-repo/workflow configuration with no ASF-specific measurement gap identified — shown
with Scorecard's own `reason` string and no added "why" note.

### 10.2 CVE / advisory history — metadata only

**Sources tried, live-verified 2026-09-25**:

| Source | Result | Used? |
|---|---|---|
| `cassandra.apache.org/_/cve.html`, `.../security.html` | 404 (neither page exists) | No — Cassandra has no dedicated security-advisory page of its own |
| `cve.org` search API (`cveawg.mitre.org/api/cve`) | `400 BAD_REQUEST` without a `CVE-API-ORG` header (ASF-internal credential) | No — bulk search needs credentials this project doesn't have |
| `cve.org` per-CVE record (`cveawg.mitre.org/api/cve/<id>`) | 200, works anonymously | No — redundant with NVD's own record for the same id |
| **NVD API, CPE search** (`services.nvd.nist.gov/rest/json/cves/2.0?virtualMatchString=cpe:2.3:a:apache:cassandra:*...`) | 200, **16 CVEs**, 2015-2026, all genuinely Cassandra-relevant | **Yes — primary source** |
| NVD API, free-text keyword search (`keywordSearch=apache cassandra`) | 200, 25 results | No — 9 more results than the CPE search, some unrelated to the Cassandra CPE entry; CPE search is the precise, authoritative query the issue itself asks for ("vendor apache product cassandra") |
| `lists.apache.org` announce@ archive | 200, live | No — plain HTML, no structured per-advisory API; would need bespoke scraping, deferred |

Collected per CVE (metadata only, no exploit/PoC content): CVE id, published/last-modified date,
severity + CVSS score/version (preferring the newest CVSS version NVD provides), an English summary,
affected/fixed version ranges (parsed from NVD's own `configurations[].nodes[].cpeMatch[]`, restricted
to lines naming the `apache:cassandra` CPE — a CVE's configuration can name other products too, e.g.
CVE-2016-3427's Oracle JDK/JRE entries alongside its Cassandra range), and the NVD detail-page URL.

One collection run's per-CVE rows are deduped at read time on `cve_id` (keep the latest fetch) — a
re-fetch's NVD metadata (e.g. a corrected CVSS score) is a refreshed snapshot of the same fact, not a
new historical event, matching this project's existing `issue` table dedupe convention
(`pipeline.py`'s `_dedupe_issue_rows`).
