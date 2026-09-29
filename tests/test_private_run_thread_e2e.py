"""End-to-end coverage for issue #114's thread-level metrics and newcomer
rows, exercised through the real `run_private_run` pipeline (real HTTP
mocking via `httpx`/`httpx2.MockTransport`, real Pony Mail/JIRA fetch code,
real §2.3 derivation) -- not just the unit-level `thread_derive`/
`thread_aggregate` tests. Synthetic data only.

Builds a 45-thread dev@ population, large enough to clear every §5.1
thread-level and newcomer floor at once (including the 30-*qualifying*-
threads floor on each individual rate's own denominator, not just 30
threads overall), split into four engineered groups so escalation,
resolution, abandonment, and pile-on all actually fire real, non-
`insufficient data` numbers (rather than every metric reporting `None`,
which the smaller fixtures in `test_private_run_runner.py` already cover):

- Group A (15 threads): root -> `technical_disagreement` reply -> `hostility`
  reply from a third participant. Two distinct authors each strike a new
  peak tier and the thread reaches tier 3 -> escalates; the hostile
  message's target never posts again -> abandoned.
- Group B (15 threads): root -> `technical_disagreement` reply ->
  `resolution_marker` reply. Resolves; never escalates (only one author's
  message climbs). A+B together clear the escalation rate's own 30-
  qualifying-thread floor (its denominator is threads with >=1
  `technical_disagreement` message).
- Group C (10 threads): root -> `dismissiveness` reply directed at the
  root. Reaches friction but never escalates (single climbing author);
  the root (the newcomer) never posts again -> abandoned.
- Group D (5 threads): root -> three *distinct* responders each post a
  tier >= 2 message directed at the root -> a pile-on event per thread,
  five distinct targets across the group (clears the pile-on rate's
  distinct-targets weaponization floor). A+C+D together clear the
  abandonment rate's own 30-qualifying-thread floor (its denominator is
  threads that reached tier >= 2).

Every thread's root author is unique and has no other history anywhere in
the fixture, so every reply directed at a root is a newcomer-directed
message (COMMUNITY-HEALTH.md §2.3 rule 8) -- this also exercises the
newcomer response-rate floor with a real split between constructive and
dismissive/hostile responses.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx2

from project_health.classify.questions import EXPECTED_MODEL, MESSAGE_LEVEL_LABELS
from project_health.private_run.runner import run_private_run
from tests.test_private_run_runner import (
    _jira_paginating_transport,
    _mail_record,
    _ponymail_transport,
    _project_config,
    _write_issue,
    _write_message,
    _write_message_thread,
)


def _ts(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# Marker -> which label this fixture wants Jev to report "present" (high
# probability) for. Any text without a recognized marker gets a uniformly
# low probability on every label (tier 0, neutral) -- the thread roots.
_MARKER_LABELS: dict[str, str] = {
    "MARK_TECH": "technical_disagreement",
    "MARK_HOSTILE": "hostility",
    "MARK_RESOLVE": "resolution_marker",
    "MARK_DISMISS": "dismissiveness",
}


def _marker_jev_transport() -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        text = payload["state"]["message"]["text"]
        target_label = next(
            (label for marker, label in _MARKER_LABELS.items() if marker in text), None
        )
        answers = {
            label: {"type": "noul", "noul": 0.9 if label == target_label else 0.01}
            for label in MESSAGE_LEVEL_LABELS
        }
        answers["tone_intensity"] = {
            "type": "score",
            "score": 1.0,
            "confidence": 0.8,
            "legend": {0: "neutral", 1: "firm", 2: "sharp", 3: "heated", 4: "aggressive"},
            "probabilities": {0: 0.1, 1: 0.6, 2: 0.2, 3: 0.05, 4: 0.05},
        }
        body = {
            "model": EXPECTED_MODEL,
            "usage": {"input_tokens": 20, "output_tokens": 5},
            "answers": answers,
        }
        return httpx2.Response(200, json=body)

    return httpx2.MockTransport(handler)


def _build_fixture(tmp_path):
    data_dir = tmp_path / "data"
    thread_rows = []
    message_rows = []
    mail_emails = []

    def _add_message(msg_id, sender, occurred_at, thread_id, body, in_reply_to=None):
        message_rows.append(
            {
                "message_id": msg_id,
                "sender_raw_value": sender,
                "occurred_at": occurred_at,
                "thread_id": thread_id,
                "in_reply_to": in_reply_to,
            }
        )
        mail_emails.append(_mail_record(msg_id, sender, body, in_reply_to=in_reply_to or ""))

    # Group A: escalates, ends abandoned.
    for i in range(15):
        thread_id = f"thA{i}"
        thread_rows.append({"thread_id": thread_id, "started_at": _ts(2024, 1, 1)})
        root_id = f"<a-root-{i}@x>"
        mid_id = f"<a-mid-{i}@x>"
        hot_id = f"<a-hot-{i}@x>"
        _add_message(root_id, f"rootA{i}@example.org", _ts(2024, 1, 1, 0, 0), thread_id, "ROOT")
        _add_message(
            mid_id, "responderX@example.org", _ts(2024, 1, 1, 0, 1), thread_id, "MARK_TECH", root_id
        )
        _add_message(
            hot_id,
            "responderY@example.org",
            _ts(2024, 1, 1, 0, 2),
            thread_id,
            "MARK_HOSTILE",
            mid_id,
        )

    # Group B: resolves, never escalates.
    for i in range(15):
        thread_id = f"thB{i}"
        thread_rows.append({"thread_id": thread_id, "started_at": _ts(2024, 1, 2)})
        root_id = f"<b-root-{i}@x>"
        mid_id = f"<b-mid-{i}@x>"
        resolve_id = f"<b-resolve-{i}@x>"
        _add_message(root_id, f"rootB{i}@example.org", _ts(2024, 1, 2, 0, 0), thread_id, "ROOT")
        _add_message(
            mid_id, "responderX@example.org", _ts(2024, 1, 2, 0, 1), thread_id, "MARK_TECH", root_id
        )
        _add_message(
            resolve_id,
            "responderY@example.org",
            _ts(2024, 1, 2, 0, 2),
            thread_id,
            "MARK_RESOLVE",
            mid_id,
        )

    # Group C: reaches friction directed at the (newcomer) root, root never
    # replies again -> abandoned.
    for i in range(10):
        thread_id = f"thC{i}"
        thread_rows.append({"thread_id": thread_id, "started_at": _ts(2024, 1, 3)})
        root_id = f"<c-root-{i}@x>"
        dismiss_id = f"<c-dismiss-{i}@x>"
        _add_message(root_id, f"rootC{i}@example.org", _ts(2024, 1, 3, 0, 0), thread_id, "ROOT")
        _add_message(
            dismiss_id,
            "responderZ@example.org",
            _ts(2024, 1, 3, 0, 1),
            thread_id,
            "MARK_DISMISS",
            root_id,
        )

    # Group D: three distinct responders pile on the (newcomer) root.
    for i in range(5):
        thread_id = f"thD{i}"
        thread_rows.append({"thread_id": thread_id, "started_at": _ts(2024, 1, 4)})
        root_id = f"<d-root-{i}@x>"
        _add_message(root_id, f"rootD{i}@example.org", _ts(2024, 1, 4, 0, 0), thread_id, "ROOT")
        for j, attacker in enumerate(("attacker1", "attacker2", "attacker3")):
            reply_id = f"<d-{attacker}-{i}@x>"
            _add_message(
                reply_id,
                f"{attacker}@example.org",
                _ts(2024, 1, 4, 0, j + 1),
                thread_id,
                "MARK_DISMISS",
                root_id,
            )

    _write_message_thread(data_dir, thread_rows)
    _write_message(data_dir, message_rows)
    _write_issue(data_dir, [{"issue_key": "EXAMPLE-1", "created_at": _ts(2024, 1, 1)}])

    ponymail_months = {"2024-01": {"hits": len(mail_emails), "emails": mail_emails}}
    jira_comments = {"EXAMPLE-1": []}
    return data_dir, ponymail_months, jira_comments


class TestThreadLevelMetricsRealNumbers:
    def test_full_pipeline_produces_non_insufficient_thread_and_newcomer_numbers(self, tmp_path):
        config = _project_config(tmp_path)
        data_dir, ponymail_months, jira_comments = _build_fixture(tmp_path)
        out_dir = tmp_path / "out"

        result = run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            k=60,
            api_key="test-key",
            monthly_cap_usd=1000.0,
            bootstrap_iterations=30,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
            jev_async_transport=_marker_jev_transport(),
        )

        metrics = result.aggregates["thread_metrics_by_year"]["mailing_list"]["2024"]["0.5"]
        assert metrics["threads_total"] == 45

        escalation = metrics["escalation_rate"]
        assert escalation["insufficient_data"] is False
        # Only group A's 15 threads escalate, out of the 30 threads (A+B)
        # that have a technical_disagreement message at all.
        assert abs(escalation["rate"] - 0.5) < 1e-9

        resolution = metrics["constructive_resolution_rate"]
        assert resolution["insufficient_data"] is False
        assert 0.0 < resolution["rate"] < 1.0

        abandonment = metrics["thread_abandonment_rate_post_friction"]
        assert abandonment["insufficient_data"] is False
        assert abandonment["rate"] > 0.0

        pile_on = metrics["pile_on_rate"]
        assert pile_on["insufficient_data"] is False
        assert pile_on["distinct_targets"] == 5  # group D's 5 distinct roots
        assert pile_on["per_100_threads"] > 0.0

        newcomer = result.aggregates["newcomer_by_year"]["mailing_list"]["2024"]
        assert newcomer["insufficient_data"] is False
        # A/B/C/D's "reply directed at the thread root" messages: 15+15+10+15.
        assert newcomer["messages_directed_at_newcomers"] == 55
        assert newcomer["distinct_newcomers"] == 45
        assert (
            newcomer["dismissive_hostile_response_rate"] > newcomer["constructive_response_rate"]
        )
        # no ack/compromise ever targets a root
        assert newcomer["constructive_response_rate"] == 0.0

        report_text = result.report_path.read_text(encoding="utf-8")
        assert "insufficient data" in report_text  # jira_comment venue still is
        # The mailing_list thread-level table renders real (non-
        # "insufficient data") formatted rates -- not every section
        # collapsed to `insufficient data`.
        assert "### mailing_list -- thread-level metrics (§2.3, >= 0.5), by year" in report_text
        assert "0.500 [" in report_text  # the escalation rate point estimate
        assert "1.000 [1.000, 1.000]" in report_text  # the abandonment rate

        # No raw identity/text ever leaks, even with this much richer fixture.
        raw_aggregates = result.aggregates_path.read_text(encoding="utf-8")
        for forbidden in ("rootA0@example.org", "responderX@example.org", "MARK_HOSTILE", "ROOT"):
            assert forbidden not in raw_aggregates
        for forbidden in ("rootA0@example.org", "responderX@example.org", "MARK_HOSTILE"):
            assert forbidden not in report_text
