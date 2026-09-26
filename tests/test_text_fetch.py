"""Tests for project_health.classify.text_fetch (issue #43).

All tests are fully offline: every `httpx` call goes through an injected
`httpx.MockTransport` built from `tests/fixtures/text_fetch/*` (recorded
response *shape*, synthetic text throughout -- see
`tests/fixtures/text_fetch/README.md`) or small synthetic payloads built
inline. Retry/backoff tests inject a no-op `sleep_fn`. Nothing here touches
the network; `tests/conftest.py`'s suite-wide block would fail any real
request regardless.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from project_health.classify.text_fetch import (
    FetchError,
    JiraCommentRef,
    JiraCommentTextFetcher,
    MailMessageRef,
    PonyMailTextFetcher,
    PrivacyGuardError,
    build_state,
    fetch_and_preprocess_jira,
    fetch_and_preprocess_mail,
    guard_write_path,
    write_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "text_fetch"


def _load(name: str) -> dict:
    with open(FIXTURES / name, encoding="utf-8") as fh:
        return json.load(fh)


# --- Pony Mail transport -----------------------------------------------------


def _ponymail_transport(months: dict[str, dict], call_log: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        params = parse_qs(request.url.query.decode())
        year_month = params["d"][0]
        if call_log is not None:
            call_log.append(year_month)
        if year_month not in months:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=months[year_month])

    return httpx.MockTransport(handler)


def _ponymail_fetcher(
    months: dict[str, dict], call_log: list[str] | None = None
) -> PonyMailTextFetcher:
    return PonyMailTextFetcher(
        transport=_ponymail_transport(months, call_log),
        min_request_interval=0,
        sleep_fn=lambda _seconds: None,
    )


class TestPonyMailTextFetcherFetchMessages:
    def test_fetches_a_message_body_by_ref(self):
        months = {"2026-09": _load("ponymail_month_dev_2026-09.json")}
        fetcher = _ponymail_fetcher(months)
        ref = MailMessageRef("dev", "example.org", "2026-09", "<root-thread-1@example.com>")
        result = fetcher.fetch_messages([ref])
        assert set(result) == {"<root-thread-1@example.com>"}
        assert "shared testing module" in result["<root-thread-1@example.com>"].text

    def test_only_fetches_the_months_actually_referenced(self):
        months = {
            "2026-09": _load("ponymail_month_dev_2026-09.json"),
            "2026-08": _load("ponymail_month_dev_2026-08.json"),
        }
        call_log: list[str] = []
        fetcher = _ponymail_fetcher(months, call_log)
        refs = [
            MailMessageRef("dev", "example.org", "2026-09", "<root-thread-1@example.com>"),
            MailMessageRef("dev", "example.org", "2026-09", "<reply-thread-1@example.com>"),
        ]
        fetcher.fetch_messages(refs)
        assert call_log == ["2026-09"]  # one request, not one per ref
        assert "2026-08" not in call_log

    def test_caches_a_month_across_multiple_fetch_calls(self):
        months = {"2026-09": _load("ponymail_month_dev_2026-09.json")}
        call_log: list[str] = []
        fetcher = _ponymail_fetcher(months, call_log)
        ref = MailMessageRef("dev", "example.org", "2026-09", "<root-thread-1@example.com>")
        fetcher.fetch_messages([ref])
        fetcher.fetch_messages([ref])
        assert call_log == ["2026-09"]

    def test_missing_message_id_is_omitted_from_result(self):
        months = {"2026-09": _load("ponymail_month_dev_2026-09.json")}
        fetcher = _ponymail_fetcher(months)
        ref = MailMessageRef("dev", "example.org", "2026-09", "<does-not-exist@example.com>")
        assert fetcher.fetch_messages([ref]) == {}

    def test_unknown_month_returns_empty_without_raising(self):
        fetcher = _ponymail_fetcher({})
        ref = MailMessageRef("dev", "example.org", "2099-01", "<anything@example.com>")
        assert fetcher.fetch_messages([ref]) == {}


class TestPonyMailTextFetcherResolveParent:
    def test_resolves_parent_within_the_same_month(self):
        months = {"2026-09": _load("ponymail_month_dev_2026-09.json")}
        fetcher = _ponymail_fetcher(months)
        ref = MailMessageRef("dev", "example.org", "2026-09", "<reply-thread-1@example.com>")
        raw = fetcher.fetch_messages([ref])[ref.message_id]
        parent = fetcher.resolve_parent(ref, raw)
        assert parent is not None
        assert parent.message_id == "<root-thread-1@example.com>"

    def test_thread_root_has_no_parent(self):
        months = {"2026-09": _load("ponymail_month_dev_2026-09.json")}
        fetcher = _ponymail_fetcher(months)
        ref = MailMessageRef("dev", "example.org", "2026-09", "<root-thread-1@example.com>")
        raw = fetcher.fetch_messages([ref])[ref.message_id]
        assert fetcher.resolve_parent(ref, raw) is None

    def test_falls_back_to_previous_month_for_the_parent(self):
        months = {
            "2026-09": _load("ponymail_month_dev_2026-09.json"),
            "2026-08": _load("ponymail_month_dev_2026-08.json"),
        }
        fetcher = _ponymail_fetcher(months)
        ref = MailMessageRef("dev", "example.org", "2026-09", "<cross-month-reply@example.com>")
        raw = fetcher.fetch_messages([ref])[ref.message_id]
        parent = fetcher.resolve_parent(ref, raw)
        assert parent is not None
        assert parent.message_id == "<root-in-previous-month@example.com>"

    def test_unresolvable_parent_returns_none(self):
        months = {"2026-09": _load("ponymail_month_dev_2026-09.json")}
        fetcher = _ponymail_fetcher(months)
        ref = MailMessageRef("dev", "example.org", "2026-09", "<cross-month-reply@example.com>")
        raw = fetcher.fetch_messages([ref])[ref.message_id]
        # previous month (2026-08) isn't in `months` at all here -> 404 -> None
        assert fetcher.resolve_parent(ref, raw, lookback_months=1) is None


class TestPonyMailRetryAndErrors:
    def test_retries_on_500_then_succeeds(self):
        months = {"2026-09": _load("ponymail_month_dev_2026-09.json")}
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 2:
                return httpx.Response(500)
            return httpx.Response(200, json=months["2026-09"])

        fetcher = PonyMailTextFetcher(
            transport=httpx.MockTransport(handler),
            min_request_interval=0,
            sleep_fn=lambda _seconds: None,
        )
        ref = MailMessageRef("dev", "example.org", "2026-09", "<root-thread-1@example.com>")
        result = fetcher.fetch_messages([ref])
        assert attempts["n"] == 2
        assert ref.message_id in result

    def test_raises_fetch_error_after_exhausting_retries(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        fetcher = PonyMailTextFetcher(
            transport=httpx.MockTransport(handler),
            max_retries=2,
            min_request_interval=0,
            sleep_fn=lambda _seconds: None,
        )
        ref = MailMessageRef("dev", "example.org", "2026-09", "<root-thread-1@example.com>")
        with pytest.raises(FetchError):
            fetcher.fetch_messages([ref])


# --- JIRA transport -----------------------------------------------------


def _jira_transport(comments_by_issue: dict[str, dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.split("/")[-2]
        if key not in comments_by_issue:
            return httpx.Response(404, json={"errorMessages": ["not found"]})
        return httpx.Response(200, json=comments_by_issue[key])

    return httpx.MockTransport(handler)


def _jira_fetcher(comments_by_issue: dict[str, dict]) -> JiraCommentTextFetcher:
    return JiraCommentTextFetcher(
        "https://issues.apache.org/jira",
        transport=_jira_transport(comments_by_issue),
        min_request_interval=0,
        sleep_fn=lambda _seconds: None,
    )


class TestJiraCommentTextFetcher:
    def test_fetches_a_comment_body_by_ref(self):
        comments = {"EXAMPLE-1": _load("jira_comments_EXAMPLE-1.json")}
        fetcher = _jira_fetcher(comments)
        ref = JiraCommentRef("EXAMPLE-1", "90000001")
        result = fetcher.fetch_comments([ref])
        assert set(result) == {"90000001"}
        assert "Repro steps" in result["90000001"].text

    def test_missing_comment_id_is_omitted(self):
        comments = {"EXAMPLE-1": _load("jira_comments_EXAMPLE-1.json")}
        fetcher = _jira_fetcher(comments)
        ref = JiraCommentRef("EXAMPLE-1", "99999999")
        assert fetcher.fetch_comments([ref]) == {}

    def test_unknown_issue_returns_empty(self):
        fetcher = _jira_fetcher({})
        ref = JiraCommentRef("NOPE-1", "1")
        assert fetcher.fetch_comments([ref]) == {}

    def test_resolve_parent_is_the_previous_comment_by_created_order(self):
        comments = {"EXAMPLE-1": _load("jira_comments_EXAMPLE-1.json")}
        fetcher = _jira_fetcher(comments)
        parent = fetcher.resolve_parent(JiraCommentRef("EXAMPLE-1", "90000003"))
        assert parent is not None
        assert parent.comment_id == "90000002"

    def test_first_comment_has_no_parent(self):
        comments = {"EXAMPLE-1": _load("jira_comments_EXAMPLE-1.json")}
        fetcher = _jira_fetcher(comments)
        assert fetcher.resolve_parent(JiraCommentRef("EXAMPLE-1", "90000001")) is None

    def test_caches_an_issue_across_multiple_calls(self):
        comments = {"EXAMPLE-1": _load("jira_comments_EXAMPLE-1.json")}
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(200, json=comments["EXAMPLE-1"])

        fetcher = JiraCommentTextFetcher(
            "https://issues.apache.org/jira",
            transport=httpx.MockTransport(handler),
            min_request_interval=0,
            sleep_fn=lambda _seconds: None,
        )
        fetcher.fetch_comments([JiraCommentRef("EXAMPLE-1", "90000001")])
        fetcher.resolve_parent(JiraCommentRef("EXAMPLE-1", "90000003"))
        assert call_count["n"] == 1


# --- Fetch + preprocess + automated-sender filtering, and state shape -------


class TestFetchAndPreprocessMail:
    def test_returns_state_shape_and_resolves_parent(self):
        months = {"2026-09": _load("ponymail_month_dev_2026-09.json")}
        fetcher = _ponymail_fetcher(months)
        refs = [MailMessageRef("dev", "example.org", "2026-09", "<reply-thread-1@example.com>")]
        result = fetch_and_preprocess_mail(fetcher, refs)
        state = result["<reply-thread-1@example.com>"]
        assert set(state) == {"message", "parent"}
        assert set(state["message"]) == {"text", "source"}
        assert state["message"]["source"] == "mailing_list"
        assert "shared testing module" in state["parent"]["text"]
        # quoting and signature were stripped from the message text itself
        assert "wrote:" not in state["message"]["text"]
        assert "Example Corp" not in state["message"]["text"]

    def test_thread_root_has_no_parent_key_value(self):
        months = {"2026-09": _load("ponymail_month_dev_2026-09.json")}
        fetcher = _ponymail_fetcher(months)
        refs = [MailMessageRef("dev", "example.org", "2026-09", "<root-thread-1@example.com>")]
        result = fetch_and_preprocess_mail(fetcher, refs)
        assert result["<root-thread-1@example.com>"]["parent"] is None

    def test_drops_automated_sender_entirely(self):
        months = {"2026-09": _load("ponymail_month_dev_2026-09.json")}
        fetcher = _ponymail_fetcher(months)
        refs = [MailMessageRef("dev", "example.org", "2026-09", "<automated-notice@example.com>")]
        result = fetch_and_preprocess_mail(fetcher, refs, automated_sender_patterns=[r"\(JIRA\)"])
        assert result == {}

    def test_non_automated_sender_is_kept(self):
        months = {"2026-09": _load("ponymail_month_dev_2026-09.json")}
        fetcher = _ponymail_fetcher(months)
        refs = [MailMessageRef("dev", "example.org", "2026-09", "<root-thread-1@example.com>")]
        result = fetch_and_preprocess_mail(fetcher, refs, automated_sender_patterns=[r"\(JIRA\)"])
        assert "<root-thread-1@example.com>" in result

    def test_stack_trace_and_personal_attack_message_still_preprocesses(self):
        months = {"2026-09": _load("ponymail_month_dev_2026-09.json")}
        fetcher = _ponymail_fetcher(months)
        message_id = "<reply-thread-1-attack@example.com>"
        refs = [MailMessageRef("dev", "example.org", "2026-09", message_id)]
        result = fetch_and_preprocess_mail(fetcher, refs)
        text = result[message_id]["message"]["text"]
        assert "[stacktrace]" in text
        assert "do_thing" not in text


class TestFetchAndPreprocessJira:
    def test_returns_state_shape_with_wiki_markup_stripped(self):
        comments = {"EXAMPLE-1": _load("jira_comments_EXAMPLE-1.json")}
        fetcher = _jira_fetcher(comments)
        refs = [JiraCommentRef("EXAMPLE-1", "90000001")]
        result = fetch_and_preprocess_jira(fetcher, refs)
        state = result["90000001"]
        assert state["message"]["source"] == "jira_comment"
        assert "h2." not in state["message"]["text"]
        assert "[code]" in state["message"]["text"]
        assert state["parent"] is None  # first comment on the issue

    def test_resolves_parent_comment(self):
        comments = {"EXAMPLE-1": _load("jira_comments_EXAMPLE-1.json")}
        fetcher = _jira_fetcher(comments)
        refs = [JiraCommentRef("EXAMPLE-1", "90000003")]
        result = fetch_and_preprocess_jira(fetcher, refs)
        assert result["90000003"]["parent"] is not None

    def test_drops_automated_sender_entirely(self):
        comments = {"EXAMPLE-1": _load("jira_comments_EXAMPLE-1.json")}
        fetcher = _jira_fetcher(comments)
        refs = [JiraCommentRef("EXAMPLE-1", "90000002")]  # author "githubbot"
        result = fetch_and_preprocess_jira(fetcher, refs, automated_sender_patterns=[r"(?i)bot$"])
        assert result == {}


class TestBuildState:
    def test_shape_matches_question_set_state_schema_exactly(self):
        state = build_state("hello", "mailing_list", "parent text")
        assert state == {
            "message": {"text": "hello", "source": "mailing_list"},
            "parent": {"text": "parent text"},
        }

    def test_no_parent_is_none_not_a_dict(self):
        state = build_state("hello", "jira_comment", None)
        assert state["parent"] is None


# --- Privacy guard -----------------------------------------------------------


class TestPrivacyGuard:
    def test_refuses_to_write_under_the_repo_root(self, tmp_path):
        repo_root = Path(__file__).resolve().parents[1]
        target = repo_root / "some_leaked_pilot_text.txt"
        with pytest.raises(PrivacyGuardError):
            guard_write_path(target)

    def test_refuses_to_write_under_a_nested_repo_path(self):
        repo_root = Path(__file__).resolve().parents[1]
        target = repo_root / "src" / "project_health" / "classify" / "leaked.txt"
        with pytest.raises(PrivacyGuardError):
            guard_write_path(target)

    def test_refuses_to_write_under_a_data_directory_outside_the_repo(self, tmp_path):
        target = tmp_path / "data" / "pilot_corpus.jsonl"
        with pytest.raises(PrivacyGuardError):
            guard_write_path(target, repo_root=tmp_path / "unrelated_repo_root")

    def test_allows_a_safe_path_outside_repo_and_data(self, tmp_path):
        target = tmp_path / "private_benchmark" / "corpus.jsonl"
        resolved = guard_write_path(target, repo_root=tmp_path / "unrelated_repo_root")
        assert resolved == target.resolve()

    def test_write_text_raises_and_does_not_create_the_file(self, tmp_path):
        repo_root = Path(__file__).resolve().parents[1]
        target = repo_root / "should_never_exist_leaked.txt"
        with pytest.raises(PrivacyGuardError):
            write_text(target, "some classified pilot text")
        assert not target.exists()

    def test_write_text_succeeds_to_a_safe_path(self, tmp_path):
        target = tmp_path / "private_benchmark" / "corpus.jsonl"
        write_text(target, "synthetic content", repo_root=tmp_path / "unrelated_repo_root")
        assert target.read_text(encoding="utf-8") == "synthetic content"


# --- Fixture hygiene ----------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_ALLOWED_SYNTHETIC_DOMAINS = ("@example.com", "@example.org", "@example.net")


def test_fixtures_contain_no_real_looking_email_addresses():
    """Every email-shaped substring in every text_fetch fixture must end in
    an allowed synthetic domain -- this project's convention for "this is
    definitely not a real address" (issue #43's requirement to scan
    fixtures for anything resembling a real email address)."""
    checked_any = False
    for path in FIXTURES.glob("*"):
        if path.suffix not in (".json", ".md"):
            continue
        checked_any = True
        content = path.read_text(encoding="utf-8")
        for match in _EMAIL_RE.findall(content):
            assert match.endswith(_ALLOWED_SYNTHETIC_DOMAINS), (
                f"{path.name} contains a real-looking email address: {match!r}"
            )
    assert checked_any, "expected at least one fixture file to scan"
