"""Tests for project_health.private_run.runner (issue #110).

Fully offline: dev@/JIRA text fetches go through injected `httpx.
MockTransport`s (synthetic bodies throughout, never real Cassandra text),
the Jev classifier call goes through an injected `httpx2.MockTransport`
(mirroring `tests/test_classifier.py`'s own pattern), and the sampling
frame comes from tiny synthetic Parquet partitions written via
`project_health.storage.write_partition`. `tests/conftest.py`'s suite-wide
network block would fail any real request regardless.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import httpx2
import pyarrow as pa
import yaml

from project_health import storage
from project_health.classify.questions import EXPECTED_MODEL, MESSAGE_LEVEL_LABELS
from project_health.config import ProjectConfig, load_project
from project_health.private_run.runner import (
    MAX_MESSAGES_PER_THREAD,
    collect_dev_pending,
    collect_jira_pending,
    run_private_run,
)
from project_health.private_run.sample import StratumSample

# --- Fixtures / helpers ------------------------------------------------------


def _ts(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _project_config(tmp_path: Path, *, project_key: str = "EXAMPLE") -> ProjectConfig:
    config_path = tmp_path / "project.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {"id": "example", "display_name": "Example"},
                "reviewer_extraction": {
                    "commit_trailer": {"type": "commit_message_regex", "pattern": ".*"},
                    "jira_fields": {"type": "jira_custom_field"},
                },
                "mailing_lists": {
                    "type": "ponymail",
                    "domain": "example.org",
                    "lists": ["dev", "user"],
                },
                "issue_tracker": {
                    "type": "jira",
                    "base_url": "https://issues.example.org/jira",
                    "project_key": project_key,
                },
                "automated_senders": [{"regex": r"(?i)jenkins@|-bot$", "note": "CI/bot traffic"}],
            }
        )
    )
    return load_project(config_path)


def _write_message_thread(data_dir, rows: list[dict]) -> None:
    table = pa.table(
        {
            "thread_id": pa.array([r["thread_id"] for r in rows], type=pa.string()),
            "list": pa.array(["dev"] * len(rows), type=pa.string()),
            "root_message_id": pa.array([r["thread_id"] + "-root" for r in rows], type=pa.string()),
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


def _write_message(data_dir, rows: list[dict]) -> None:
    table = pa.table(
        {
            "message_id": pa.array([r["message_id"] for r in rows], type=pa.string()),
            "list": pa.array(["dev"] * len(rows), type=pa.string()),
            "sender_identity_id": pa.array([None] * len(rows), type=pa.string()),
            "sender_raw_type": pa.array(["mailing_list_address"] * len(rows), type=pa.string()),
            "sender_raw_value": pa.array([r["sender_raw_value"] for r in rows], type=pa.string()),
            "sender_display_name": pa.array([None] * len(rows), type=pa.string()),
            "occurred_at": pa.array(
                [r["occurred_at"] for r in rows], type=pa.timestamp("us", tz="UTC")
            ),
            "subject_hash": pa.array(["a" * 64] * len(rows), type=pa.string()),
            "in_reply_to": pa.array([r.get("in_reply_to") for r in rows], type=pa.string()),
            "references": pa.array([[] for _ in rows], type=pa.list_(pa.string())),
            "thread_id": pa.array([r["thread_id"] for r in rows], type=pa.string()),
            "source_snapshot_id": pa.array(["snap-1"] * len(rows), type=pa.string()),
        }
    )
    storage.write_partition(data_dir, "ponymail", "message", "2026-09-25", "run-1", table)


def _write_issue(data_dir, rows: list[dict]) -> None:
    n = len(rows)
    table = pa.table(
        {
            "issue_key": pa.array([r["issue_key"] for r in rows], type=pa.string()),
            "summary": pa.array([None] * n, type=pa.string()),
            "status": pa.array([None] * n, type=pa.string()),
            "status_category": pa.array([None] * n, type=pa.string()),
            "priority": pa.array([None] * n, type=pa.string()),
            "issue_type": pa.array([None] * n, type=pa.string()),
            "created_at": pa.array(
                [r["created_at"] for r in rows], type=pa.timestamp("us", tz="UTC")
            ),
            "updated_at": pa.array(
                [r["created_at"] for r in rows], type=pa.timestamp("us", tz="UTC")
            ),
            "resolved_at": pa.array([None] * n, type=pa.timestamp("us", tz="UTC")),
            "reporter_identity_id": pa.array([None] * n, type=pa.string()),
            "reporter_raw": pa.array([None] * n, type=pa.string()),
            "assignee_identity_id": pa.array([None] * n, type=pa.string()),
            "assignee_raw": pa.array([None] * n, type=pa.string()),
            "resolution": pa.array([None] * n, type=pa.string()),
            "source_snapshot_id": pa.array(["snap-1"] * n, type=pa.string()),
        }
    )
    storage.write_partition(data_dir, "jira", "issue", "2026-09-25", "run-1", table)


def _ponymail_transport(months: dict[str, dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        params = parse_qs(request.url.query.decode())
        year_month = params["d"][0]
        if year_month not in months:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=months[year_month])

    return httpx.MockTransport(handler)


def _mail_record(message_id: str, sender: str, body: str, in_reply_to: str = "") -> dict:
    return {
        "message-id": message_id,
        "mid": message_id.strip("<>").replace("@", "-"),
        "from": sender,
        "in-reply-to": in_reply_to,
        "body": body,
    }


def _jira_paginating_transport(comments_by_issue: dict[str, list[dict]]) -> httpx.MockTransport:
    """Serves `/rest/api/2/issue/{key}/comment` honoring `startAt`/
    `maxResults`, so pagination (issue #110: "fetch full comment streams...
    via paged search") is actually exercised, not just a single-page
    fixture."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.split("/")[-2]
        comments = comments_by_issue.get(key)
        if comments is None:
            return httpx.Response(404, json={"errorMessages": ["not found"]})
        params = parse_qs(request.url.query.decode())
        start_at = int(params.get("startAt", ["0"])[0])
        max_results = int(params.get("maxResults", ["50"])[0])
        page = comments[start_at : start_at + max_results]
        return httpx.Response(
            200,
            json={
                "startAt": start_at,
                "maxResults": max_results,
                "total": len(comments),
                "comments": page,
            },
        )

    return httpx.MockTransport(handler)


def _jira_comment(comment_id: str, author: str, created: str, body: str) -> dict:
    return {"id": comment_id, "author": {"name": author}, "created": created, "body": body}


def _jev_transport(
    probability: float = 0.6, call_log: list[dict] | None = None
) -> httpx2.MockTransport:
    answers = {label: {"type": "noul", "noul": probability} for label in MESSAGE_LEVEL_LABELS}
    answers["tone_intensity"] = {
        "type": "score",
        "score": 1.0,
        "confidence": 0.8,
        "legend": {0: "neutral", 1: "firm", 2: "sharp", 3: "heated", 4: "aggressive"},
        "probabilities": {0: 0.1, 1: 0.6, 2: 0.2, 3: 0.05, 4: 0.05},
    }
    body = {
        "model": EXPECTED_MODEL,
        "usage": {"input_tokens": 50, "output_tokens": 10},
        "answers": answers,
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        if call_log is not None:
            call_log.append(json.loads(request.content))
        return httpx2.Response(200, json=body)

    return httpx2.MockTransport(handler)


def _jev_402_after_n_transport(n: int, probability: float = 0.6) -> httpx2.MockTransport:
    """Serves `n` successful responses, then HTTP 402 (no credits) for every
    call after that -- simulates issue #110's real failure (a run that
    dies partway through because the TypeSafe org ran out of credits)."""
    answers = {label: {"type": "noul", "noul": probability} for label in MESSAGE_LEVEL_LABELS}
    answers["tone_intensity"] = {
        "type": "score",
        "score": 1.0,
        "confidence": 0.8,
        "legend": {0: "neutral", 1: "firm", 2: "sharp", 3: "heated", 4: "aggressive"},
        "probabilities": {0: 0.1, 1: 0.6, 2: 0.2, 3: 0.05, 4: 0.05},
    }
    success_body = {
        "model": EXPECTED_MODEL,
        "usage": {"input_tokens": 50, "output_tokens": 10},
        "answers": answers,
    }
    count = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        count["n"] += 1
        if count["n"] <= n:
            return httpx2.Response(200, json=success_body)
        return httpx2.Response(
            402, json={"error": "Your organization has no available TypeSafe API credits"}
        )

    return httpx2.MockTransport(handler)


# --- collect_dev_pending -----------------------------------------------------


class TestCollectDevPending:
    def test_caps_at_max_messages_per_thread_and_records_truncation(self, tmp_path):
        n = MAX_MESSAGES_PER_THREAD + 5
        message_rows = [
            {
                "message_id": f"<m{i}@example.org>",
                "sender_raw_value": "alice@example.org",
                "occurred_at": _ts(2024, 1, 1, i % 23, 0, 0)
                if i < 24
                else _ts(2024, 1, 2, i % 23, 0, 0),
                "thread_id": "big-thread",
            }
            for i in range(n)
        ]
        _write_message(tmp_path, message_rows)

        strata = {
            "2024Q1": StratumSample(
                venue="mailing_list",
                quarter="2024Q1",
                population=1,
                sampled_ids=("big-thread",),
                weight=1.0,
            )
        }

        month_digest = {
            "hits": n,
            "emails": [
                _mail_record(r["message_id"], "Alice <alice@example.org>", "hello world")
                for r in message_rows
            ],
        }
        transport = _ponymail_transport({"2024-01": month_digest, "2024-02": month_digest})

        from project_health.classify.text_fetch import PonyMailTextFetcher

        with PonyMailTextFetcher(transport=transport, min_request_interval=0) as fetcher:
            pending, truncated = collect_dev_pending(
                tmp_path, "dev", "example.org", strata, fetcher, automated_sender_patterns=[]
            )

        assert len(pending) == MAX_MESSAGES_PER_THREAD
        assert truncated["2024Q1"] == 1

    def test_drops_automated_senders_after_capping(self, tmp_path):
        message_rows = [
            {
                "message_id": "<m1@example.org>",
                "sender_raw_value": "alice@example.org",
                "occurred_at": _ts(2024, 1, 1, 10, 0, 0),
                "thread_id": "t1",
            },
            {
                "message_id": "<m2@example.org>",
                "sender_raw_value": "jenkins@example.org",
                "occurred_at": _ts(2024, 1, 1, 11, 0, 0),
                "thread_id": "t1",
            },
        ]
        _write_message(tmp_path, message_rows)
        strata = {
            "2024Q1": StratumSample(
                venue="mailing_list",
                quarter="2024Q1",
                population=1,
                sampled_ids=("t1",),
                weight=1.0,
            )
        }
        month_digest = {
            "hits": 2,
            "emails": [
                _mail_record("<m1@example.org>", "Alice <alice@example.org>", "a real message"),
                _mail_record("<m2@example.org>", "Jenkins <jenkins@example.org>", "build ok"),
            ],
        }
        transport = _ponymail_transport({"2024-01": month_digest})
        from project_health.classify.text_fetch import PonyMailTextFetcher

        with PonyMailTextFetcher(transport=transport, min_request_interval=0) as fetcher:
            pending, _truncated = collect_dev_pending(
                tmp_path,
                "dev",
                "example.org",
                strata,
                fetcher,
                automated_sender_patterns=[{"regex": r"(?i)jenkins@"}],
            )

        assert len(pending) == 1
        assert pending[0].call_id == "mail:<m1@example.org>"

    def test_empty_stratum_returns_nothing(self, tmp_path):
        strata = {
            "2024Q1": StratumSample(
                venue="mailing_list", quarter="2024Q1", population=0, sampled_ids=(), weight=0.0
            )
        }
        from project_health.classify.text_fetch import PonyMailTextFetcher

        with PonyMailTextFetcher(
            transport=_ponymail_transport({}), min_request_interval=0
        ) as fetcher:
            pending, truncated = collect_dev_pending(
                tmp_path, "dev", "example.org", strata, fetcher, automated_sender_patterns=[]
            )
        assert pending == []
        assert truncated["2024Q1"] == 0


# --- collect_jira_pending -----------------------------------------------------


class TestCollectJiraPending:
    def test_fetches_the_full_paginated_comment_stream(self, tmp_path):
        # 55 comments, page size 50 (JiraCommentTextFetcher's default) --
        # exercises real multi-page pagination, all below the 60 cap.
        comments = [
            _jira_comment(
                str(1000 + i),
                "alice",
                f"2024-01-{(i % 27) + 1:02d}T10:00:00.000+0000",
                f"comment {i}",
            )
            for i in range(55)
        ]
        transport = _jira_paginating_transport({"EXAMPLE-1": comments})
        strata = {
            "2024Q1": StratumSample(
                venue="jira_comment",
                quarter="2024Q1",
                population=1,
                sampled_ids=("EXAMPLE-1",),
                weight=1.0,
            )
        }

        from project_health.classify.text_fetch import JiraCommentTextFetcher

        with JiraCommentTextFetcher(
            "https://issues.example.org/jira", transport=transport, min_request_interval=0
        ) as fetcher:
            pending, truncated = collect_jira_pending(strata, fetcher, automated_sender_patterns=[])

        assert len(pending) == 55
        assert truncated["2024Q1"] == 0

    def test_caps_and_records_truncation_over_60_comments(self, tmp_path):
        comments = [
            _jira_comment(
                str(2000 + i),
                "alice",
                f"2024-01-{(i % 27) + 1:02d}T10:00:00.000+0000",
                f"comment {i}",
            )
            for i in range(70)
        ]
        transport = _jira_paginating_transport({"EXAMPLE-1": comments})
        strata = {
            "2024Q1": StratumSample(
                venue="jira_comment",
                quarter="2024Q1",
                population=1,
                sampled_ids=("EXAMPLE-1",),
                weight=1.0,
            )
        }

        from project_health.classify.text_fetch import JiraCommentTextFetcher

        with JiraCommentTextFetcher(
            "https://issues.example.org/jira", transport=transport, min_request_interval=0
        ) as fetcher:
            pending, truncated = collect_jira_pending(strata, fetcher, automated_sender_patterns=[])

        assert len(pending) == MAX_MESSAGES_PER_THREAD
        assert truncated["2024Q1"] == 1

    def test_drops_automated_authors(self, tmp_path):
        comments = [
            _jira_comment("3000", "alice", "2024-01-01T10:00:00.000+0000", "real comment"),
            _jira_comment("3001", "githubbot", "2024-01-01T11:00:00.000+0000", "automated comment"),
        ]
        transport = _jira_paginating_transport({"EXAMPLE-1": comments})
        strata = {
            "2024Q1": StratumSample(
                venue="jira_comment",
                quarter="2024Q1",
                population=1,
                sampled_ids=("EXAMPLE-1",),
                weight=1.0,
            )
        }

        from project_health.classify.text_fetch import JiraCommentTextFetcher

        with JiraCommentTextFetcher(
            "https://issues.example.org/jira", transport=transport, min_request_interval=0
        ) as fetcher:
            pending, _truncated = collect_jira_pending(
                strata, fetcher, automated_sender_patterns=[{"regex": r"(?i)bot$"}]
            )

        assert len(pending) == 1
        assert pending[0].call_id == "jira:EXAMPLE-1:3000"


# --- run_private_run end-to-end -----------------------------------------------


def _small_fixture(tmp_path: Path, project_key: str = "EXAMPLE"):
    data_dir = tmp_path / "data"
    _write_message_thread(
        data_dir,
        [
            {"thread_id": "t1", "started_at": _ts(2024, 1, 5)},
            {"thread_id": "t2", "started_at": _ts(2024, 1, 6)},
        ],
    )
    _write_message(
        data_dir,
        [
            {
                "message_id": "<m1@example.org>",
                "sender_raw_value": "alice@example.org",
                "occurred_at": _ts(2024, 1, 5, 10, 0, 0),
                "thread_id": "t1",
            },
            {
                "message_id": "<m2@example.org>",
                "sender_raw_value": "bob@example.org",
                "occurred_at": _ts(2024, 1, 5, 11, 0, 0),
                "thread_id": "t1",
                "in_reply_to": "<m1@example.org>",
            },
            {
                "message_id": "<m3@example.org>",
                "sender_raw_value": "carol@example.org",
                "occurred_at": _ts(2024, 1, 6, 9, 0, 0),
                "thread_id": "t2",
            },
        ],
    )
    _write_issue(
        data_dir,
        [
            {"issue_key": f"{project_key}-1", "created_at": _ts(2024, 1, 2)},
        ],
    )

    ponymail_months = {
        "2024-01": {
            "hits": 3,
            "emails": [
                _mail_record("<m1@example.org>", "Alice <alice@example.org>", "opening message"),
                _mail_record(
                    "<m2@example.org>",
                    "Bob <bob@example.org>",
                    "a reply",
                    in_reply_to="<m1@example.org>",
                ),
                _mail_record("<m3@example.org>", "Carol <carol@example.org>", "another thread"),
            ],
        }
    }
    jira_comments = {
        f"{project_key}-1": [
            _jira_comment("9001", "dave", "2024-01-03T10:00:00.000+0000", "first comment"),
            _jira_comment("9002", "erin", "2024-01-04T10:00:00.000+0000", "second comment"),
        ]
    }
    return data_dir, ponymail_months, jira_comments


class TestRunPrivateRunEndToEnd:
    def test_writes_report_and_aggregates_with_expected_counts(self, tmp_path):
        config = _project_config(tmp_path)
        data_dir, ponymail_months, jira_comments = _small_fixture(tmp_path)
        out_dir = tmp_path / "out"

        result = run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            seed=110,
            k=60,
            api_key="test-key",
            monthly_cap_usd=1000.0,
            bootstrap_iterations=10,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
            jev_async_transport=_jev_transport(probability=0.6),
        )

        assert result.report_path.is_file()
        assert result.aggregates_path.is_file()
        aggregates = result.aggregates
        assert aggregates["venue_totals"]["mailing_list"]["messages_classified"] == 3
        assert aggregates["venue_totals"]["jira_comment"]["messages_classified"] == 2
        # below the §5.1 floors (few messages/authors) -> insufficient data
        cell = aggregates["cells_by_quarter"]["mailing_list"]["2024Q1"]
        assert cell["insufficient_data"] is True
        assert cell["messages_classified"] == 3
        assert cell["distinct_authors"] == 3  # alice, bob, carol

    def test_never_writes_message_text_or_ids_to_aggregates_json(self, tmp_path):
        config = _project_config(tmp_path)
        data_dir, ponymail_months, jira_comments = _small_fixture(tmp_path)
        out_dir = tmp_path / "out"

        result = run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            api_key="test-key",
            monthly_cap_usd=1000.0,
            bootstrap_iterations=5,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
            jev_async_transport=_jev_transport(),
        )
        raw = result.aggregates_path.read_text(encoding="utf-8")
        for forbidden in (
            "opening message",
            "a reply",
            "<m1@example.org>",
            "alice@example.org",
            "t1",
        ):
            assert forbidden not in raw
        report_text = result.report_path.read_text(encoding="utf-8")
        for forbidden in ("opening message", "<m1@example.org>", "alice@example.org"):
            assert forbidden not in report_text

    def test_resuming_reuses_the_classification_cache_and_makes_no_new_calls(self, tmp_path):
        config = _project_config(tmp_path)
        data_dir, ponymail_months, jira_comments = _small_fixture(tmp_path)
        out_dir = tmp_path / "out"
        call_log: list[dict] = []

        run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            api_key="test-key",
            monthly_cap_usd=1000.0,
            bootstrap_iterations=5,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
            jev_async_transport=_jev_transport(call_log=call_log),
        )
        first_calls = len(call_log)
        assert first_calls == 5  # 3 mail + 2 jira

        result2 = run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            api_key="test-key",
            monthly_cap_usd=1000.0,
            bootstrap_iterations=5,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
            jev_async_transport=_jev_transport(call_log=call_log),
        )
        assert len(call_log) == first_calls  # no new Jev calls on the second run
        assert result2.run_result.calls_made == 0
        assert result2.run_result.cache_hits == 5

    def test_sample_only_writes_manifest_with_no_jev_call(self, tmp_path):
        config = _project_config(tmp_path)
        data_dir, _ponymail_months, _jira_comments = _small_fixture(tmp_path)
        out_dir = tmp_path / "out"

        result = run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            k=1,
            sample_only=True,
        )
        assert result.sample_manifest_path.is_file()
        manifest = json.loads(result.sample_manifest_path.read_text())
        assert manifest["mailing_list"]["2024Q1"]["population"] == 2
        assert manifest["mailing_list"]["2024Q1"]["sampled"] == 1
        assert manifest["mailing_list"]["2024Q1"]["weight"] == 2.0
        assert manifest["jira_comment"]["2024Q1"]["population"] == 1
        assert result.run_result is None
        assert result.report_path is None

    def test_cost_ledger_persists_across_runs_and_cumulative_reflects_lifetime_spend(
        self, tmp_path
    ):
        """Issue #110 fixup round 1: a cache-only re-run's own `calls_made
        == 0`/`$0.00` must not erase the record of what this --out
        directory has really cost over its lifetime."""
        config = _project_config(tmp_path)
        data_dir, ponymail_months, jira_comments = _small_fixture(tmp_path)
        out_dir = tmp_path / "out"

        result1 = run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            api_key="test-key",
            monthly_cap_usd=1000.0,
            bootstrap_iterations=5,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
            jev_async_transport=_jev_transport(probability=0.6),
        )
        assert (out_dir / "cost_ledger.jsonl").is_file()
        ledger_lines_1 = (out_dir / "cost_ledger.jsonl").read_text().splitlines()
        assert len(ledger_lines_1) == 1

        first_cost = result1.aggregates["cost_ledger"]["cumulative_from_cache"][
            "estimated_cost_usd"
        ]
        assert first_cost > 0.0
        assert result1.aggregates["cost_ledger"]["runs_recorded"] == 1
        assert (
            result1.aggregates["cost_ledger"]["cumulative_from_cache"][
                "distinct_messages_ever_classified"
            ]
            == 5
        )

        # re-run: 0 new Jev calls (fully cached), but the ledger gains a
        # second entry and cumulative cost is NOT reset to 0.
        result2 = run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            api_key="test-key",
            monthly_cap_usd=1000.0,
            bootstrap_iterations=5,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
            jev_async_transport=_jev_transport(probability=0.6),
        )
        assert result2.run_result.calls_made == 0
        assert result2.aggregates["cost_summary"]["estimated_cost_usd"] == 0.0

        ledger_lines_2 = (out_dir / "cost_ledger.jsonl").read_text().splitlines()
        assert len(ledger_lines_2) == 2

        cumulative_2 = result2.aggregates["cost_ledger"]["cumulative_from_cache"]
        assert cumulative_2["estimated_cost_usd"] == first_cost  # unchanged, not zeroed
        assert cumulative_2["distinct_messages_ever_classified"] == 5
        assert result2.aggregates["cost_ledger"]["runs_recorded"] == 2

        # the ledger-summed cost is exact per recorded run: run 1 paid for
        # 5 real calls, run 2 paid for 0 (fully cached) -- summed, it must
        # equal exactly what run 1 alone billed.
        ledger_runs_2 = result2.aggregates["cost_ledger"]["cumulative_from_ledger_runs"]
        assert ledger_runs_2["calls_made"] == 5
        assert (
            ledger_runs_2["estimated_cost_usd"]
            == result1.aggregates["cost_summary"]["estimated_cost_usd"]
        )

    def test_trend_summary_structure_and_headline_cutoff(self, tmp_path):
        config = _project_config(tmp_path)
        data_dir, ponymail_months, jira_comments = _small_fixture(tmp_path)
        out_dir = tmp_path / "out"

        # 2024Q1 falls inside the "2023_2025" recent trend window; nothing
        # in this fixture falls in "2017_2019", so that window must render
        # as insufficient data end-to-end.
        result = run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            api_key="test-key",
            monthly_cap_usd=1000.0,
            bootstrap_iterations=5,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
            jev_async_transport=_jev_transport(probability=0.6),
        )
        trend = result.aggregates["trend_summary"]
        assert set(trend) == {"mailing_list", "jira_comment"}
        mailing_list_trend = trend["mailing_list"]
        assert mailing_list_trend["headline_cutoff"] == "0.5"
        assert set(mailing_list_trend["windows"]) == {"2017_2019", "2023_2025"}

        early = mailing_list_trend["windows"]["2017_2019"]
        assert early["insufficient_data"] is True
        assert early["messages_classified"] == 0

        recent = mailing_list_trend["windows"]["2023_2025"]
        assert recent["messages_classified"] == 3  # same 3 mail messages as 2024Q1

        # early window is empty -> overlap is undecidable (None) for every label
        assert all(v is None for v in mailing_list_trend["ci_overlap_by_label"].values())

        report_text = result.report_path.read_text(encoding="utf-8")
        assert "Trend summary" in report_text
        assert report_text.index("Trend summary") < report_text.index("Frame definition")

    def test_sensitivity_thresholds_carry_dataset_name_and_permissive_flag(self, tmp_path):
        config = _project_config(tmp_path)
        data_dir, ponymail_months, jira_comments = _small_fixture(tmp_path)
        out_dir = tmp_path / "out"

        benchmark_path = tmp_path / "public-v1.md"
        benchmark_path.write_text(
            "## LKML Ferreira Set (2021)\n\n"
            "| Our label | Strength | Gate role | Best-F1 threshold | Precision (95% CI) | "
            "Recall (95% CI) | F1 (95% CI) | Sample prevalence | Population prevalence | Notes |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n"
            "| `personal_attack` | strong | gating | 0.15 | 0.5 [0.4,0.6] | 0.5 [0.4,0.6] | "
            "0.479 [0.4,0.6] | 10/100 = 0.1 | 100/1000 = 0.1 | note |\n",
            encoding="utf-8",
        )

        result = run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            api_key="test-key",
            monthly_cap_usd=1000.0,
            bootstrap_iterations=5,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
            jev_async_transport=_jev_transport(probability=0.6),
            public_benchmark_path=benchmark_path,
        )
        thresholds = result.aggregates["sensitivity_thresholds"]
        assert thresholds == {
            "personal_attack": {
                "threshold": 0.15,
                "f1": 0.479,
                "dataset_name": "LKML Ferreira Set (2021)",
                "permissive": True,
            }
        }
        report_text = result.report_path.read_text(encoding="utf-8")
        assert "LKML Ferreira Set (2021)" in report_text

    def test_402_pauses_cleanly_and_reports_partial_run(self, tmp_path):
        """Issue #110 fixup round 2: the real failure this reproduces --
        TypeSafe runs out of API credits partway through a run. Must not
        raise, must still write report.md/aggregates.json, and must surface
        a strata coverage table."""
        config = _project_config(tmp_path)
        data_dir, ponymail_months, jira_comments = _small_fixture(tmp_path)
        out_dir = tmp_path / "out"

        # 5 total pending (3 mail + 2 jira, dispatched in that order at
        # concurrency=1): the first 2 succeed, the rest get 402.
        result = run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            api_key="test-key",
            concurrency=1,
            monthly_cap_usd=1000.0,
            bootstrap_iterations=5,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
            jev_async_transport=_jev_402_after_n_transport(2),
        )  # must not raise

        assert result.run_result.status == "paused_no_credits"
        assert result.run_result.calls_made == 2
        assert result.report_path.is_file()
        assert result.aggregates_path.is_file()

        partial = result.aggregates["partial_run"]
        assert partial is not None
        assert partial["status"] == "paused_no_credits"
        assert partial["messages_classified"] == 2
        assert partial["messages_sampled"] == 5
        coverage_venues = {row["venue"] for row in partial["coverage_by_stratum"]}
        assert coverage_venues == {"mailing_list", "jira_comment"}
        total_sampled_in_table = sum(
            row["messages_sampled"] for row in partial["coverage_by_stratum"]
        )
        assert total_sampled_in_table == 5

        report_text = result.report_path.read_text(encoding="utf-8")
        assert "PARTIAL RUN" in report_text
        assert "paused_no_credits" in report_text
        assert "2 of 5 sampled messages were classified" in report_text
        # PARTIAL RUN must appear near the top, before the trend summary.
        assert report_text.index("PARTIAL RUN") < report_text.index("Trend summary")

    def test_cost_cap_pause_also_reports_partial_run(self, tmp_path):
        """The pre-existing D10 cost-cap pause gets the same PARTIAL RUN
        treatment as a 402 -- not a status-specific special case."""
        config = _project_config(tmp_path)
        data_dir, ponymail_months, jira_comments = _small_fixture(tmp_path)
        out_dir = tmp_path / "out"

        result = run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            api_key="test-key",
            concurrency=1,
            # 50 input tokens/call at the default $0.042/M price is a tiny
            # fraction of a cent, but a $0.0000005 cap is exceeded by the
            # very first call's own cost, so nothing after it goes through.
            monthly_cap_usd=0.0000005,
            bootstrap_iterations=5,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
            jev_async_transport=_jev_transport(probability=0.6),
        )

        assert result.run_result.status == "paused_cost_cap"
        partial = result.aggregates["partial_run"]
        assert partial is not None
        assert partial["status"] == "paused_cost_cap"
        assert partial["messages_classified"] < partial["messages_sampled"]

    def test_no_classify_makes_zero_jev_calls_and_aggregates_from_cache(self, tmp_path):
        config = _project_config(tmp_path)
        data_dir, ponymail_months, jira_comments = _small_fixture(tmp_path)
        out_dir = tmp_path / "out"

        # Populate the cache fully with a real (mocked) classify run first.
        run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            api_key="test-key",
            monthly_cap_usd=1000.0,
            bootstrap_iterations=5,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
            jev_async_transport=_jev_transport(probability=0.6),
        )

        # Now re-run with --no-classify: no api_key, no jev_async_transport
        # at all -- if this tried to reach TypeSafe, it would either raise
        # (no transport configured) or hit tests/conftest.py's network
        # block. Neither happens if zero Jev calls are actually made.
        result = run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            monthly_cap_usd=1000.0,
            bootstrap_iterations=5,
            no_classify=True,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
        )

        assert result.run_result.status == "completed"
        assert result.run_result.calls_made == 0
        assert result.aggregates["venue_totals"]["mailing_list"]["messages_classified"] == 3
        assert result.aggregates["venue_totals"]["jira_comment"]["messages_classified"] == 2
        assert result.aggregates["partial_run"] is None  # cache was already complete

    def test_no_classify_against_a_partially_cached_out_dir_reports_partial_run(self, tmp_path):
        config = _project_config(tmp_path)
        data_dir, ponymail_months, jira_comments = _small_fixture(tmp_path)
        out_dir = tmp_path / "out"

        # First run: only 2 of 5 messages get classified (402 partway).
        run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            api_key="test-key",
            concurrency=1,
            monthly_cap_usd=1000.0,
            bootstrap_iterations=5,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
            jev_async_transport=_jev_402_after_n_transport(2),
        )

        # --no-classify against that same --out: must not attempt to
        # classify the remaining 3, and must still report the shortfall.
        result = run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            monthly_cap_usd=1000.0,
            bootstrap_iterations=5,
            no_classify=True,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
        )

        assert result.run_result.calls_made == 0
        partial = result.aggregates["partial_run"]
        assert partial is not None
        assert partial["status"] == "completed"  # --no-classify never "pauses" itself
        assert partial["messages_classified"] == 2
        assert partial["messages_sampled"] == 5

    def test_fully_classified_run_has_no_partial_run_section(self, tmp_path):
        config = _project_config(tmp_path)
        data_dir, ponymail_months, jira_comments = _small_fixture(tmp_path)
        out_dir = tmp_path / "out"

        result = run_private_run(
            project_config=config,
            data_dir=data_dir,
            out_dir=out_dir,
            quarters=["2024Q1"],
            api_key="test-key",
            monthly_cap_usd=1000.0,
            bootstrap_iterations=5,
            ponymail_transport=_ponymail_transport(ponymail_months),
            jira_transport=_jira_paginating_transport(jira_comments),
            jev_async_transport=_jev_transport(probability=0.6),
        )
        assert result.aggregates["partial_run"] is None
        assert "PARTIAL RUN" not in result.report_path.read_text(encoding="utf-8")
