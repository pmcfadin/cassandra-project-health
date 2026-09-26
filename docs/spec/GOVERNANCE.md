# Governance: Per-Commit Minimums

Research deliverable for issue #32 (part of Epic #31), implementing D14/D15 of `docs/spec/DECISIONS.md`.
Consistent with `docs/spec/DATA-SOURCES.md` (source access, rate limits, evidence reliability) and
`docs/spec/ARCHITECTURE.md` §3.1 (reviewer data model).

**STATUS: DRAFT. Owner approval required (D14) before any compliance result is published on
`/governance/`.** This document and `governance-policy.draft.yaml` are an agent's draft of what Cassandra's
own documented rules are and how they can be checked from public data — not yet a ratified scoring policy for
this project.

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
- **Result semantics**: `pass` if a named reviewer is found by either method; `unknown` if neither method
  finds one (never `fail` — a real review can happen with no durable public trace, e.g. informal Slack/hallway
  review, or a reviewer named only in a since-superseded JIRA comment).
- **What is explicitly NOT scored**: the "two +1 committer votes" *count* and the "must be a committer"
  *status* requirement. Neither the commit trailer nor the JIRA field states whether a named reviewer held
  committer status **at the time of review**, and no confirmed, public, dated committer-only (non-PMC) roster
  exists (`DATA-SOURCES.md` §6 — Whimsy's `committee-info.json` is PMC-only; `reporter.apache.org` is
  401/ASF-committer-gated). Scoring "was this a committer" would require guessing from current PMC/committer
  status projected backward, which is exactly the kind of unverifiable inference D15 forbids for named
  results. **Recommendation: score "named reviewer present," never "committer vote count."**

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
- **What is explicitly NOT scored**: pass/fail on CI *results* (did it succeed) — only whether evidence of a
  CI run exists. `ci-cassandra.apache.org`'s Jenkins JSON API for post-commit-by-SHA was in scope for this
  research per the issue but was not evaluated live this session (time-boxed); flagged as a follow-up before
  this check is implemented, not assumed working.

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
- **Result semantics**: `pass` if an issue key is found; `unknown` if not, because there is no reliable way
  to distinguish "should have had a ticket and didn't" from "correctly exempt as a trivial fix" without
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
- **Result semantics**: `pass` if the relevant file is touched; **`unknown`, never `fail`, if it is not** —
  the rule is conditional on "user-impacting"/"if needed," a judgment this project cannot make
  algorithmically from file paths alone. A commit that correctly omits both files is indistinguishable, from
  git alone, from one that should have included them and didn't.

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
- **Result semantics**: `pass` if a check-run exists and is green; `unknown` if no check-run exists at all
  (never `fail` for "no check-run found" — per the false-negative source above). A check-run that exists and
  is red is a legitimate `fail`, since that is direct, unambiguous evidence.

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

## 4. Rules NOT scored yet, and why

| Rule | Why not scored |
|---|---|
| **Committer status of a named reviewer / the "two +1" vote count** (R1) | No confirmed, public, dated non-PMC committer roster exists (`DATA-SOURCES.md` §6). Scoring this would require inferring committer status at a past point in time from present-day PMC/committer lists — an unverifiable backward projection, forbidden by D15's evidence standard for named results. |
| **Explicit veto / "-1 blocking" / "active reasonable discussion" rules** (ratified governance page) | Detecting a `-1` and whether it was "resolved by follow-up commits" or "not engaged with reasonably" requires reading and classifying comment *content* — gated behind Phase 2a's classifier-validation requirement (D1), not available in Phase 1's metadata-only, deterministic scope. |
| **CI *result* (pass/fail of the run itself)**, only presence of CI evidence (R2) | The `ci-cassandra.apache.org` Jenkins JSON API for post-commit-by-SHA results was named in the issue as in-scope but was not live-tested this session; do not assume its shape or reliability until it has been. |
| **"Tests required" as a universal rule** | Not a documented Cassandra rule — `patches.html` states explicitly that "the extent to which tests are required depends on how likely your changes will effect the stability of Cassandra in production," a discretionary, risk-proportional standard, not a fixed minimum. See §5 for the optional, non-authoritative signal proposed instead. |
| **CHANGES.txt / NEWS.txt absence as a fail** (R4) | Both rules are explicitly conditional ("user-impacting," "if needed"). Absence is not evidence of non-compliance without knowing whether the change was user-impacting, which this project does not classify in Phase 1. |
| **Merge-forward completeness** (§3) | Requires product knowledge (does the bug affect this branch?) no structural source can supply. Shown descriptively only, never scored. |
| **Checkstyle/CI-on-every-push for `cassandra-4.0` and earlier branches** (R5) | The rule's own source ties checkstyle-in-build to 4.1+; applying it to older branches would misattribute a rule that didn't exist there yet. |
| **Any rule for commits before 2020-06-25** where the *only* source is the ratified governance page | The page states its own ratification date. Per D14 ("Each commit is judged against the policy in force on its commit date"), R1's two-committer-vote language and R2's CI-before-commit language should not be back-applied before 2020-06-25 even though the underlying conventions (reviewer trailers, CI running) are older and continuously observed in practice — the *formal, binding* rule is dated 2020-06-25; the informal convention is undated and should not be presented as if it had the same evidentiary weight. |

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

---

## 7. Open questions for the project owner (must be answered before approval)

1. **Committer-status verification**: is it acceptable to ship R1 without ever checking committer status
   (i.e., score "named reviewer present," never "was this reviewer a committer" or "was this two votes")?
   This is the biggest gap between the ratified rule's literal text and what public data can verify.
2. **`ci-cassandra.apache.org` Jenkins API**: should this be evaluated live before Phase 1 ships, given it was
   named in the issue but not tested this session, or is JIRA-comment CI evidence (40% hit rate) an
   acceptable interim signal with the gap disclosed on the page?
3. **Historical scope**: should `unknown`-only pre-2020-06-25 commits show the ratified-rule columns at all
   (as `unknown`, since the rule didn't formally exist yet), or should those columns be hidden entirely for
   commits before the rule's effective date, to avoid an implication that the rule applied retroactively?
4. **The "client-impacting"/"doc-impacting" JIRA label cross-check** (§2 R4) was identified but not
   hit-rate-tested this session — worth a follow-up measurement before it's added as a second CHANGES.txt/
   NEWS.txt signal?
5. **Descriptive cross-branch view** (§3): does the owner want "found on branches: X, Y, Z" shown on every
   commit row from day one, or only once the collector's multi-branch ingestion (already planned per
   `DATA-SOURCES.md` §1's "trunk plus active release branches" watermark strategy) is built?
