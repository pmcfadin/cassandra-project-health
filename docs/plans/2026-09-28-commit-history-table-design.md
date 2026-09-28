# Governance: commit history table (design)

Owner-agreed 2026-09-28. This replaces the Governance page's "Currently failing" list and "Per-commit detail" table with one definitive view: **the commit history, with the evidence for each policy requirement.** For every commit and every requirement, a cell answers "did it happen?" and, if it did, what it was.

## Amendment (2026-09-28, same day): informational stance, not a verdict

Later the same day the owner narrowed this design before implementation: "Let's not publish any policy on our website. That's up to the project. Let's get more in an informational stance and let project maintainers judge." The site must never render a pass/fail/unknown/exempt/not_in_force verdict, a "Currently failing" section, a Result filter, a correction-request link tied to a verdict, a "pass rate" headline, or good/bad-colored trend lines. The backend (`governance-policy.yaml`, `governance/checks.py`, `governance/engine.py`) keeps scoring every commit exactly as documented below — that scoring is still what the structured-evidence fields (`state`, `evidence_kind`, `evidence_label`, `evidence_at`, `lead_time_seconds`, `reason`) are derived from — but everything below this note describes the pre-amendment page shape; the shipped page instead shows, per column, what the public record states as a plain fact ("S. Tunnicliffe (trailer)" / "none named"; "ci_summary attached, 2 days before commit" / "none found"; "ant-check-jdk11 success/failure" / "no check-run recorded"), tags a commit "docs-only change" / "release-process commit" / "declares ninja" neutrally rather than as an exemption, and closes with a References section linking the project's own published guidance instead of quoting it as a rule this project enforces. The default range, the older-history toggle, hiding forward-merge commits, sortable columns with URL-persisted filters/sort, and CSV/JSON export of the filtered set are unchanged by this amendment. See `src/project_health/site/governance_page.py`'s module docstring for the implementation of this amendment (issue #97).

## Scope and range
- One row per commit. The default range starts at **2020-06-25**, when the governance rules were ratified.
- A toggle, "Include history before 2020-06-25 (rules not yet ratified)", loads the older history. It is off by default so the older commits don't clutter the results.
- Forward-merge commits are hidden by default, with a toggle to show them. They repeat a patch already shown on its original branch (GOVERNANCE.md §3).
- The trend cards and the policy header stay above the table.

## Columns
| Column | Content |
|---|---|
| Date | Commit date (UTC). Sortable. |
| Commit | Short SHA linked to GitHub, the subject line, and the author underneath. |
| Ticket | CASSANDRA key(s) linked to JIRA, or "—". |
| **Reviewed by** | Requirement: reviewer-present. Reviewer names and where each came from (trailer, JIRA field). |
| **CI results provided** | Requirement: pre-commit-ci-evidence. What was found (attachment name or comment link) and how long before the commit. |
| **CI artefacts on JIRA** | Requirement: ci-artefacts-attached. Which artefacts are attached and when, or which are missing. |
| **Checkstyle** | Requirement: code-style-checkstyle. The check-run name and its conclusion, linked. |
| CHANGES.txt | Informational only: "included" or "—". Not a requirement, because the docs require it only for user-impacting changes. |
| NEWS.txt | Informational only: "included" or "—". |

Optional columns, hidden by default: branch, committer, tests touched, merge commit, and whether the message declares "ninja".

## Cell states (one visual language across all four requirement columns)
- **Met ✓:** the evidence itself, e.g. "S. Tunnicliffe (trailer)", "ci_summary + results_details, 5 h before commit", "ant-check-jdk11 green".
- **Missing ✗:** exactly what is missing, e.g. "no reviewer named", "results_details missing (ci_summary attached 09-19)", "evidence posted 2 days after commit".
- **Unverified ?:** why it couldn't be checked, e.g. "ticket not checked yet (backfill)", "no ticket referenced", "check-run older than GitHub retention".
- **Not required –:** the policy reason, e.g. "Commit Then Review (docs-only)", "release process", "not in force before 2026-08-19".

## Filters and sorting
- **Result:** Pass / Fail / Unverified across the four requirements.
  - Fail means any requirement is missing.
  - Unverified means none is missing but at least one is unverified.
  - Pass means every requirement is met or not required.
- **Per requirement:** met / missing / unverified / not required.
- Also: branch, date range, author, and free-text search over SHA, ticket and subject.
- Every column is sortable. Filters and sort are kept in the URL so a view can be shared.
- CSV and JSON export whatever is currently filtered.
- The old failing list becomes the filter Result = Fail, and the page offers a quick link to it.

## Row expansion
Expanding a row shows the full evidence trail. For each requirement it shows the policy quote and source link, each piece of evidence with its timestamp and link (trailer line, JIRA comment or attachment, check-run), and "Request a correction" when anything is missing.

## Data
The evidence is stored today mostly as a free-text string. Add structured fields per commit × requirement:
- `state` (met, missing, unverified, not_required)
- `evidence_kind`, `evidence_label`, `evidence_url`, `evidence_at`
- `lead_time_seconds` (evidence_at relative to the commit)
- `reason`
- for the reviewer requirement, reviewer names with their source

Everything needed is already collected. Downloads keep the existing columns and add the new ones.

## Performance and mobile
- About 5,000 non-merge commits since 2020. Load the default range as one JSON file, filter on the client, and page the table. Load the older history only when the toggle is on.
- On a narrow screen each row collapses to a card with the four requirements as labeled lines.
