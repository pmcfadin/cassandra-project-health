"""`/corrections/` page context builder (issue #160).

A single, on-site front door for every correction/feedback path this
project already has, plus a general "data correction" catch-all for
anything the four specific templates don't cover. Nothing here changes
how a correction is actually processed (still a PR-reviewed, human change
to `identity_overrides.yaml`/`governance_overrides.yaml`/config, per D15,
D19, COMMUNITY-HEALTH.md §7.6) -- this page only explains the path and
links to it.

Linked from the footer of every page (`templates/base.html`) and from
every existing "report an issue"/feedback link on the site
(`community.html`'s leaderboard corrections link and per-row "Report an
issue" link, `conversations.html`'s conversation-patterns feedback link).
"""

from __future__ import annotations

from dataclasses import dataclass

REPO_URL = "https://github.com/pmcfadin/cassandra-project-health"
ISSUES_NEW_URL = f"{REPO_URL}/issues/new"
OPEN_QUESTIONS_URL = f"{REPO_URL}/blob/main/docs/spec/OPEN-QUESTIONS.md"
IDENTITY_OVERRIDES_URL = f"{REPO_URL}/blob/main/identity_overrides.yaml"
GOVERNANCE_OVERRIDES_URL = f"{REPO_URL}/blob/main/governance_overrides.yaml"


@dataclass(frozen=True)
class CorrectionPath:
    """One row of the "How to request a correction" list -- an existing
    GitHub issue-form template, or the page's own general catch-all."""

    title: str
    description: str
    template_url: str


# The three existing per-topic templates (D15, D19, D26) plus the new
# general "data correction" template this issue adds
# (`.github/ISSUE_TEMPLATE/data-correction.yml`) for anything that isn't
# an identity merge, a governance check result, or conversation-patterns
# feedback -- e.g. a wrong release date, a miscounted metric, a broken
# link. Governance's own per-check correction path (`governance-
# correction.yml`) isn't yet linked from a page row (no "Report an issue"
# link exists on /governance/ today), so it's listed here directly instead
# of only via a page link, same as the other three.
CORRECTION_PATHS: tuple[CorrectionPath, ...] = (
    CorrectionPath(
        title="Identity correction",
        description=(
            "Two raw identifiers (a git email, a commit-trailer name, a JIRA "
            "username) should be merged as one person, or an existing merge is "
            "wrong -- affects the contributor leaderboard and every contributor-"
            "count metric."
        ),
        template_url=f"{ISSUES_NEW_URL}?template=identity-correction.yml",
    ),
    CorrectionPath(
        title="Governance check correction",
        description=(
            "One commit's governance check result on /governance/ (reviewer "
            "present, ticket referenced, CI evidence, checkstyle) is wrong."
        ),
        template_url=f"{ISSUES_NEW_URL}?template=governance-correction.yml",
    ),
    CorrectionPath(
        title="Thread disagreement",
        description=(
            "A specific thread's row on /conversations/threads/ (outcome, peak "
            "intensity, flagged-message counts) looks wrong."
        ),
        template_url=f"{ISSUES_NEW_URL}?template=thread-score-disagreement.yml",
    ),
    CorrectionPath(
        title="Conversation patterns feedback",
        description=(
            "Feedback on the preliminary, classified /conversations/ "
            "\"Conversation patterns\" section -- any part of it."
        ),
        template_url=f"{ISSUES_NEW_URL}?template=conversation-patterns-feedback.yml",
    ),
    CorrectionPath(
        title="Data correction (general)",
        description=(
            "Anything else: a wrong number, a stale value, a broken link, a "
            "miscollected fact that doesn't fit one of the paths above."
        ),
        template_url=f"{ISSUES_NEW_URL}?template=data-correction.yml",
    ),
)


def build_corrections_context() -> dict:
    """Everything `templates/corrections.html` needs. Static -- no
    `data_dir`/snapshot read."""
    return {
        "correction_paths": CORRECTION_PATHS,
        "open_questions_url": OPEN_QUESTIONS_URL,
        "identity_overrides_url": IDENTITY_OVERRIDES_URL,
        "governance_overrides_url": GOVERNANCE_OVERRIDES_URL,
    }


__all__ = ["CorrectionPath", "build_corrections_context"]
