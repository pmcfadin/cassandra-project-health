"""End-to-end coverage for issue #122 (D27): `run_private_run` writes
`--out/threads.jsonl` with public, D26/D27-allowlist-safe rows -- a real
Pony Mail thread permalink (never the raw dev@ Message-ID) and subject for
dev@ threads, a JIRA issue-tracker browse URL and `summary` for JIRA
"threads". Fully offline, same mocking pattern as
`test_private_run_thread_e2e.py`.
"""

from __future__ import annotations

import json

import httpx
import pyarrow as pa

from project_health import storage
from project_health.private_run.runner import run_private_run
from tests.test_private_run_runner import (
    _jev_transport,
    _jira_comment,
    _jira_paginating_transport,
    _project_config,
    _ts,
    _write_issue,
    _write_message,
)


def _write_message_thread(data_dir, rows: list[dict]) -> None:
    """Local variant that lets each row set its own `root_message_id`
    (`test_private_run_runner.py`'s own helper always derives it as
    `thread_id + "-root"`, which doesn't line up with a real message id
    this test also needs to appear in the Pony Mail digest fixture)."""
    table = pa.table(
        {
            "thread_id": pa.array([r["thread_id"] for r in rows], type=pa.string()),
            "list": pa.array(["dev"] * len(rows), type=pa.string()),
            "root_message_id": pa.array([r["root_message_id"] for r in rows], type=pa.string()),
            "started_at": pa.array(
                [r["started_at"] for r in rows], type=pa.timestamp("us", tz="UTC")
            ),
            "last_activity_at": pa.array(
                [r["started_at"] for r in rows], type=pa.timestamp("us", tz="UTC")
            ),
            "message_count": pa.array([r.get("message_count", 1) for r in rows], type=pa.int64()),
            "source_snapshot_id": pa.array(["snap-1"] * len(rows), type=pa.string()),
        }
    )
    storage.write_partition(data_dir, "ponymail", "message_thread", "2026-09-25", "run-1", table)


def _mail_record(
    message_id: str, sender: str, body: str, subject: str, in_reply_to: str = ""
) -> dict:
    return {
        "message-id": message_id,
        "mid": message_id.strip("<>").replace("@", "-"),
        "from": sender,
        "in-reply-to": in_reply_to,
        "subject": subject,
        "body": body,
    }


def _ponymail_transport(months: dict) -> httpx.MockTransport:
    from urllib.parse import parse_qs

    def handler(request: httpx.Request) -> httpx.Response:
        params = parse_qs(request.url.query.decode())
        year_month = params["d"][0]
        if year_month not in months:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=months[year_month])

    return httpx.MockTransport(handler)


def _fixture(tmp_path, project_key: str = "EXAMPLE"):
    data_dir = tmp_path / "data"
    _write_message_thread(
        data_dir,
        [{"thread_id": "t1", "root_message_id": "<m1@example.org>", "started_at": _ts(2024, 1, 1)}],
    )
    _write_message(
        data_dir,
        [
            {
                "message_id": "<m1@example.org>",
                "sender_raw_value": "alice@example.org",
                "occurred_at": _ts(2024, 1, 1, 9, 0, 0),
                "thread_id": "t1",
                "in_reply_to": None,
            },
            {
                "message_id": "<m2@example.org>",
                "sender_raw_value": "bob@example.org",
                "occurred_at": _ts(2024, 1, 1, 10, 0, 0),
                "thread_id": "t1",
                "in_reply_to": "<m1@example.org>",
            },
        ],
    )
    _write_issue(
        data_dir,
        [{"issue_key": f"{project_key}-1", "created_at": _ts(2024, 1, 2)}],
    )
    ponymail_months = {
        "2024-01": {
            "hits": 2,
            "emails": [
                _mail_record(
                    "<m1@example.org>",
                    "Alice <alice@example.org>",
                    "opening message",
                    "[DISCUSS] A public thread",
                ),
                _mail_record(
                    "<m2@example.org>",
                    "Bob <bob@example.org>",
                    "a reply",
                    "Re: [DISCUSS] A public thread",
                    in_reply_to="<m1@example.org>",
                ),
            ],
        }
    }
    jira_comments = {
        f"{project_key}-1": [
            _jira_comment("9001", "carol", "2024-01-03T10:00:00.000+0000", "a comment"),
        ]
    }
    return data_dir, ponymail_months, jira_comments


class TestThreadsJsonlWiring:
    def test_writes_threads_jsonl_with_public_urls_and_subjects(self, tmp_path):
        project_key = "EXAMPLE"
        config = _project_config(tmp_path, project_key=project_key)
        data_dir, ponymail_months, jira_comments = _fixture(tmp_path, project_key)
        out_dir = tmp_path / "out"

        result = run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            seed=1,
            k=60,
            api_key="test-key",
            monthly_cap_usd=1000.0,
            bootstrap_iterations=5,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
            jev_async_transport=_jev_transport(probability=0.6),
        )

        assert result.threads_path is not None
        assert result.threads_path == out_dir / "threads.jsonl"
        lines = result.threads_path.read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines]
        assert len(rows) == 2  # one dev@ thread, one JIRA "thread"

        by_venue = {row["venue"]: row for row in rows}

        dev_row = by_venue["mailing_list"]
        # The public thread_key/url must be Pony Mail's own opaque `mid`
        # (derived from "<m1@example.org>" -> "m1-example.org" by
        # `_mail_record`), never the raw Message-ID itself.
        assert dev_row["thread_key"] == "m1-example.org"
        assert dev_row["url"] == "https://lists.apache.org/thread/m1-example.org"
        assert dev_row["subject"] == "[DISCUSS] A public thread"
        assert dev_row["quarter"] == "2024Q1"
        assert dev_row["n_messages"] == 2
        assert dev_row["n_distinct_participants"] == 2
        assert "<m1@example.org>" not in json.dumps(dev_row)
        assert "alice@example.org" not in json.dumps(dev_row)

        jira_row = by_venue["jira_comment"]
        assert jira_row["thread_key"] == f"{project_key}-1"
        assert jira_row["url"] == f"https://issues.example.org/jira/browse/{project_key}-1"
        assert jira_row["quarter"] == "2024Q1"
        assert jira_row["n_messages"] == 1

        for row in rows:
            assert set(row["outcome"]) == {
                "escalation",
                "deescalation",
                "constructive_resolution",
                "abandonment_after_friction",
                "pile_on",
            }
            assert isinstance(row["peak_intensity_tier"], int)
            assert isinstance(row["label_counts"], dict)

    def test_threads_jsonl_rebuilt_from_scratch_each_run(self, tmp_path):
        project_key = "EXAMPLE"
        config = _project_config(tmp_path, project_key=project_key)
        data_dir, ponymail_months, jira_comments = _fixture(tmp_path, project_key)
        out_dir = tmp_path / "out"
        kwargs = dict(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            seed=1,
            k=60,
            api_key="test-key",
            monthly_cap_usd=1000.0,
            bootstrap_iterations=5,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
            jev_async_transport=_jev_transport(probability=0.6),
        )
        run_private_run(**kwargs)
        result2 = run_private_run(**kwargs)
        lines = result2.threads_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2  # not doubled by a second run
