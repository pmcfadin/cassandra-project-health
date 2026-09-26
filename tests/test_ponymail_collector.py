"""Tests for project_health.collectors.ponymail (issue #33).

All tests are fully offline: every `httpx` call goes through an injected
`httpx.MockTransport` built from `tests/fixtures/ponymail/*` (real recorded
`stats.lua`/`mbox.lua` responses, bodies stripped before being saved — see
`tests/fixtures/ponymail/README.md`) or from small synthetic mbox bytes built
inline in this file. Retry/backoff tests inject a no-op `sleep_fn`. Nothing
here touches the network.

D1/D16 (no message body is ever persisted) is the hard rule this module
exists to enforce; `TestBodyNeverPersisted` below is the acceptance-criterion
test for that -- it scans every string cell of every output column, and every
byte of every committed fixture file, for body content.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from project_health.collectors.ponymail import (
    CollectionError,
    PonyMailCollector,
    _build_message_thread_rows,
    _extract_message_meta,
    iter_mbox_messages,
    month_range,
    months_needed,
    next_watermark_for,
    select_backfill_months,
)
from project_health.config import ProjectConfig, load_project

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "ponymail"

STATS_DEV = (FIXTURES_DIR / "stats_dev.json").read_text()
MBOX_2009_01 = (FIXTURES_DIR / "dev_2009-01.mbox").read_bytes()
MBOX_2009_02 = (FIXTURES_DIR / "dev_2009-02.mbox").read_bytes()
EMPTY_MBOX = b""

# A synthetic stats.lua response whose active range is exactly the two
# recorded fixture months -- i.e. "2009-02" is this (synthetic) list's
# current month. Used by tests that want deterministic, unrestricted
# fetching of exactly those two real recorded months without needing a
# `max_months_per_list` cap to stand in for it (the real STATS_DEV fixture's
# true range is 2009-01..2026-09, ~213 months -- see `TestBackfill` for tests
# that specifically exercise capping/backfill against that full range).
TWO_MONTH_STATS = b'{"firstYear": 2009, "firstMonth": 1, "lastYear": 2009, "lastMonth": 2}'

# The real body text that was in dev_2009-01.mbox *before* it was stripped
# for the committed fixture (tests/fixtures/ponymail/README.md). It must
# never reappear in the fixture file itself, nor in any collector output.
STRIPPED_BODY_CANARY = "sole use of the recipient named above"


@pytest.fixture
def config() -> ProjectConfig:
    return load_project("projects/cassandra.yaml")


def _offline_collector(
    config: ProjectConfig, transport: httpx.MockTransport
) -> PonyMailCollector:
    return PonyMailCollector(
        config,
        transport=transport,
        min_request_interval=0,
        sleep_fn=lambda s: None,
    )


def _fixture_transport() -> httpx.MockTransport:
    """Serves the real recorded dev@ fixtures for any list (this project
    config's `mailing_lists.lists` is `[dev, user]`; `user` gets the same two
    fixture months since `stats.lua`'s synthetic range here is exactly those
    two months -- no `max_months_per_list` cap needed to bound the fetch)."""

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        if request.url.path.endswith("stats.lua"):
            return httpx.Response(200, content=TWO_MONTH_STATS)
        assert request.url.path.endswith("mbox.lua")
        date = query["date"][0]
        if date == "2009-01":
            return httpx.Response(200, content=MBOX_2009_01)
        if date == "2009-02":
            return httpx.Response(200, content=MBOX_2009_02)
        return httpx.Response(200, content=EMPTY_MBOX)

    return httpx.MockTransport(handler)


class TestMonthRangeAndWatermark:
    def test_month_range_spans_year_boundary(self):
        assert month_range(2009, 11, 2010, 2) == [
            "2009-11",
            "2009-12",
            "2010-01",
            "2010-02",
        ]

    def test_month_range_single_month(self):
        assert month_range(2009, 1, 2009, 1) == ["2009-01"]

    def test_months_needed_first_run_returns_everything(self):
        all_months = ["2009-01", "2009-02", "2009-03"]
        assert months_needed(all_months, None) == all_months

    def test_months_needed_incremental_returns_after_watermark(self):
        all_months = ["2009-01", "2009-02", "2009-03"]
        assert months_needed(all_months, "2009-01") == ["2009-02", "2009-03"]

    def test_months_needed_watermark_at_current_month_refetches_current_only(self):
        all_months = ["2009-01", "2009-02"]
        # watermark already at the last-completed month -- only the current
        # (never-completed) month is refetched.
        assert months_needed(all_months, "2009-01") == ["2009-02"]

    def test_next_watermark_is_latest_fetched_month_excluding_current(self):
        assert next_watermark_for(["2009-01", "2009-02"], "2009-03", None) == "2009-02"

    def test_next_watermark_keeps_previous_when_only_current_month_fetched(self):
        # The current month is never "completed" -- fetching only it advances
        # nothing.
        assert next_watermark_for(["2009-02"], "2009-02", "2009-01") == "2009-01"
        assert next_watermark_for([], "2009-02", None) is None

    def test_next_watermark_uses_max_completed_even_when_short_of_current(self):
        # max_months_per_list can cap a run short of the list's true current
        # month -- every fetched month before it is still "completed".
        assert next_watermark_for(["2009-01", "2009-02"], "2026-09", None) == "2009-02"


class TestMessageExtraction:
    def test_lowercases_sender_address(self):
        import email

        raw = (
            b"Message-ID: <abc@example.org>\r\n"
            b"From: Alice Example <ALICE@Example.ORG>\r\n"
            b"Date: Wed, 28 Jan 2009 13:33:59 -0500\r\n"
            b"Subject: Hello\r\n"
            b"\r\n"
            b"body\r\n"
        )
        message = email.message_from_bytes(raw)
        meta = _extract_message_meta(message, "dev")

        assert meta["sender_raw_value"] == "alice@example.org"
        assert meta["sender_display_name"] == "Alice Example"
        assert meta["sender_raw_type"] == "mailing_list_address"

    def test_missing_message_id_is_skipped(self):
        import email

        raw = (
            b"From: Alice <alice@example.org>\r\n"
            b"Date: Wed, 28 Jan 2009 13:33:59 -0500\r\n"
            b"Subject: Hello\r\n\r\nbody\r\n"
        )
        message = email.message_from_bytes(raw)
        assert _extract_message_meta(message, "dev") is None

    def test_missing_date_is_skipped(self):
        import email

        raw = (
            b"Message-ID: <abc@example.org>\r\n"
            b"From: Alice <alice@example.org>\r\n"
            b"Subject: Hello\r\n\r\nbody\r\n"
        )
        message = email.message_from_bytes(raw)
        assert _extract_message_meta(message, "dev") is None

    def test_thread_id_prefers_first_reference_over_in_reply_to(self):
        import email

        raw = (
            b"Message-ID: <child@example.org>\r\n"
            b"From: Bob <bob@example.org>\r\n"
            b"Date: Wed, 28 Jan 2009 13:33:59 -0500\r\n"
            b"Subject: Re: Hello\r\n"
            b"In-Reply-To: <middle@example.org>\r\n"
            b"References: <root@example.org> <middle@example.org>\r\n"
            b"\r\nbody\r\n"
        )
        message = email.message_from_bytes(raw)
        meta = _extract_message_meta(message, "dev")

        assert meta["thread_id"] == "<root@example.org>"
        assert meta["in_reply_to"] == "<middle@example.org>"
        assert meta["references"] == ["<root@example.org>", "<middle@example.org>"]

    def test_header_returned_as_header_object_does_not_crash_extraction(self):
        """Regression test (issue #33 live-run finding against a real 2021
        dev@cassandra.apache.org message): a header containing raw,
        non-MIME-encoded 8-bit bytes comes back from `email.message.Message.
        get()` as an `email.header.Header` instance, not a plain `str`.
        `Header` also defines an `.encode(maxlinelen, splitchars, linesep)`
        method with a totally different signature from `str.encode`, so
        calling `.encode("utf-8", "surrogateescape")` on one (as if it were
        always a plain string) raised a confusing
        `TypeError: unsupported operand type(s) for -: 'str' and 'int'`
        buried inside `email.header` -- reproduced here directly rather than
        depending on a specific external archive's contents so it can never
        regress silently again.
        """
        import email.message
        from email.header import Header

        message = email.message.Message()
        message["Message-ID"] = "<8bit-subject@example.org>"
        message["From"] = "Bob <bob@example.org>"
        message["Date"] = "Wed, 28 Jan 2009 13:33:59 -0500"
        raw_bytes = b"caf\xe9 discussion"
        message["Subject"] = Header(raw_bytes.decode("ascii", "surrogateescape"), "unknown-8bit")

        meta = _extract_message_meta(message, "dev")

        assert meta is not None
        assert isinstance(meta["subject_hash"], str)
        assert len(meta["subject_hash"]) == 64

    def test_thread_id_falls_back_to_own_message_id_when_no_parent(self):
        import email

        raw = (
            b"Message-ID: <standalone@example.org>\r\n"
            b"From: Bob <bob@example.org>\r\n"
            b"Date: Wed, 28 Jan 2009 13:33:59 -0500\r\n"
            b"Subject: New topic\r\n\r\nbody\r\n"
        )
        message = email.message_from_bytes(raw)
        meta = _extract_message_meta(message, "dev")

        assert meta["thread_id"] == "<standalone@example.org>"
        assert meta["in_reply_to"] is None
        assert meta["references"] is None


class TestThreadRollup:
    def test_groups_messages_by_list_and_thread_id(self):
        rows = [
            {
                "list": "dev",
                "thread_id": "t1",
                "message_id": "m1",
                "occurred_at": datetime(2009, 1, 1, tzinfo=timezone.utc),
            },
            {
                "list": "dev",
                "thread_id": "t1",
                "message_id": "m2",
                "occurred_at": datetime(2009, 1, 3, tzinfo=timezone.utc),
            },
            {
                "list": "dev",
                "thread_id": "t2",
                "message_id": "m3",
                "occurred_at": datetime(2009, 1, 2, tzinfo=timezone.utc),
            },
        ]
        rows_out = _build_message_thread_rows(rows, "snap-1")
        by_thread = {r["thread_id"]: r for r in rows_out}

        assert len(rows_out) == 2
        assert by_thread["t1"]["message_count"] == 2
        assert by_thread["t1"]["started_at"] == datetime(2009, 1, 1, tzinfo=timezone.utc)
        assert by_thread["t1"]["last_activity_at"] == datetime(2009, 1, 3, tzinfo=timezone.utc)
        assert by_thread["t1"]["root_message_id"] == "t1"
        assert by_thread["t2"]["message_count"] == 1


class TestMboxParsing:
    def test_iter_mbox_messages_yields_each_message(self):
        messages = list(iter_mbox_messages(MBOX_2009_02))
        assert len(messages) == 3

    def test_iter_mbox_messages_empty_bytes_yields_nothing(self):
        assert list(iter_mbox_messages(EMPTY_MBOX)) == []


class TestCollection:
    def test_collect_fetches_needed_months_and_builds_rows(self, config):
        transport = _fixture_transport()
        collector = _offline_collector(config, transport)

        result = collector.collect(watermarks=None)

        # config's mailing_lists.lists is [dev, user] -- both lists get the
        # same two fixture months served, so 4 messages each = 8 total.
        assert result.message_count == 8
        assert result.skipped_count == 0
        assert result.messages.num_rows == 8

    def test_message_rows_populate_raw_columns_and_subject_hash(self, config):
        transport = _fixture_transport()
        collector = _offline_collector(config, transport)

        result = collector.collect(watermarks=None)
        rows = {
            row["message_id"]: row
            for row in result.messages.to_pylist()
            if row["list"] == "dev"
        }

        standalone = rows["<9FA9ACEAB0F1CA46B3FB68B616AC9A7904C373E8@Exc-A01.dotomi.com>"]
        assert standalone["sender_raw_type"] == "mailing_list_address"
        assert standalone["sender_raw_value"] == "davidd@dotomi.com"
        assert standalone["sender_display_name"] == "David Dabbs"
        assert standalone["sender_identity_id"] is None
        assert standalone["occurred_at"] == datetime(2009, 1, 28, 18, 33, 59, tzinfo=timezone.utc)
        assert standalone["subject_hash"] == hashlib.sha256(
            "Curious when SVN will migrate to ASF".encode()
        ).hexdigest()
        assert standalone["in_reply_to"] is None
        assert standalone["references"] is None
        assert standalone["thread_id"] == standalone["message_id"]

    def test_thread_reconstruction_across_a_recorded_reply_chain(self, config):
        transport = _fixture_transport()
        collector = _offline_collector(config, transport)

        result = collector.collect(watermarks=None)
        dev_messages = [r for r in result.messages.to_pylist() if r["list"] == "dev"]
        dev_threads = [r for r in result.message_threads.to_pylist() if r["list"] == "dev"]

        root_id = "<6c59d89a0902200037t78cf4413o1b1ea473111aace0@mail.gmail.com>"
        thread_ids = {m["thread_id"] for m in dev_messages if m["message_id"] != (
            "<9FA9ACEAB0F1CA46B3FB68B616AC9A7904C373E8@Exc-A01.dotomi.com>"
        )}
        assert thread_ids == {root_id}

        # 2 threads total for dev@ this run: the standalone Jan message and
        # the 3-message Feb reply chain.
        assert len(dev_threads) == 2
        by_id = {t["thread_id"]: t for t in dev_threads}
        assert by_id[root_id]["message_count"] == 3
        assert by_id[root_id]["root_message_id"] == root_id
        standalone_thread_id = "<9FA9ACEAB0F1CA46B3FB68B616AC9A7904C373E8@Exc-A01.dotomi.com>"
        assert by_id[standalone_thread_id]["message_count"] == 1

    def test_rows_validate_against_schemas(self, config):
        from project_health.schema import get_schema, validate

        transport = _fixture_transport()
        collector = _offline_collector(config, transport)
        result = collector.collect(watermarks=None)

        assert validate("message", result.messages).schema.equals(get_schema("message"))
        assert validate("message_thread", result.message_threads).schema.equals(
            get_schema("message_thread")
        )


class TestIncrementalMonthLogic:
    def test_first_run_fetches_from_lists_first_month(self, config):
        seen_dates: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            if request.url.path.endswith("stats.lua"):
                return httpx.Response(200, content=TWO_MONTH_STATS)
            seen_dates.append(query["date"][0])
            return httpx.Response(200, content=EMPTY_MBOX)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(config, transport)

        collector.collect(watermarks=None)

        # Both configured lists (dev, user) start their fetch at the list's
        # first active month per the synthetic two-month stats range
        # (2009-01), in order.
        assert seen_dates == ["2009-01", "2009-02", "2009-01", "2009-02"]


class TestBackfill:
    """issue #33 fixup: an uncapped first-run backfill of a long-lived list
    (a real dev@ full-history pull measured ~31 minutes live) doesn't fit the
    nightly job's `timeout-minutes: 60` alongside git/JIRA/roster. These
    tests exercise `select_backfill_months` directly and end to end against
    the real, full-range `stats_dev.json` fixture (2009-01..2026-09, ~213
    months)."""

    def test_select_backfill_months_no_cap_returns_everything(self):
        needed = ["2009-01", "2009-02", "2009-03"]
        fetch, remaining = select_backfill_months(needed, "2009-03", None)
        assert fetch == needed
        assert remaining == 0

    def test_select_backfill_months_cap_not_reached_returns_everything(self):
        needed = ["2009-01", "2009-02", "2009-03"]
        fetch, remaining = select_backfill_months(needed, "2009-03", 10)
        assert fetch == needed
        assert remaining == 0

    def test_select_backfill_months_caps_oldest_first_plus_current_month(self):
        needed = ["2009-01", "2009-02", "2009-03", "2009-04", "2009-05"]
        # cap=3 -> 2 oldest completed months + the current month always.
        fetch, remaining = select_backfill_months(needed, "2009-05", 3)
        assert fetch == ["2009-01", "2009-02", "2009-05"]
        assert remaining == 2  # "2009-03", "2009-04" still outstanding

    def test_select_backfill_months_cap_of_one_still_fetches_current_month(self):
        needed = ["2009-01", "2009-02", "2009-03"]
        fetch, remaining = select_backfill_months(needed, "2009-03", 1)
        assert fetch == ["2009-03"]
        assert remaining == 2

    def test_select_backfill_months_zero_remaining_when_cap_exactly_covers_backlog(self):
        needed = ["2009-01", "2009-02"]
        fetch, remaining = select_backfill_months(needed, "2009-02", 2)
        assert fetch == needed
        assert remaining == 0

    def test_capped_run_against_real_range_advances_watermark_and_reports_partial(self, config):
        """First run against the real dev@ full range (~213 months), capped
        to 3 months/list: fetches the 2 oldest real recorded months plus
        whatever the real current month ("2026-09") mbox resolves to (empty
        here), advances the watermark to the newest month it actually
        fetched, and reports the real backlog size."""
        single = _single_list_config(config)

        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            if request.url.path.endswith("stats.lua"):
                return httpx.Response(200, content=STATS_DEV)
            date = query["date"][0]
            if date == "2009-01":
                return httpx.Response(200, content=MBOX_2009_01)
            if date == "2009-02":
                return httpx.Response(200, content=MBOX_2009_02)
            return httpx.Response(200, content=EMPTY_MBOX)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(single, transport)

        result = collector.collect(watermarks=None, max_months_per_list=3)

        assert result.message_count == 4  # 1 (2009-01) + 3 (2009-02)
        assert result.next_watermarks == {"dev": "2009-02"}
        # 213 total months - 1 (current, "2026-09", never "backlog") - 2
        # fetched completed months ("2009-01", "2009-02") = 210 remaining.
        assert result.backfill == {"dev": {"months_remaining": 210}}
        assert result.partial is True

    def test_fully_caught_up_run_reports_partial_false(self, config):
        """Once a list's watermark is one month behind the real current
        month, a capped run fetches only the (always-refetched) current
        month, has no backlog left, and reports partial=False."""
        single = _single_list_config(config)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("stats.lua"):
                return httpx.Response(200, content=STATS_DEV)
            return httpx.Response(200, content=EMPTY_MBOX)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(single, transport)

        # STATS_DEV's real lastYear/lastMonth is 2026-09; "2026-08" is the
        # last completed month immediately before it.
        result = collector.collect(watermarks={"dev": "2026-08"}, max_months_per_list=3)

        assert result.backfill == {"dev": {"months_remaining": 0}}
        assert result.partial is False

    def test_second_run_with_stored_watermark_only_refetches_current_month(self, config):
        """A synthetic stats.lua response whose active range is exactly the
        two recorded fixture months (2009-01, 2009-02) -- i.e. "2009-02" is
        this list's current month -- makes the "current month always
        re-fetched, completed months fetched once" rule (ARCHITECTURE.md
        §4.3) directly observable end to end against real recorded mbox
        data, without a `max_months_per_list` cap standing in for it.
        """
        two_month_stats = (
            b'{"firstYear": 2009, "firstMonth": 1, "lastYear": 2009, "lastMonth": 2}'
        )

        def handler(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            if request.url.path.endswith("stats.lua"):
                return httpx.Response(200, content=two_month_stats)
            date = query["date"][0]
            return httpx.Response(200, content=MBOX_2009_01 if date == "2009-01" else MBOX_2009_02)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(config, transport)

        first = collector.collect(watermarks=None)
        # "2009-02" is this (synthetic) list's current month, so only
        # "2009-01" is "completed" -- for both configured lists.
        assert first.next_watermarks == {"dev": "2009-01", "user": "2009-01"}
        assert first.message_count == 8  # (1 + 3) messages x 2 lists

        seen_dates: list[str] = []

        def handler2(request: httpx.Request) -> httpx.Response:
            query = parse_qs(request.url.query.decode())
            if request.url.path.endswith("stats.lua"):
                return httpx.Response(200, content=two_month_stats)
            date = query["date"][0]
            seen_dates.append(date)
            return httpx.Response(200, content=MBOX_2009_02)

        transport2 = httpx.MockTransport(handler2)
        collector2 = _offline_collector(config, transport2)
        second = collector2.collect(watermarks=first.next_watermarks)

        # Only the still-current month is re-requested -- "2009-01" (already
        # completed) is never fetched again.
        assert seen_dates == ["2009-02", "2009-02"]
        assert second.message_count == 6  # 3 messages x 2 lists, re-fetched
        assert second.next_watermarks == {"dev": "2009-01", "user": "2009-01"}


def _single_list_config(config: ProjectConfig, list_name: str = "dev") -> ProjectConfig:
    """A copy of `config` restricted to one mailing list, so retry-count
    assertions don't have to account for a second list's requests."""
    return config.model_copy(
        update={"mailing_lists": config.mailing_lists.model_copy(update={"lists": [list_name]})}
    )


class TestRetry:
    def test_two_503s_then_200_succeeds(self, config):
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("stats.lua"):
                return httpx.Response(200, content=STATS_DEV)
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return httpx.Response(503, text="Service Unavailable")
            return httpx.Response(200, content=EMPTY_MBOX)

        transport = httpx.MockTransport(handler)
        collector = PonyMailCollector(
            _single_list_config(config),
            transport=transport,
            max_retries=5,
            min_request_interval=0,
            sleep_fn=lambda s: None,
        )

        result = collector.collect(watermarks=None, max_months_per_list=1)

        assert call_count["n"] == 3
        assert result.message_count == 0

    def test_five_503s_raises_collection_error(self, config):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("stats.lua"):
                return httpx.Response(200, content=STATS_DEV)
            return httpx.Response(503, text="Service Unavailable")

        transport = httpx.MockTransport(handler)
        collector = PonyMailCollector(
            _single_list_config(config),
            transport=transport,
            max_retries=5,
            min_request_interval=0,
            sleep_fn=lambda s: None,
        )

        with pytest.raises(CollectionError):
            collector.collect(watermarks=None, max_months_per_list=1)

    def test_retry_honors_retry_after_header(self, config):
        sleeps: list[float] = []
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("stats.lua"):
                return httpx.Response(200, content=STATS_DEV)
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "3"}, text="slow down")
            return httpx.Response(200, content=EMPTY_MBOX)

        transport = httpx.MockTransport(handler)
        collector = PonyMailCollector(
            config,
            transport=transport,
            max_retries=5,
            min_request_interval=0,
            sleep_fn=sleeps.append,
        )

        collector.collect(watermarks=None, max_months_per_list=1)

        assert 3.0 in sleeps


class TestNoNetworkAccess:
    def test_default_transport_is_not_used_when_mock_supplied(self, config):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            assert request.url.host == "lists.apache.org"
            if request.url.path.endswith("stats.lua"):
                return httpx.Response(200, content=STATS_DEV)
            return httpx.Response(200, content=EMPTY_MBOX)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(config, transport)
        collector.collect(watermarks=None, max_months_per_list=1)
        assert calls["n"] > 0


class TestBodyNeverPersisted:
    """D1/D16 acceptance criterion: body content never reaches any output
    column, and never reaches a saved fixture file either."""

    def test_no_body_or_subject_text_reaches_any_output_column(self, config):
        """Feed a synthetic (not committed -- built inline here) mbox
        message with a distinctive, obviously-body-shaped payload through
        the real collector code path, then scan every string cell of every
        output column of both `message` and `message_thread` for it."""
        body_canary = "TOTALLY-UNIQUE-BODY-MARKER-8f3c2e1a-should-never-be-persisted"
        subject_text = "a normal-looking subject line, also must never be persisted"
        raw = (
            # mbox envelope line -- required for `mailbox.mbox` to recognize
            # a message boundary at all (a bare RFC 5322 message with no
            # "From " envelope line parses to zero messages).
            b"From carol@example.org Wed Jan 28 13:33:59 2009\r\n"
            b"Message-ID: <bodytest@example.org>\r\n"
            b"From: Carol <carol@example.org>\r\n"
            b"Date: Wed, 28 Jan 2009 13:33:59 -0500\r\n"
            b"Subject: " + subject_text.encode() + b"\r\n"
            b"\r\n"
            b"This is the message body. " + body_canary.encode() + b"\r\n"
            b"multiple lines of body content follow.\r\n"
            b"more body text here.\r\n"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("stats.lua"):
                return httpx.Response(200, content=STATS_DEV)
            return httpx.Response(200, content=raw)

        transport = httpx.MockTransport(handler)
        collector = _offline_collector(config, transport)

        result = collector.collect(watermarks=None, max_months_per_list=1)

        assert result.message_count > 0

        for table in (result.messages, result.message_threads):
            for column_name in table.column_names:
                column = table.column(column_name)
                if not pa_is_string_like(column.type):
                    continue
                for value in column.to_pylist():
                    _assert_no_body_leak(value, body_canary, column_name)
                    _assert_no_body_leak(value, subject_text, column_name)
                    _assert_no_body_leak(value, "message body", column_name)

    def test_recorded_fixture_files_never_contain_body_content(self):
        """Every committed fixture under tests/fixtures/ponymail/ must never
        contain the real body text that was in the original (pre-strip)
        recorded response -- see tests/fixtures/ponymail/README.md."""
        for fixture_path in sorted(FIXTURES_DIR.iterdir()):
            if fixture_path.name == "README.md":
                continue
            raw = fixture_path.read_bytes()
            assert STRIPPED_BODY_CANARY.encode() not in raw, (
                f"body content found in committed fixture {fixture_path.name}"
            )
            # A second, independently-recalled body fragment from the same
            # original message (defense in depth against a too-narrow
            # canary).
            assert b"information contained in this communication" not in raw, (
                f"body content found in committed fixture {fixture_path.name}"
            )


def pa_is_string_like(pa_type) -> bool:
    import pyarrow as pa

    return pa.types.is_string(pa_type) or pa.types.is_list(pa_type)


def _assert_no_body_leak(value, needle: str, column_name: str) -> None:
    if value is None:
        return
    if isinstance(value, str):
        assert needle not in value, f"body/subject leak in column {column_name!r}: {value!r}"
    elif isinstance(value, list):
        for item in value:
            _assert_no_body_leak(item, needle, column_name)
