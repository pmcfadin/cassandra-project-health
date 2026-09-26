"""Tests for project_health.classify.sample (issue #44; DECISIONS.md D18).

All tests are fully offline: dev@ fetches go through an injected
`httpx.MockTransport` built from the existing `tests/fixtures/text_fetch/*`
synthetic Pony Mail fixtures (reused as-is -- issue #43 already built these
as synthetic, self-contained month digests), JIRA scanning goes through a
small synthetic in-memory transport built inline (synthetic issue/comment
text throughout, never real Cassandra text), and the dev@ metadata frame
comes from a tiny synthetic Parquet file written to `tmp_path`.
`tests/conftest.py`'s suite-wide network block would fail any real request
regardless.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import polars as pl
import pytest

from project_health.classify.sample import (
    JiraScanStats,
    _PacedJiraScanClient,
    allocate_with_capacity,
    build_corpus,
    build_manifest,
    deterministic_sample,
    enrichment_hits,
    is_english_heuristic,
    largest_remainder_allocate,
    load_dev_metadata_frame,
    load_enrichment_filters,
    render_public_manifest_markdown,
    run_pilot_sample,
    scan_dev_candidates,
    scan_jira_candidates,
    select_enrichment,
    select_prevalence,
    write_corpus_jsonl,
)
from project_health.classify.text_fetch import PonyMailTextFetcher

FIXTURES = Path(__file__).parent / "fixtures" / "text_fetch"
ENRICHMENT_FILTERS_PATH = (
    Path(__file__).parent.parent
    / "src"
    / "project_health"
    / "classify"
    / "enrichment_filters_v2.yaml"
)

AUTOMATED_SENDER_PATTERNS = [
    {"regex": r"(?i)\[bot\]|noreply@|jenkins@|-bot$"},
]


def _load_fixture(name: str) -> dict:
    with open(FIXTURES / name, encoding="utf-8") as fh:
        return json.load(fh)


# A small, self-contained synthetic Pony Mail month digest, purpose-built for
# this module's tests (rather than reusing issue #43's shared fixtures,
# whose `<reply-thread-1@...>` message deliberately opens with an "On ...
# wrote:" line and so strips to empty text under `preprocess.py`'s
# documented top-posting rule -- correct there, just not useful for
# exercising parent-text resolution here).
_SAMPLE_MONTH = {
    "hits": 4,
    "emails": [
        {
            "message-id": "<root@example.com>",
            "mid": "sampleroot0001",
            "from": "Alex Person <alex@example.com>",
            "in-reply-to": "",
            "subject": "Discuss: shared testing module",
            "body": (
                "I would like to propose a shared testing module for the ecosystem "
                "so downstream projects can reuse fixtures."
            ),
        },
        {
            "message-id": "<reply@example.com>",
            "mid": "samplereply0002",
            "from": "Bailey Person <bailey@example.com>",
            "in-reply-to": "<root@example.com>",
            "subject": "Re: Discuss: shared testing module",
            "body": (
                "Thanks for raising this, I think it makes sense.\n\n"
                "On Tue, Sep 1, 2026 at 9:00 AM, Alex Person <alex@example.com> wrote:\n"
                "> I would like to propose a shared testing module\n\n"
                "Let's move forward with it."
            ),
        },
        {
            "message-id": "<attack@example.com>",
            "mid": "sampleattack0003",
            "from": "Casey Person <casey@example.com>",
            "in-reply-to": "<reply@example.com>",
            "subject": "Re: Discuss: shared testing module",
            "body": (
                "I don't think this benchmark holds up and honestly you clearly have "
                "no idea how this module works -- maybe read the code before proposing "
                "changes."
            ),
        },
        {
            "message-id": "<bot@example.com>",
            "mid": "samplebot0004",
            "from": "Jenkins CI <jenkins@example.com>",
            "in-reply-to": "",
            "subject": "Build notification",
            "body": "Automated build notification: build #123 succeeded on branch trunk.",
        },
    ],
}


# --- is_english_heuristic ------------------------------------------------


class TestIsEnglishHeuristic:
    def test_english_prose_with_stopwords_is_english(self):
        assert is_english_heuristic("I think this patch is not quite ready for the merge.")

    def test_empty_text_is_not_english(self):
        assert not is_english_heuristic("")
        assert not is_english_heuristic("   ")

    def test_predominantly_non_ascii_with_no_stopwords_is_not_english(self):
        text = "这是一个关于压缩策略的讨论,我们需要考虑很多因素和权衡取舍的问题"
        assert not is_english_heuristic(text)

    def test_occasional_accented_name_does_not_flip_english_text(self):
        text = "Thanks for the patch, Jaroslav — this looks good to merge."
        assert is_english_heuristic(text)


# --- enrichment filters ----------------------------------------------------


class TestEnrichmentFilters:
    def test_loads_all_five_rare_labels(self):
        filters = load_enrichment_filters(ENRICHMENT_FILTERS_PATH)
        assert set(filters) == {
            "personal_attack",
            "gatekeeping",
            "dismissiveness",
            "status_authority_invocation",
            "sarcasm",
        }

    def test_personal_attack_term_matches_synthetic_fixture_text(self):
        filters = load_enrichment_filters(ENRICHMENT_FILTERS_PATH)
        text = (
            "And honestly you clearly have no idea how this module works -- "
            "maybe read the code before proposing changes."
        )
        assert "personal_attack" in enrichment_hits(text, filters)

    def test_clean_technical_text_has_no_hits(self):
        filters = load_enrichment_filters(ENRICHMENT_FILTERS_PATH)
        text = "The compaction path needs its own lock to avoid contention under load."
        assert enrichment_hits(text, filters) == frozenset()

    def test_status_authority_invocation_term_matches(self):
        filters = load_enrichment_filters(ENRICHMENT_FILTERS_PATH)
        text = "I designed this subsystem originally. It stays as-is."
        assert "status_authority_invocation" in enrichment_hits(text, filters)


# --- deterministic_sample / allocation --------------------------------------


class TestDeterministicSample:
    def test_same_seed_same_pick_regardless_of_input_order(self):
        ids = [f"id-{i}" for i in range(20)]
        picked_a = deterministic_sample(42, "ns", ids, 5)
        picked_b = deterministic_sample(42, "ns", list(reversed(ids)), 5)
        assert set(picked_a) == set(picked_b)
        assert len(picked_a) == 5

    def test_different_seed_can_pick_differently(self):
        ids = [f"id-{i}" for i in range(20)]
        picked_a = deterministic_sample(1, "ns", ids, 5)
        picked_b = deterministic_sample(2, "ns", ids, 5)
        assert picked_a != picked_b

    def test_different_namespace_can_pick_differently_for_same_seed(self):
        ids = [f"id-{i}" for i in range(20)]
        picked_a = deterministic_sample(1, "ns-a", ids, 5)
        picked_b = deterministic_sample(1, "ns-b", ids, 5)
        assert picked_a != picked_b

    def test_k_larger_than_pool_returns_whole_pool(self):
        ids = ["a", "b", "c"]
        assert set(deterministic_sample(1, "ns", ids, 10)) == {"a", "b", "c"}

    def test_k_zero_returns_empty(self):
        assert deterministic_sample(1, "ns", ["a", "b"], 0) == []


class TestLargestRemainderAllocate:
    def test_sums_to_target(self):
        alloc = largest_remainder_allocate(10, {"a": 1.0, "b": 1.0, "c": 1.0})
        assert sum(alloc.values()) == 10

    def test_proportional_split(self):
        alloc = largest_remainder_allocate(100, {"a": 3.0, "b": 1.0})
        assert alloc["a"] == 75
        assert alloc["b"] == 25

    def test_zero_total_weight_returns_zeros(self):
        alloc = largest_remainder_allocate(10, {"a": 0.0, "b": 0.0})
        assert alloc == {"a": 0, "b": 0}


class TestAllocateWithCapacity:
    def test_redistributes_shortfall_to_other_keys(self):
        alloc = allocate_with_capacity(10, {"a": 1.0, "b": 1.0}, {"a": 2, "b": 100})
        assert alloc["a"] == 2
        assert alloc["b"] == 8
        assert sum(alloc.values()) == 10

    def test_never_exceeds_total_capacity(self):
        alloc = allocate_with_capacity(10, {"a": 1.0, "b": 1.0}, {"a": 2, "b": 3})
        assert sum(alloc.values()) == 5
        assert alloc["a"] <= 2
        assert alloc["b"] <= 3


# --- load_dev_metadata_frame -------------------------------------------------


def _write_dev_parquet(tmp_path: Path, rows: list[dict], subdir: str = "data") -> Path:
    data_dir = tmp_path / subdir
    message_dir = data_dir / "raw" / "ponymail" / "message" / "date=2026-09-26"
    message_dir.mkdir(parents=True)
    df = pl.DataFrame(rows)
    df.write_parquet(message_dir / "part.parquet")
    return data_dir


class TestLoadDevMetadataFrame:
    def test_filters_by_list_window_and_automated_sender(self, tmp_path):
        rows = [
            {
                "message_id": "<in-window@example.com>",
                "list": "dev",
                "occurred_at": datetime(2020, 5, 1, tzinfo=timezone.utc),
                "sender_raw_value": "person@example.com",
            },
            {
                "message_id": "<too-old@example.com>",
                "list": "dev",
                "occurred_at": datetime(2010, 1, 1, tzinfo=timezone.utc),
                "sender_raw_value": "person@example.com",
            },
            {
                "message_id": "<wrong-list@example.com>",
                "list": "user",
                "occurred_at": datetime(2020, 5, 1, tzinfo=timezone.utc),
                "sender_raw_value": "person@example.com",
            },
            {
                "message_id": "<automated@example.com>",
                "list": "dev",
                "occurred_at": datetime(2020, 5, 1, tzinfo=timezone.utc),
                "sender_raw_value": "jenkins@example.com",
            },
        ]
        data_dir = _write_dev_parquet(tmp_path, rows)
        frame = load_dev_metadata_frame(
            data_dir, "dev", AUTOMATED_SENDER_PATTERNS, date(2017, 1, 1), date(2026, 8, 31)
        )
        assert [row.message_id for row in frame] == ["<in-window@example.com>"]
        assert frame[0].year == 2020
        assert frame[0].year_month == "2020-05"

    def test_missing_parquet_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_dev_metadata_frame(
                tmp_path / "no-data", "dev", [], date(2017, 1, 1), date(2026, 8, 31)
            )


# --- scan_dev_candidates (reuses issue #43's synthetic Pony Mail fixtures) ---


def _ponymail_fetcher() -> PonyMailTextFetcher:
    months = {"2026-09": _SAMPLE_MONTH}

    def handler(request: httpx.Request) -> httpx.Response:
        params = parse_qs(request.url.query.decode())
        year_month = params["d"][0]
        if year_month not in months:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=months[year_month])

    return PonyMailTextFetcher(
        transport=httpx.MockTransport(handler), min_request_interval=0, sleep_fn=lambda _s: None
    )


class TestScanDevCandidates:
    def _frame_rows(self):
        from project_health.classify.sample import DevFrameRow

        return [
            DevFrameRow("<root@example.com>", 2026, "2026-09"),
            DevFrameRow("<reply@example.com>", 2026, "2026-09"),
            DevFrameRow("<attack@example.com>", 2026, "2026-09"),
            DevFrameRow("<bot@example.com>", 2026, "2026-09"),
        ]

    def test_eligible_candidates_get_text_and_archive_url(self):
        fetcher = _ponymail_fetcher()
        filters = load_enrichment_filters(ENRICHMENT_FILTERS_PATH)
        candidates, text_by_id, parent_by_id = scan_dev_candidates(
            fetcher, self._frame_rows(), "example.org", "dev", AUTOMATED_SENDER_PATTERNS, filters
        )
        by_id = {c.message_id: c for c in candidates}
        root = by_id["<root@example.com>"]
        assert root.eligible
        assert root.archive_url == "https://lists.apache.org/thread/sampleroot0001"
        assert "<root@example.com>" in text_by_id
        assert parent_by_id["<root@example.com>"] is None

    def test_reply_resolves_parent_text(self):
        fetcher = _ponymail_fetcher()
        filters = load_enrichment_filters(ENRICHMENT_FILTERS_PATH)
        _candidates, text_by_id, parent_by_id = scan_dev_candidates(
            fetcher, self._frame_rows(), "example.org", "dev", AUTOMATED_SENDER_PATTERNS, filters
        )
        assert "<reply@example.com>" in text_by_id
        assert parent_by_id["<reply@example.com>"] is not None
        assert "shared testing module" in parent_by_id["<reply@example.com>"]

    def test_personal_attack_message_is_flagged(self):
        fetcher = _ponymail_fetcher()
        filters = load_enrichment_filters(ENRICHMENT_FILTERS_PATH)
        candidates, _text, _parent = scan_dev_candidates(
            fetcher, self._frame_rows(), "example.org", "dev", AUTOMATED_SENDER_PATTERNS, filters
        )
        by_id = {c.message_id: c for c in candidates}
        assert "personal_attack" in by_id["<attack@example.com>"].hits

    def test_automated_sender_is_excluded_entirely(self):
        fetcher = _ponymail_fetcher()
        filters = load_enrichment_filters(ENRICHMENT_FILTERS_PATH)
        candidates, text_by_id, _parent = scan_dev_candidates(
            fetcher, self._frame_rows(), "example.org", "dev", AUTOMATED_SENDER_PATTERNS, filters
        )
        ids = {c.message_id for c in candidates}
        assert "<bot@example.com>" not in ids
        assert "<bot@example.com>" not in text_by_id


# --- scan_jira_candidates (synthetic transport) ------------------------------


def _jira_issue_key(index: int) -> str:
    return f"SYNTH-{1000 + index}"


def _make_jira_transport(
    total_issues: int, comments_by_issue: dict[str, list[dict]], call_log: list[str] | None = None
):
    keys_in_order = [_jira_issue_key(i) for i in range(total_issues)]

    def handler(request: httpx.Request) -> httpx.Response:
        if call_log is not None:
            call_log.append(str(request.url))
        if request.url.path.endswith("/rest/api/2/search"):
            params = parse_qs(request.url.query.decode())
            max_results = int(params.get("maxResults", ["50"])[0])
            if max_results == 0:
                return httpx.Response(200, json={"total": total_issues})
            start_at = int(params.get("startAt", ["0"])[0])
            page_keys = keys_in_order[start_at : start_at + max_results]
            return httpx.Response(
                200,
                json={
                    "total": total_issues,
                    "startAt": start_at,
                    "issues": [{"key": k, "fields": {}} for k in page_keys],
                },
            )
        if request.url.path.endswith("/comment"):
            issue_key = request.url.path.split("/")[-2]
            comments = comments_by_issue.get(issue_key, [])
            if not comments:
                return httpx.Response(404, json={"errorMessages": ["not found"]})
            return httpx.Response(200, json={"total": len(comments), "comments": comments})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _comment(comment_id: str, created: str, author: str, body: str) -> dict:
    return {
        "id": comment_id,
        "created": created,
        "author": {"name": author},
        "body": body,
    }


def _jira_client(transport: httpx.MockTransport) -> _PacedJiraScanClient:
    return _PacedJiraScanClient(
        "https://issues.apache.org/jira",
        transport=transport,
        min_request_interval=0,
        sleep_fn=lambda _s: None,
    )


class TestScanJiraCandidates:
    # A single-year window keeps these unit tests to one population-count
    # call + a small number of block-search calls, rather than exercising
    # the full 10-year default frame every time.
    ONE_YEAR = (date(2020, 1, 1), date(2020, 12, 31))

    def test_finds_eligible_comments_in_window(self):
        comments_by_issue = {
            _jira_issue_key(0): [
                _comment(
                    "1",
                    "2020-03-01T10:00:00.000+0000",
                    "alice",
                    "This is a normal comment with enough words.",
                ),
                _comment(
                    "2",
                    "2020-03-02T10:00:00.000+0000",
                    "bob",
                    "I designed this subsystem originally. It stays as-is.",
                ),
            ]
        }
        transport = _make_jira_transport(1, comments_by_issue)
        client = _jira_client(transport)
        filters = load_enrichment_filters(ENRICHMENT_FILTERS_PATH)
        start, end = self.ONE_YEAR
        candidates, text_by_id, parent_by_id, stats = scan_jira_candidates(
            client,
            seed=1,
            project_key="SYNTH",
            automated_sender_patterns=AUTOMATED_SENDER_PATTERNS,
            enrichment_filter_map=filters,
            start=start,
            end=end,
            total_issue_target=10,
            min_issues_per_year=10,
            block_size=10,
        )
        assert stats.comments_eligible == 2
        assert {c.comment_id for c in candidates} == {"1", "2"}
        second = next(c for c in candidates if c.comment_id == "2")
        assert "status_authority_invocation" in second.hits
        assert parent_by_id["2"] is not None
        assert "1" in text_by_id
        assert stats.year_issue_counts == {2020: 1}

    def test_excludes_automated_author(self):
        comments_by_issue = {
            _jira_issue_key(0): [
                _comment(
                    "1",
                    "2020-03-01T10:00:00.000+0000",
                    "jira-bot",
                    "Automated notification body here [bot].",
                ),
            ]
        }
        transport = _make_jira_transport(1, comments_by_issue)
        client = _jira_client(transport)
        filters = load_enrichment_filters(ENRICHMENT_FILTERS_PATH)
        start, end = self.ONE_YEAR
        candidates, _text, _parent, stats = scan_jira_candidates(
            client,
            seed=1,
            project_key="SYNTH",
            automated_sender_patterns=AUTOMATED_SENDER_PATTERNS,
            enrichment_filter_map=filters,
            start=start,
            end=end,
            total_issue_target=10,
            min_issues_per_year=10,
            block_size=10,
        )
        assert candidates == []
        assert stats.comments_in_window == 1
        assert stats.comments_eligible == 0

    def test_excludes_comments_outside_date_window(self):
        comments_by_issue = {
            _jira_issue_key(0): [
                _comment(
                    "1",
                    "2010-03-01T10:00:00.000+0000",
                    "alice",
                    "Way too old to be in the sampling frame.",
                ),
            ]
        }
        transport = _make_jira_transport(1, comments_by_issue)
        client = _jira_client(transport)
        filters = load_enrichment_filters(ENRICHMENT_FILTERS_PATH)
        start, end = self.ONE_YEAR
        candidates, _text, _parent, stats = scan_jira_candidates(
            client,
            seed=1,
            project_key="SYNTH",
            automated_sender_patterns=AUTOMATED_SENDER_PATTERNS,
            enrichment_filter_map=filters,
            start=start,
            end=end,
            total_issue_target=10,
            min_issues_per_year=10,
            block_size=10,
        )
        assert candidates == []
        assert stats.comments_in_window == 0

    def test_respects_call_budget(self):
        comments_by_issue = {
            _jira_issue_key(i): [
                _comment(
                    str(i),
                    "2020-03-01T10:00:00.000+0000",
                    "alice",
                    "Enough words in this comment body.",
                )
            ]
            for i in range(20)
        }
        transport = _make_jira_transport(20, comments_by_issue)
        client = _jira_client(transport)
        filters = load_enrichment_filters(ENRICHMENT_FILTERS_PATH)
        start, end = self.ONE_YEAR
        _candidates, _text, _parent, stats = scan_jira_candidates(
            client,
            seed=1,
            project_key="SYNTH",
            automated_sender_patterns=AUTOMATED_SENDER_PATTERNS,
            enrichment_filter_map=filters,
            start=start,
            end=end,
            total_issue_target=20,
            min_issues_per_year=20,
            block_size=20,
            max_calls=5,
        )
        assert stats.budget_exhausted is True
        assert stats.api_calls <= 5

    def test_never_persists_comment_body_in_stats(self):
        comments_by_issue = {
            _jira_issue_key(0): [
                _comment(
                    "1",
                    "2020-03-01T10:00:00.000+0000",
                    "alice",
                    "SECRET_BODY_MARKER should never leak out.",
                ),
            ]
        }
        transport = _make_jira_transport(1, comments_by_issue)
        client = _jira_client(transport)
        filters = load_enrichment_filters(ENRICHMENT_FILTERS_PATH)
        start, end = self.ONE_YEAR
        candidates, _text, _parent, stats = scan_jira_candidates(
            client,
            seed=1,
            project_key="SYNTH",
            automated_sender_patterns=AUTOMATED_SENDER_PATTERNS,
            enrichment_filter_map=filters,
            start=start,
            end=end,
            total_issue_target=10,
            min_issues_per_year=10,
            block_size=10,
        )
        assert "SECRET_BODY_MARKER" not in repr(candidates)
        assert "SECRET_BODY_MARKER" not in repr(stats)

    def test_year_weights_come_from_population_not_scan_luck(self):
        """Issue #44 fix round 2 (1a): a transport whose per-year population
        counts are wildly uneven should be reflected exactly in
        `year_issue_counts`, read verbatim off each year's own JQL
        `maxResults=0` count -- never inferred from the (separately budgeted,
        potentially uneven) issue-key block search."""
        year_totals = {2020: 5, 2021: 500, 2022: 50}

        def handler(request: httpx.Request) -> httpx.Response:
            params = parse_qs(request.url.query.decode())
            jql = params.get("jql", [""])[0]
            max_results = int(params.get("maxResults", ["50"])[0])
            if request.url.path.endswith("/rest/api/2/search"):
                year = next(y for y in year_totals if f"{y}-01-01" in jql)
                total = year_totals[year]
                if max_results == 0:
                    return httpx.Response(200, json={"total": total})
                start_at = int(params.get("startAt", ["0"])[0])
                # However many issues exist for this year, return distinct,
                # year-tagged keys so each year's own scan can't collide.
                end_at = min(start_at + max_results, total)
                keys = [f"SYNTH-{year}-{i}" for i in range(start_at, end_at)]
                return httpx.Response(
                    200,
                    json={
                        "total": total,
                        "startAt": start_at,
                        "issues": [{"key": k} for k in keys],
                    },
                )
            return httpx.Response(404, json={"errorMessages": ["not found"]})

        client = _jira_client(httpx.MockTransport(handler))
        filters = load_enrichment_filters(ENRICHMENT_FILTERS_PATH)
        _candidates, _text, _parent, stats = scan_jira_candidates(
            client,
            seed=1,
            project_key="SYNTH",
            automated_sender_patterns=AUTOMATED_SENDER_PATTERNS,
            enrichment_filter_map=filters,
            start=date(2020, 1, 1),
            end=date(2022, 12, 31),
            total_issue_target=30,
            min_issues_per_year=2,
            block_size=10,
        )
        assert stats.year_issue_counts == year_totals
        # 2021 has 100x 2020's population -- it must get more of the (small)
        # issue-scan budget, never an equal or smaller share.
        assert stats.issues_scanned_by_year[2021] > stats.issues_scanned_by_year[2020]


# --- select_prevalence / select_enrichment -----------------------------------


def _dev_candidate(message_id, year, eligible=True, hits=frozenset()):
    from project_health.classify.sample import DevCandidate

    return DevCandidate(
        message_id=message_id,
        year=year,
        word_count=10,
        is_english=True,
        eligible=eligible,
        hits=hits,
        archive_url=f"https://lists.apache.org/thread/{message_id}",
    )


def _jira_candidate(comment_id, year, eligible=True, hits=frozenset()):
    from project_health.classify.sample import JiraCandidate

    return JiraCandidate(
        comment_id=comment_id,
        issue_key="SYNTH-1",
        year=year,
        word_count=10,
        is_english=True,
        eligible=eligible,
        hits=hits,
        archive_url=f"https://issues.apache.org/jira/browse/SYNTH-1?focusedCommentId={comment_id}",
    )


class TestSelectPrevalence:
    def test_achieves_target_when_pool_is_large_enough(self):
        dev = [_dev_candidate(f"m{i}", 2020 + (i % 3)) for i in range(300)]
        jira = [_jira_candidate(f"c{i}", 2020 + (i % 3)) for i in range(300)]
        stats = JiraScanStats(
            candidate_issue_total=1000,
            issues_scanned=100,
            comments_seen=300,
            comments_in_window=300,
            comments_eligible=300,
            api_calls=100,
            budget_exhausted=False,
            estimated_total_eligible=3000.0,
            year_issue_counts={2020: 1000, 2021: 1000, 2022: 1000},
        )
        dev_ids, jira_ids, sel_stats = select_prevalence(7, dev, jira, stats, target=150)
        assert len(dev_ids) + len(jira_ids) == 150
        assert len(set(dev_ids)) == len(dev_ids)
        assert len(set(jira_ids)) == len(jira_ids)
        assert sel_stats["achieved"] == 150

    def test_dev_heavy_weight_selects_more_dev_items(self):
        dev = [_dev_candidate(f"m{i}", 2020) for i in range(300)]
        jira = [_jira_candidate(f"c{i}", 2020) for i in range(300)]
        stats = JiraScanStats(
            1000, 100, 300, 300, 300, 100, False,
            estimated_total_eligible=1.0, year_issue_counts={2020: 300},
        )
        dev_ids, jira_ids, _stats = select_prevalence(7, dev, jira, stats, target=150)
        assert len(dev_ids) > len(jira_ids)

    def test_is_deterministic_across_repeated_calls(self):
        dev = [_dev_candidate(f"m{i}", 2020 + (i % 3)) for i in range(50)]
        jira = [_jira_candidate(f"c{i}", 2020 + (i % 3)) for i in range(50)]
        stats = JiraScanStats(
            200, 50, 50, 50, 50, 50, False,
            estimated_total_eligible=200.0,
            year_issue_counts={2020: 200, 2021: 200, 2022: 200},
        )
        result_a = select_prevalence(7, dev, jira, stats, target=40)
        result_b = select_prevalence(7, dev, jira, stats, target=40)
        assert result_a[0] == result_b[0]
        assert result_a[1] == result_b[1]

    def test_shortfall_in_one_source_never_borrows_from_the_other(self):
        # Only 3 dev items exist at all -- dev's allocation can never exceed 3,
        # even though its weight alone would otherwise entitle it to more.
        dev = [_dev_candidate(f"m{i}", 2020) for i in range(3)]
        jira = [_jira_candidate(f"c{i}", 2020) for i in range(300)]
        stats = JiraScanStats(
            1000, 100, 300, 300, 300, 100, False,
            estimated_total_eligible=1.0, year_issue_counts={2020: 1000},
        )
        dev_ids, jira_ids, _stats = select_prevalence(7, dev, jira, stats, target=150)
        assert len(dev_ids) == 3
        assert len(jira_ids) <= 300

    def test_jira_year_allocation_follows_population_not_scan_sample_counts(self):
        """Issue #44 fix round 2 (1a): give the scan an even per-year sample
        (100 eligible comments in each of three years) but a population
        wildly skewed toward one year, and confirm the *population* weight
        drives the year split -- not the scan's own even-looking sample,
        which is exactly the bug the coordinator flagged (year weights
        reflected which blocks the seed picked, not JIRA's real volume)."""
        jira = (
            [_jira_candidate(f"c2020-{i}", 2020) for i in range(100)]
            + [_jira_candidate(f"c2021-{i}", 2021) for i in range(100)]
            + [_jira_candidate(f"c2022-{i}", 2022) for i in range(100)]
        )
        stats = JiraScanStats(
            10000, 300, 300, 300, 300, 300, False,
            estimated_total_eligible=10000.0,
            # Population is 100x bigger in 2021 than in 2020/2022, even
            # though the scan itself sampled exactly 100 eligible comments
            # from each year above.
            year_issue_counts={2020: 10, 2021: 1000, 2022: 10},
        )
        _dev_ids, jira_ids, stats_out = select_prevalence(7, [], jira, stats, target=90)
        by_year = {2020: 0, 2021: 0, 2022: 0}
        for comment_id in jira_ids:
            year = int(comment_id.split("-")[0].removeprefix("c"))
            by_year[year] += 1
        assert by_year[2021] > by_year[2020]
        assert by_year[2021] > by_year[2022]
        assert stats_out["jira_by_year_population"] == {2020: 10, 2021: 1000, 2022: 10}


class TestSelectEnrichment:
    def test_only_selects_items_with_hits(self):
        dev = [_dev_candidate("m1", 2020, hits=frozenset({"sarcasm"}))]
        dev.append(_dev_candidate("m2", 2020, hits=frozenset()))
        selected, stats = select_enrichment(1, dev, [], set(), set(), target=10)
        assert [ref for _s, ref, _l in selected] == ["m1"]
        assert stats["achieved"] == 1

    def test_excludes_ids_already_in_prevalence(self):
        dev = [_dev_candidate("m1", 2020, hits=frozenset({"sarcasm"}))]
        selected, _stats = select_enrichment(1, dev, [], {"m1"}, set(), target=10)
        assert selected == []

    def test_disjoint_from_prevalence_and_labels_balanced(self):
        dev = [
            _dev_candidate(f"m{i}", 2020, hits=frozenset({"sarcasm"})) for i in range(10)
        ] + [_dev_candidate(f"g{i}", 2020, hits=frozenset({"gatekeeping"})) for i in range(10)]
        selected, stats = select_enrichment(1, dev, [], set(), set(), target=10)
        assert len(selected) == 10
        labels_selected = {label for _s, _r, label in selected}
        assert labels_selected == {"sarcasm", "gatekeeping"}

    def test_redistributes_when_a_label_has_no_candidates(self):
        dev = [_dev_candidate(f"m{i}", 2020, hits=frozenset({"sarcasm"})) for i in range(10)]
        selected, stats = select_enrichment(1, dev, [], set(), set(), target=10)
        assert len(selected) == 10
        assert all(label == "sarcasm" for _s, _r, label in selected)

    def test_a_candidate_hitting_multiple_labels_is_counted_once(self):
        dev = [_dev_candidate("m1", 2020, hits=frozenset({"sarcasm", "gatekeeping"}))]
        selected, _stats = select_enrichment(1, dev, [], set(), set(), target=10)
        assert len(selected) == 1


# --- build_corpus / write_corpus_jsonl / manifest ---------------------------


class TestCorpusAssembly:
    def _sample_items(self):
        dev = [_dev_candidate("m1", 2020)]
        jira = [_jira_candidate("c1", 2021)]
        dev_meta = {c.message_id: c for c in dev}
        jira_meta = {c.comment_id: c for c in jira}
        dev_text = {"m1": "Synthetic dev message text with enough words in it."}
        dev_parent = {"m1": None}
        jira_text = {"c1": "Synthetic jira comment text with enough words in it."}
        jira_parent = {"c1": "Synthetic parent comment text."}
        return build_corpus(
            ["m1"], ["c1"], [], dev_meta, jira_meta, dev_text, dev_parent, jira_text, jira_parent
        )

    def test_item_shape(self):
        items = self._sample_items()
        assert len(items) == 2
        mail_item = next(i for i in items if i.source == "mailing_list")
        assert mail_item.item_id == "mail:m1"
        assert mail_item.stratum == "prevalence"
        assert mail_item.parent_text is None
        assert len(mail_item.checksum) == 64

    def test_jira_item_id_includes_issue_key(self):
        items = self._sample_items()
        jira_item = next(i for i in items if i.source == "jira_comment")
        assert jira_item.item_id == "jira:SYNTH-1:c1"
        assert jira_item.parent_text == "Synthetic parent comment text."

    def test_write_corpus_jsonl_round_trips_and_checksums(self, tmp_path):
        items = self._sample_items()
        out_path = tmp_path / "corpus" / "v0" / "pilot.jsonl"
        checksum = write_corpus_jsonl(items, out_path)
        assert out_path.exists()
        assert checksum == __import__("hashlib").sha256(out_path.read_bytes()).hexdigest()
        lines = out_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        record = json.loads(lines[0])
        # Matches project_health.label.store's required corpus fields
        # exactly (`id, stratum, source, archive_url, text, parent_text`,
        # issue #44 fix round 2/3) plus the tolerated extras `year`/`checksum`.
        assert set(record) == {
            "id",
            "stratum",
            "source",
            "archive_url",
            "year",
            "text",
            "parent_text",
            "checksum",
        }
        assert isinstance(record["text"], str) and record["text"]

    def test_public_manifest_markdown_has_no_message_text(self, tmp_path):
        items = self._sample_items()
        out_path = tmp_path / "pilot.jsonl"
        checksum = write_corpus_jsonl(items, out_path)
        prevalence_stats = {
            "target": 150,
            "achieved": 2,
            "source_allocation_target": {"dev": 1, "jira": 1},
            "source_weights": {"dev": 1.0, "jira": 1.0},
        }
        enrichment_stats = {
            "target": 100,
            "achieved": 0,
            "label_candidate_counts": {
                label: 0
                for label in (
                    "personal_attack", "gatekeeping", "dismissiveness",
                    "status_authority_invocation", "sarcasm",
                )
            },
            "label_selected_counts": {
                label: 0
                for label in (
                    "personal_attack", "gatekeeping", "dismissiveness",
                    "status_authority_invocation", "sarcasm",
                )
            },
            "eligible_scanned": {"mailing_list": 10, "jira_comment": 10},
            "hit_rate_by_source": {"mailing_list": 0.0, "jira_comment": 0.0},
        }
        jira_stats = JiraScanStats(100, 10, 10, 10, 10, 10, False, 100.0)
        manifest = build_manifest(
            42, items, prevalence_stats, enrichment_stats, jira_stats, checksum
        )
        markdown = render_public_manifest_markdown(manifest)
        assert "Synthetic dev message text" not in markdown
        assert "Synthetic jira comment text" not in markdown
        assert "m1" not in markdown
        assert "c1" not in markdown
        assert str(42) in markdown
        assert checksum in markdown

    def test_corpus_loads_with_the_labeling_tools_own_loader(self, tmp_path):
        """Issue #44 fix round 2 (fix 3): a corpus this sampler writes must
        load with `project_health.label.store.load_corpus` unchanged -- the
        two modules are maintained separately (issue #46 landed after this
        one) and previously drifted (`item_id`/`message.text`/`parent.text`
        here vs. `id`/`text`/`parent_text` there). This is the regression
        test for that drift, using only synthetic text."""
        from project_health.label.store import load_corpus

        items = self._sample_items()
        out_path = tmp_path / "corpus" / "v0" / "pilot.jsonl"
        write_corpus_jsonl(items, out_path)

        loaded = load_corpus(out_path)
        assert {item.id for item in loaded} == {"mail:m1", "jira:SYNTH-1:c1"}
        mail_item = next(item for item in loaded if item.id == "mail:m1")
        assert mail_item.text == "Synthetic dev message text with enough words in it."
        assert mail_item.parent_text is None
        jira_item = next(item for item in loaded if item.id == "jira:SYNTH-1:c1")
        assert jira_item.parent_text == "Synthetic parent comment text."
        assert jira_item.source == "jira_comment"
        assert jira_item.checksum is not None


# --- run_pilot_sample end-to-end smoke test ----------------------------------


class TestRunPilotSampleEndToEnd:
    def _setup(self, tmp_path, subdir: str = "data"):
        rows = [
            {
                "message_id": "<root@example.com>",
                "list": "dev",
                "occurred_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
                "sender_raw_value": "alex@example.com",
            },
            {
                "message_id": "<reply@example.com>",
                "list": "dev",
                "occurred_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
                "sender_raw_value": "bailey@example.com",
            },
            {
                "message_id": "<attack@example.com>",
                "list": "dev",
                "occurred_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
                "sender_raw_value": "casey@example.com",
            },
        ]
        data_dir = _write_dev_parquet(tmp_path, rows, subdir=subdir)

        comments_by_issue = {
            _jira_issue_key(0): [
                _comment(
                    "1",
                    "2020-03-01T10:00:00.000+0000",
                    "alice",
                    "A normal jira comment with enough words.",
                ),
                _comment(
                    "2",
                    "2020-03-02T10:00:00.000+0000",
                    "bob",
                    "I designed this subsystem. It stays as-is.",
                ),
            ]
        }
        jira_transport = _make_jira_transport(1, comments_by_issue)
        jira_client = _PacedJiraScanClient(
            "https://issues.apache.org/jira",
            transport=jira_transport,
            min_request_interval=0,
            sleep_fn=lambda _s: None,
        )
        return data_dir, jira_client

    def test_writes_corpus_and_manifest(self, tmp_path):
        data_dir, jira_client = self._setup(tmp_path)
        result = run_pilot_sample(
            data_dir=data_dir,
            seed=99,
            domain="example.org",
            list_name="dev",
            jira_base_url="https://issues.apache.org/jira",
            jira_project_key="SYNTH",
            automated_sender_patterns=AUTOMATED_SENDER_PATTERNS,
            enrichment_filters_path=ENRICHMENT_FILTERS_PATH,
            corpus_output_path=tmp_path / "out" / "pilot.jsonl",
            manifest_output_path=tmp_path / "out" / "manifest.json",
            ponymail_fetcher=_ponymail_fetcher(),
            jira_client=jira_client,
            jira_total_issue_target=10,
            jira_min_issues_per_year=1,
            jira_block_size=10,
        )
        assert result.corpus_path.exists()
        assert result.manifest_path.exists()
        assert len(result.items) > 0
        assert result.manifest["seed"] == 99
        assert "corpus_checksum_sha256" in result.manifest

    def test_deterministic_across_two_runs(self, tmp_path):
        data_dir_a, jira_client_a = self._setup(tmp_path, subdir="data_a")
        data_dir_b, jira_client_b = self._setup(tmp_path, subdir="data_b")
        result_a = run_pilot_sample(
            data_dir=data_dir_a,
            seed=5,
            domain="example.org",
            list_name="dev",
            jira_base_url="https://issues.apache.org/jira",
            jira_project_key="SYNTH",
            automated_sender_patterns=AUTOMATED_SENDER_PATTERNS,
            enrichment_filters_path=ENRICHMENT_FILTERS_PATH,
            corpus_output_path=tmp_path / "out_a" / "pilot.jsonl",
            manifest_output_path=tmp_path / "out_a" / "manifest.json",
            ponymail_fetcher=_ponymail_fetcher(),
            jira_client=jira_client_a,
            jira_total_issue_target=10,
            jira_min_issues_per_year=1,
            jira_block_size=10,
        )
        result_b = run_pilot_sample(
            data_dir=data_dir_b,
            seed=5,
            domain="example.org",
            list_name="dev",
            jira_base_url="https://issues.apache.org/jira",
            jira_project_key="SYNTH",
            automated_sender_patterns=AUTOMATED_SENDER_PATTERNS,
            enrichment_filters_path=ENRICHMENT_FILTERS_PATH,
            corpus_output_path=tmp_path / "out_b" / "pilot.jsonl",
            manifest_output_path=tmp_path / "out_b" / "manifest.json",
            ponymail_fetcher=_ponymail_fetcher(),
            jira_client=jira_client_b,
            jira_total_issue_target=10,
            jira_min_issues_per_year=1,
            jira_block_size=10,
        )
        ids_a = sorted(i.item_id for i in result_a.items)
        ids_b = sorted(i.item_id for i in result_b.items)
        assert ids_a == ids_b
        checksum_a = result_a.manifest["corpus_checksum_sha256"]
        checksum_b = result_b.manifest["corpus_checksum_sha256"]
        assert checksum_a == checksum_b
