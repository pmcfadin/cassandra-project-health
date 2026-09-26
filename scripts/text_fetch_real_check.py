#!/usr/bin/env python3
"""Live, in-memory-only check for the Phase 2a text pipeline (issue #43).

This is a **script**, not a test -- it makes real network calls (dev@ Pony
Mail, ASF JIRA, and one real TypeSafe Jev call) and is deliberately kept out
of `tests/` so the offline test suite (`tests/conftest.py` blocks all real
network access) never depends on it. Run it manually:

    .venv/bin/python scripts/text_fetch_real_check.py

What it does, per issue #43's "Real check" requirement:

1. Discovers 10 recent real dev@ message ids and 10 recent real JIRA comment
   ids (a small amount of read-only discovery this script does itself --
   `classify/text_fetch.py`'s fetchers only fetch bodies for ids they're
   *given*, by design; discovering which ids to ask for is the caller's job,
   normally the already-collected Phase 1 metadata).
2. Fetches and preprocesses all 20 messages **in memory only** via
   `classify.text_fetch`/`classify.preprocess` -- nothing is written to disk
   anywhere.
3. Prints aggregate stats only (lengths before/after, how many had
   quotes/code/signatures stripped, how many parents resolved) -- never the
   message text itself.
4. Runs exactly one of the fetched, preprocessed messages through the real
   Jev question set (`classify/questions_v1.yaml`) via `typesafe_sdk`, using
   `TYPESAFE_API_KEY` sourced from the repo's gitignored `.env` (`jev_key=`)
   without ever printing the key. Prints only the returned label
   probabilities -- never the text.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_health.classify.preprocess import is_automated_sender  # noqa: E402
from project_health.classify.questions import load_question_set  # noqa: E402
from project_health.classify.text_fetch import (  # noqa: E402
    JiraCommentRef,
    JiraCommentTextFetcher,
    MailMessageRef,
    PonyMailTextFetcher,
    fetch_and_preprocess_jira,
    fetch_and_preprocess_mail,
)

DEV_LIST = "dev"
DEV_DOMAIN = "cassandra.apache.org"
JIRA_BASE_URL = "https://issues.apache.org/jira"
N_MESSAGES = 10

# Same placeholder set as projects/cassandra.yaml's automated_senders, kept
# inline here too so this script's discovery step (which bypasses
# ProjectConfig for simplicity) still filters automated senders the same way
# `fetch_and_preprocess_mail`/`fetch_and_preprocess_jira` do downstream.
AUTOMATED_SENDER_PATTERNS = [
    r"(?i)^jira@|@jira\.apache\.org$",
    r"(?i)^git@|^commits?@",
    r"(?i)noreply@github\.com$|^notifications@github\.com$",
    r"(?i)\[bot\]|-bot$|^dependabot",
    r"(?i)^hudson@|^jenkins@|^ci@|^builds?@",
    r"(?i)^svn-role$|^git-role$",
]


def _load_jev_key_from_dotenv() -> None:
    """Read `jev_key=` from the repo's gitignored `.env` and set
    `TYPESAFE_API_KEY` from it, without ever printing the value.

    Looks in the current worktree first, then falls back to the main
    checkout (via `git rev-parse --git-common-dir`) -- a git worktree
    doesn't share untracked/gitignored files like `.env` with the main
    checkout, so a worktree-run of this script needs the fallback.
    """
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env"]
    try:
        common_dir = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        candidates.append(Path(common_dir).resolve().parent / ".env")
    except Exception:
        pass

    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "jev_key":
                os.environ["TYPESAFE_API_KEY"] = value.strip()
                return
    checked = [str(c) for c in candidates]
    raise RuntimeError(f"jev_key not found in any .env file (checked: {checked})")


def _discover_recent_mail_refs(n: int) -> list[MailMessageRef]:
    """The last `n` message ids in the current calendar month's dev@ digest.

    Real network call. This discovery step is intentionally simple (one
    recent month) -- the pilot's actual sampling (D18) is a separate,
    already-scoped concern; this script only needs *some* recent real ids to
    exercise the fetch/preprocess pipeline end to end.
    """
    from datetime import datetime, timezone

    year_month = datetime.now(timezone.utc).strftime("%Y-%m")
    with httpx.Client(base_url="https://lists.apache.org", timeout=30.0) as client:
        response = client.get(
            "/api/stats.lua", params={"list": DEV_LIST, "domain": DEV_DOMAIN, "d": year_month}
        )
        response.raise_for_status()
        emails = response.json().get("emails", [])
    emails = [e for e in emails if e.get("message-id")]
    chosen = emails[-n:] if len(emails) >= n else emails
    return [
        MailMessageRef(DEV_LIST, DEV_DOMAIN, year_month, e["message-id"]) for e in chosen
    ]


def _discover_recent_jira_refs(n: int) -> list[JiraCommentRef]:
    """The last `n` comments (across a handful of recently updated issues
    with comments) on the CASSANDRA JIRA project. Real network call."""
    jql = "project=CASSANDRA ORDER BY updated DESC"
    with httpx.Client(base_url=JIRA_BASE_URL, timeout=30.0) as client:
        search = client.get(
            "/rest/api/2/search",
            params={"jql": jql, "fields": "key", "maxResults": 25},
        )
        search.raise_for_status()
        issue_keys = [issue["key"] for issue in search.json().get("issues", [])]

        refs: list[JiraCommentRef] = []
        for key in issue_keys:
            if len(refs) >= n:
                break
            resp = client.get(
                f"/rest/api/2/issue/{key}/comment",
                params={"orderBy": "-created", "maxResults": n - len(refs)},
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            for comment in resp.json().get("comments", []):
                refs.append(JiraCommentRef(key, str(comment["id"])))
                if len(refs) >= n:
                    break
    return refs


def _has_quote_markers(raw_text: str) -> bool:
    has_gt_line = any(line.strip().startswith(">") for line in raw_text.splitlines())
    return has_gt_line or "wrote:" in raw_text


def _has_code_or_stacktrace_markers(raw_text: str) -> bool:
    return (
        "```" in raw_text
        or "{code" in raw_text
        or "{noformat}" in raw_text
        or "Traceback (most recent call last):" in raw_text
        or "\tat " in raw_text
    )


def _has_signature_marker(raw_text: str) -> bool:
    return any(line.strip() in ("--", "-- ") for line in raw_text.splitlines())


def main() -> None:
    _load_jev_key_from_dotenv()
    assert os.environ.get("TYPESAFE_API_KEY"), "TYPESAFE_API_KEY must be set (never printed)"

    print("Discovering recent real dev@ and JIRA ids (metadata only)...")
    mail_refs = _discover_recent_mail_refs(N_MESSAGES)
    jira_refs = _discover_recent_jira_refs(N_MESSAGES)
    print(f"  dev@ refs discovered: {len(mail_refs)}")
    print(f"  JIRA comment refs discovered: {len(jira_refs)}")

    mail_fetcher = PonyMailTextFetcher()
    jira_fetcher = JiraCommentTextFetcher(JIRA_BASE_URL)

    # Raw (pre-preprocessing) fetch, for the "before" stats -- reuses the
    # same fetchers/caches `fetch_and_preprocess_*` will reuse below.
    raw_mail = mail_fetcher.fetch_messages(mail_refs)
    raw_jira = jira_fetcher.fetch_comments(jira_refs)

    mail_states = fetch_and_preprocess_mail(
        mail_fetcher, mail_refs, automated_sender_patterns=AUTOMATED_SENDER_PATTERNS
    )
    jira_states = fetch_and_preprocess_jira(
        jira_fetcher, jira_refs, automated_sender_patterns=AUTOMATED_SENDER_PATTERNS
    )

    def _report(label: str, raw_by_id: dict, states: dict, sender_of) -> None:
        n_fetched = len(raw_by_id)
        n_automated = sum(
            1
            for raw in raw_by_id.values()
            if is_automated_sender(sender_of(raw), AUTOMATED_SENDER_PATTERNS)
        )
        n_kept = len(states)
        quote_count = 0
        code_count = 0
        sig_count = 0
        parent_count = 0
        len_before = []
        len_after = []
        for msg_id, raw in raw_by_id.items():
            if msg_id not in states:
                continue
            raw_text = raw.text
            len_before.append(len(raw_text))
            len_after.append(len(states[msg_id]["message"]["text"]))
            if _has_quote_markers(raw_text):
                quote_count += 1
            if _has_code_or_stacktrace_markers(raw_text):
                code_count += 1
            if _has_signature_marker(raw_text):
                sig_count += 1
            if states[msg_id]["parent"] is not None:
                parent_count += 1

        avg_before = sum(len_before) / len(len_before) if len_before else 0
        avg_after = sum(len_after) / len(len_after) if len_after else 0
        print(f"\n--- {label} ---")
        print(f"  fetched: {n_fetched}, automated (dropped): {n_automated}, kept: {n_kept}")
        print(f"  avg length before: {avg_before:.0f} chars, after: {avg_after:.0f} chars")
        print(f"  had quoted text stripped: {quote_count}/{n_kept}")
        print(f"  had code/stacktrace markers: {code_count}/{n_kept}")
        print(f"  had a signature block: {sig_count}/{n_kept}")
        print(f"  parents resolved: {parent_count}/{n_kept}")

    _report("dev@ (mailing_list)", raw_mail, mail_states, lambda raw: raw.sender)
    _report("JIRA comments", raw_jira, jira_states, lambda raw: raw.author)

    # --- One message through the real Jev question set --------------------
    sample_state = next(iter(mail_states.values()), None) or next(iter(jira_states.values()), None)
    if sample_state is None:
        print("\nNo preprocessed message available to run through Jev.")
        return

    from typesafe_sdk import TypeSafeClient

    question_set = load_question_set()
    print(f"\nRunning one {sample_state['message']['source']} message through Jev "
          f"({question_set.model}, question_set v{question_set.version})...")
    with TypeSafeClient() as client:
        response = client.system_one(state=sample_state, questions=question_set.build_questions())

    print(f"\nmodel_id (as reported by TypeSafe): {response.model}")
    print("label probabilities:")
    for label_id in sorted(question_set.labels):
        noul = response.nouls.get(label_id)
        if noul is not None:
            print(f"  {label_id}: {noul.noul:.3f}")
    score = response.scores.get("tone_intensity")
    if score is not None:
        print(f"  tone_intensity: score={score.score:.3f} confidence={score.confidence:.3f}")


if __name__ == "__main__":
    main()
