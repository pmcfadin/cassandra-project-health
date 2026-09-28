"""Tests for project_health.governance.verify_sources (D24, issue #93).

Offline unit tests for the normalization/matching helpers plus
`iter_sourced_items`'s structural walk. The one test that actually hits the
network (re-fetching every real `governance-policy.yaml` source_url) is
marked `live_network` and skipped unless `RUN_LIVE_NETWORK_TESTS=1`
(tests/conftest.py) -- see `TestLiveVerifyRealPolicy` at the bottom.
"""

from __future__ import annotations

import pytest

from project_health.governance.policy import DEFAULT_POLICY_PATH
from project_health.governance.verify_sources import (
    SourcedItem,
    fetch_source_text,
    iter_sourced_items,
    normalize_quote_text,
    quote_fragments,
    quote_found,
    verify_policy_sources,
)


class TestNormalizeQuoteText:
    def test_curly_to_straight_quotes(self):
        assert normalize_quote_text("“Commit Then Review’s”") == '"Commit Then Review\'s"'

    def test_collapses_whitespace(self):
        assert normalize_quote_text("a   b\n\nc") == "a b c"

    def test_drops_space_before_punctuation(self):
        assert normalize_quote_text("hello , world .") == "hello, world."

    def test_drops_space_adjacent_to_quote_marks(self):
        # Applied consistently to both the live page and the policy's own
        # source_quote, so it doesn't matter that this fuses "a" and the
        # opening quote together -- see verify_sources.normalize_quote_text.
        result = normalize_quote_text('a " Commit Then Review " policy')
        assert result == 'a"Commit Then Review"policy'


class TestQuoteFragments:
    def test_no_ellipsis_single_wrapped_quote_strips_outer_quotes(self):
        assert quote_fragments('"Code must not be committed."') == ["Code must not be committed."]

    def test_ellipsis_splits_into_fragments(self):
        fragments = quote_fragments('"first part... second part"')
        assert fragments == ["first part", "second part"]

    def test_inner_quotes_not_stripped_when_not_wrapping_whole_fragment(self):
        fragments = quote_fragments('"a phrase says “Commit Then Review” here"')
        assert fragments == ['a phrase says “Commit Then Review” here']

    def test_two_independent_quoted_sentences_become_two_fragments(self):
        fragments = quote_fragments('"First sentence." "Second sentence."')
        assert fragments == ["First sentence.", "Second sentence."]


class TestQuoteFound:
    def test_found_exact(self):
        assert quote_found("The quick brown fox.", '"The quick brown fox."')

    def test_found_with_trailing_period_relaxation(self):
        # Page text has no trailing period at the list-item boundary.
        page = "must be reviewed by someone else Next item"
        assert quote_found(page, '"must be reviewed by someone else."')

    def test_not_found(self):
        assert not quote_found("completely unrelated text", '"a quote that is not there"')

    def test_multi_fragment_all_must_appear(self):
        page = "Alpha bravo charlie. Delta echo foxtrot."
        assert quote_found(page, '"Alpha bravo charlie... Delta echo foxtrot"')

    def test_multi_fragment_one_missing_fails(self):
        page = "Alpha bravo charlie. Something else entirely."
        assert not quote_found(page, '"Alpha bravo charlie... Delta echo foxtrot"')


class TestIterSourcedItems:
    def test_walks_real_policy_without_error(self):
        import yaml

        raw = yaml.safe_load(DEFAULT_POLICY_PATH.read_text())
        items = iter_sourced_items(raw)
        assert len(items) >= 8
        assert all(isinstance(item, SourcedItem) for item in items)
        paths = {item.path for item in items}
        assert "reviewer-present" in paths
        assert "ci-artefacts-attached" in paths

    def test_skips_same_as_exemptions(self):
        raw = {
            "rules": [
                {
                    "id": "rule-a",
                    "scored": True,
                    "exemptions": [
                        {"id": "ex-a", "same_as": "rule-b.exemptions.ex-b"},
                    ],
                },
            ]
        }
        assert iter_sourced_items(raw) == []

    def test_skips_unscored_rules(self):
        raw = {
            "rules": [
                {
                    "id": "display-only",
                    "scored": False,
                    "source_type": "official_docs",
                    "source_url": "https://example.invalid/x",
                    "source_quote": "quote",
                },
            ]
        }
        assert iter_sourced_items(raw) == []

    def test_sub_patterns_produce_one_item_each(self):
        raw = {
            "rules": [
                {
                    "id": "rule-a",
                    "scored": True,
                    "exemptions": [
                        {
                            "id": "ex-a",
                            "sub_patterns": [
                                {
                                    "id": "sp-1",
                                    "source_type": "official_docs",
                                    "source_url": "https://example.invalid/1",
                                    "source_quote": "quote one",
                                },
                                {
                                    "id": "sp-2",
                                    "source_type": "official_docs",
                                    "source_url": "https://example.invalid/2",
                                    "source_quote": "quote two",
                                },
                            ],
                        }
                    ],
                }
            ]
        }
        items = iter_sourced_items(raw)
        assert {item.path for item in items} == {
            "rule-a.exemptions.ex-a.sub_patterns.sp-1",
            "rule-a.exemptions.ex-a.sub_patterns.sp-2",
        }


class TestFetchSourceText:
    def test_github_blob_url_rewritten_to_raw(self, monkeypatch):
        captured = {}

        class FakeResponse:
            text = "raw file content"

            def raise_for_status(self):
                return None

        class FakeClient:
            def get(self, url, timeout=None, follow_redirects=None):
                captured["url"] = url
                return FakeResponse()

        text = fetch_source_text(
            "https://github.com/apache/cassandra-builds/blob/trunk/x.sh", FakeClient()
        )
        assert text == "raw file content"
        assert captured["url"] == (
            "https://raw.githubusercontent.com/apache/cassandra-builds/trunk/x.sh"
        )

    def test_plain_html_page_is_tag_stripped(self):
        class FakeResponse:
            text = "<html><body><p>Hello &amp; welcome</p></body></html>"

            def raise_for_status(self):
                return None

        class FakeClient:
            def get(self, url, timeout=None, follow_redirects=None):
                return FakeResponse()

        text = fetch_source_text("https://cassandra.apache.org/_/development/x.html", FakeClient())
        assert "Hello & welcome" in text
        assert "<p>" not in text


class TestLiveVerifyRealPolicy:
    """The actual D24 live check, run for real: re-fetch every source_url
    the real repo-root governance-policy.yaml cites and confirm every quote
    still appears. Skipped unless RUN_LIVE_NETWORK_TESTS=1."""

    @pytest.mark.live_network
    def test_all_real_policy_sources_verify(self):
        results = verify_policy_sources(DEFAULT_POLICY_PATH)
        assert results, "expected at least one sourced item in the real policy"
        failed = [r for r in results if not r.ok]
        assert not failed, "\n".join(
            f"{r.item.path} <{r.item.source_url}>: {r.detail}" for r in failed
        )
