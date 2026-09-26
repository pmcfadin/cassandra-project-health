"""Unit tests for project_health.collectors.reviewer_trailer (issue #4).

`ReviewerExtractor.extract` is a pure function (no I/O), so every test here
feeds it a literal commit-message string. The first block of tests covers
every distinct trailer variant that appears in the #3 fixture repo
(tests/fixtures/git/build_repo.py); the second block covers 5 real-world
examples copied verbatim from apache/cassandra's trunk history (shas noted
per test), to guard against the extractor being over-fit to the synthetic
fixture.
"""

from project_health.collectors.reviewer_trailer import (
    ReviewerExtractor,
    looks_like_reviewer_trailer,
)


class TestFixtureTrailerVariants:
    """One test per distinct trailer shape used in the #3 fixture repo."""

    def setup_method(self):
        self.extractor = ReviewerExtractor()

    def test_semicolon_separator_single_reviewer_single_issue(self):
        message = (
            "Initial commit\n\n"
            "Patch by Alice Author; reviewed by Bob Reviewer for CASSANDRA-100"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.patch_by == "Alice Author"
        assert attribution.reviewers == ("Bob Reviewer",)
        assert attribution.issue_keys == ("CASSANDRA-100",)

    def test_semicolon_separator_multiple_reviewers_joined_by_and(self):
        message = (
            "Add metrics collection\n\n"
            "Patch by Bob Reviewer; reviewed by Alice Author and Charlie "
            "Contributor for CASSANDRA-101"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Alice Author", "Charlie Contributor")
        assert attribution.issue_keys == ("CASSANDRA-101",)

    def test_comma_separator_lowercase_patch(self):
        message = (
            "Fix collector bug\n\n"
            "patch by Charlie Contributor, reviewed by Alice Author for CASSANDRA-102"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.patch_by == "Charlie Contributor"
        assert attribution.reviewers == ("Alice Author",)
        assert attribution.issue_keys == ("CASSANDRA-102",)

    def test_multiple_reviewers_comma_and_multiple_issue_keys_comma(self):
        message = (
            "Refactor normalization\n\n"
            "patch by Alice Author; reviewed by Bob Reviewer, Charlie "
            "Contributor for CASSANDRA-103, CASSANDRA-104"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Bob Reviewer", "Charlie Contributor")
        assert attribution.issue_keys == ("CASSANDRA-103", "CASSANDRA-104")

    def test_trailer_line_ignores_following_co_authored_by_line(self):
        message = (
            "Implement caching layer\n\n"
            "Patch by Alice Author; reviewed by Dana Dev for CASSANDRA-112\n"
            "Co-authored-by: Dana Dev <dana@example.org>"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Dana Dev",)
        assert attribution.issue_keys == ("CASSANDRA-112",)

    def test_no_trailer_at_all_returns_none_and_is_not_flagged(self):
        message = "Performance optimization\n\nNo trailer on this commit"
        assert self.extractor.extract(message) is None
        assert looks_like_reviewer_trailer(message) is False

    def test_no_ticket_reference_returns_none_and_is_not_flagged(self):
        message = "Schema improvements\n\nNo ticket reference in this commit either"
        assert self.extractor.extract(message) is None
        assert looks_like_reviewer_trailer(message) is False

    def test_bot_commit_message_returns_none(self):
        message = "Automated update\n\nGenerated commit by CI bot"
        assert self.extractor.extract(message) is None
        assert looks_like_reviewer_trailer(message) is False


class TestRealWorldTrailerVariants:
    """5 real-world examples copied verbatim from apache/cassandra trunk history.

    Collected via a scratch `--filter=blob:none --no-checkout` clone of
    apache/cassandra (issue #4's real-data acceptance check), not a network
    call made by this test — the literal message strings are pinned here so
    these tests never touch the network.
    """

    def setup_method(self):
        self.extractor = ReviewerExtractor()

    def test_single_reviewer_three_issue_keys(self):
        # sha 2aa2b59d37899fbe114bdd75e09c10699729c32a
        message = (
            "patch by Lorina Poland; reviewed by Michael Semb Wever for "
            "CASSANDRA-19249, CASSANDRA-18990, CASSANDRA-15719"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Michael Semb Wever",)
        assert attribution.issue_keys == ("CASSANDRA-19249", "CASSANDRA-18990", "CASSANDRA-15719")

    def test_two_reviewers_comma_four_issue_keys(self):
        # sha f0655159e692816a2703e2a0ff6c9458f90cab75
        message = (
            " patch by Mick Semb Wever; reviewed by Berenguer Blasi, Brandon "
            "Williams for CASSANDRA-17989, CASSANDRA-18008, CASSANDRA-17145, "
            "CASSANDRA-18003"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Berenguer Blasi", "Brandon Williams")
        assert attribution.issue_keys == (
            "CASSANDRA-17989",
            "CASSANDRA-18008",
            "CASSANDRA-17145",
            "CASSANDRA-18003",
        )

    def test_no_punctuation_separator_between_patch_by_and_reviewed_by(self):
        # sha c53d3ac8c6a743b7e730d2ac358516842b024133 — no ";" or "," at all
        # between the "patch by" and "reviewed by" clauses.
        message = "Patch by Lukasz Antoniak reviewed by Jacek Lewandowski for CASSANDRA-19880"
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.patch_by == "Lukasz Antoniak"
        assert attribution.reviewers == ("Jacek Lewandowski",)
        assert attribution.issue_keys == ("CASSANDRA-19880",)

    def test_multiple_reviewers_joined_by_and_single_word_names(self):
        # sha f771e964e32ffb9d1b42eaa7dcf14192fc0dbd55
        message = (
            "patch by nvharikrishna; reviewed by Stefan Miklosovic and marcuse "
            "for CASSANDRA-19195"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Stefan Miklosovic", "marcuse")
        assert attribution.issue_keys == ("CASSANDRA-19195",)

    def test_unicode_name_and_no_space_comma_issue_keys(self):
        # sha e2d2bd61f479fa7128f97a1b5b1623632855ffd0
        message = (
            "patch by Mick Semb Wever; reviewed by Štefan Miklošovič "
            "for CASSANDRA-18936,CASSANDRA-18665"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Štefan Miklošovič",)
        assert attribution.issue_keys == ("CASSANDRA-18936", "CASSANDRA-18665")


class TestWrappedTrailerParagraph:
    """Issue #77: a "patch by"/"reviewed by" trailer paragraph that's
    line-wrapped (real trunk history wraps at ~72 columns) must be unwrapped
    before matching, not truncated at the first newline. Each of the first
    four cases is a real message copied verbatim from apache/cassandra
    trunk, the exact regression examples from the issue.
    """

    def setup_method(self):
        self.extractor = ReviewerExtractor()

    def test_wrapped_reviewed_by_clause_sha_38f7789534(self):
        # sha 38f7789534: "reviewed by ... and Sam\nTunnicliffe" wraps mid
        # reviewer name -- must not truncate to reviewer "Sam".
        message = (
            "Fix flaky InProgressSequenceCoordinationTest by increasing "
            "request_timeout and ensuring background threads are joined "
            "before test cleanup\n\n"
            "Patch by Sam Lightfoot; reviewed by Dmitry Konstantinov and Sam\n"
            "Tunnicliffe for CASSANDRA-21189"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.patch_by == "Sam Lightfoot"
        assert attribution.reviewers == ("Dmitry Konstantinov", "Sam Tunnicliffe")
        assert attribution.issue_keys == ("CASSANDRA-21189",)

    def test_wrapped_reviewed_by_clause_sha_b1f30e94f5_with_co_authored_by(self):
        # sha b1f30e94f5: wraps mid reviewer list ("... and\nSam
        # Tunnicliffe"), followed by a blank line then a Co-authored-by
        # trailer that must survive untouched, not be swallowed into the
        # unwrapped paragraph.
        message = (
            "Move long running TCM operations to a longer timout\n\n"
            "Replaces the fixed-retry commit loop with deadline-based "
            "exponential\n"
            "backoff for long running CMS commit operations "
            "(cms_commit_timeout=1h, 5s-60s jitter)\n"
            "to allow heavily contended CMS nodes time to commit "
            "transforms.\n\n"
            "Patch by Jon Meredith and Sam Tunnicliffe; reviewed by Jon "
            "Meredith and\n"
            "Sam Tunnicliffe for CASSANDRA-21453\n\n"
            "Co-authored-by: Sam Tunnicliffe <samt@apache.org>\n"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Jon Meredith", "Sam Tunnicliffe")
        assert attribution.issue_keys == ("CASSANDRA-21453",)

    def test_wrapped_reviewed_by_clause_sha_f05b27502f_with_two_co_authored_by(self):
        # sha f05b27502f: wraps mid single reviewer name ("... reviewed by
        # Sam\nTunnicliffe and Marcus Eriksson"), followed by two
        # Co-authored-by lines that must both survive untouched.
        message = (
            "Improve CMS initialization\n\n"
            "* Better handling of DOWN unupgraded nodes\n\n"
            "Patch by Sam Tunnicliffe and Marcus Eriksson; reviewed by Sam\n"
            "Tunnicliffe and Marcus Eriksson for CASSANDRA-21036\n\n"
            "Co-authored-by: Marcus Eriksson <marcuse@apache.org>\n"
            "Co-authored-by: Sam Tunnicliffe <samt@apache.org>\n"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Sam Tunnicliffe", "Marcus Eriksson")
        assert attribution.issue_keys == ("CASSANDRA-21036",)

    def test_wrapped_reviewed_by_clause_sha_1df3a8cef0(self):
        # sha 1df3a8cef0
        message = (
            "Setup async transformation before making internode request\n\n"
            "Patch by Sam Tunnicliffe and Dmitry Konstantinov; reviewed by "
            "Sam\n"
            "Tunnicliffe and Dmitry Konstantinov for CASSANDRA-21384\n\n"
            "Co-authored-by: Dmitry Konstantinov <netudima@gmail.com>"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Sam Tunnicliffe", "Dmitry Konstantinov")
        assert attribution.issue_keys == ("CASSANDRA-21384",)

    def test_unwrapping_does_not_swallow_unrelated_paragraph_after_blank_line(self):
        # A wrapped trailer followed by a blank line and then an unrelated
        # commit-body paragraph (not a trailer at all) must leave that
        # paragraph completely untouched.
        message = (
            "Patch by Alice Author; reviewed by Bob and\n"
            "Carol Contributor for CASSANDRA-500\n\n"
            "This paragraph is unrelated prose that happens to follow the\n"
            "trailer and must not be merged into it."
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Bob", "Carol Contributor")
        assert attribution.issue_keys == ("CASSANDRA-500",)

    def test_unwrapping_stops_at_a_new_trailer_with_no_blank_line_between(self):
        # No blank line at all between the wrapped trailer's last
        # continuation line and a following Co-authored-by line -- the "for
        # <ISSUE-KEY>" terminator must still stop the join before it.
        message = (
            "Patch by Alice Author; reviewed by Bob and\n"
            "Carol Contributor for CASSANDRA-501\n"
            "Co-authored-by: Carol Contributor <carol@example.org>"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Bob", "Carol Contributor")
        assert attribution.issue_keys == ("CASSANDRA-501",)

    def test_non_wrapped_single_line_trailer_is_unaffected(self):
        # A conventional, non-wrapped trailer must parse exactly as before
        # (the unwrap pass is a no-op when there's nothing to unwrap).
        message = "Patch by Alice Author; reviewed by Bob Reviewer for CASSANDRA-502"
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Bob Reviewer",)
        assert attribution.issue_keys == ("CASSANDRA-502",)


class TestMissingForGluesIssueKeyOntoName:
    """Real trunk trailers that drop (or typo) "for" before the issue key.

    Without special handling, the issue key becomes part of the "reviewer
    name" text (e.g. "Brandon Williams CASSANDRA-18555"), inflating unique-
    reviewer counts with garbage. Each case below is a real message, pinned
    by sha, found via the issue #4 real-data acceptance check.
    """

    def setup_method(self):
        self.extractor = ReviewerExtractor()

    def test_bare_space_before_issue_key(self):
        # sha e2a6c99310aa93ba3506ca8f603ae1039372f533
        message = (
            "Expose bootstrap and decommission state to nodetool info\n\n"
            "patch by Stefan Miklosovic; reviewed by Brandon Williams "
            "CASSANDRA-18555\n\n"
            "Co-authored-by: Jaydeepkumar Chovatia <chovatia.jaydeep@gmail.com>"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Brandon Williams",)
        assert attribution.issue_keys == ("CASSANDRA-18555",)

    def test_bare_space_before_issue_key_with_trailing_period(self):
        # sha f36a518208fae1ca3af914f4a74ef4987238c14a
        message = (
            "Fix test Failure: MixedModeFrom3LoggedBatchTest.testSimpleStrategy\n\n"
            "Patch by Alex Petrov; reviewed by Sam Tunnicliffe CASSANDRA-19066."
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Sam Tunnicliffe",)
        assert attribution.issue_keys == ("CASSANDRA-19066",)

    def test_connector_word_from(self):
        # sha 9f4368cbb74d7163b6396eec3722b8c1d7fb55dc
        message = (
            "Set io.netty.transport.noNative to false for in-jvm dtests\n\n"
            "patch by Stefan Miklosovic; reviewed by Brandon Williams from "
            "CASSANDRA-18830"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Brandon Williams",)
        assert attribution.issue_keys == ("CASSANDRA-18830",)

    def test_issue_key_wrapped_in_parens(self):
        # sha 9be8369ae65be8eb4848eb9ef58e2909a8d89016
        message = (
            "Remove commons-codec dependency\n"
            "patch by Ekaterina Dimitrova; reviewed by Brandon Williams "
            "(CASSANDRA-18772)"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Brandon Williams",)
        assert attribution.issue_keys == ("CASSANDRA-18772",)

    def test_connector_word_or_typo_for_for_with_multiple_reviewers(self):
        # sha 31aa17a2a3b18bdda723123cad811f075287807d — "or" is a typo for
        # "for", and there are two reviewers joined by "and" before it.
        message = (
            "List snapshots of dropped tables\n\n"
            "Patch by Paulo Motta; Reviewed by Stefan Miklosovic and Brandon "
            "Williams or CASSANDRA-16843"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Stefan Miklosovic", "Brandon Williams")
        assert attribution.issue_keys == ("CASSANDRA-16843",)

    def test_single_word_handle_name_with_bare_issue_key(self):
        # sha b9b2a4e1a07af518cebd4441469c940d5ac0c2ea
        message = (
            "Cassandra not starting when using enhanced startup scripts in "
            "windows\n\n"
            "patch by Shyam Phirke; reviewed by jasobrown CASSANDRA-14418"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("jasobrown",)
        assert attribution.issue_keys == ("CASSANDRA-14418",)


class TestLooksLikeReviewerTrailer:
    def test_true_when_phrase_present_any_case(self):
        assert looks_like_reviewer_trailer("Reviewed By: someone") is True
        assert looks_like_reviewer_trailer("this was reviewed by nobody in particular") is True

    def test_false_when_phrase_absent(self):
        assert looks_like_reviewer_trailer("just a plain commit message") is False


class TestUnparsedButContainsReviewedByIsDetectable:
    """A message with an unconventional "reviewed by" phrasing that the
    regex can't parse must return None from `extract` while still being
    flagged by `looks_like_reviewer_trailer`, so a caller can count/log it
    instead of silently dropping it (issue #4 acceptance criteria).
    """

    def test_author_colon_style_trailer_is_unparsed_but_flagged(self):
        # A real, rarer apache/cassandra trailer convention: "Author: X;
        # Reviewed by Y - no ticket" (no "patch by" at all).
        message = (
            "add index naming note to clarify\n\n"
            "Author: Lorina Poland (polandll); Reviewed by Mick Semb Wever "
            "(mck) - no ticket"
        )
        extractor = ReviewerExtractor()
        assert extractor.extract(message) is None
        assert looks_like_reviewer_trailer(message) is True


class TestGovernanceParserExtension:
    """Issue #36: the two real trunk regressions that made the governance
    engine add a `review_wording_check` guard (governance-policy.yaml
    `reviewer-present.review_wording_check.regression_examples`,
    docs/spec/GOVERNANCE.md §8) must now parse as real `pass` results
    instead of falling through to "review text present but unparsed".
    """

    def setup_method(self):
        self.extractor = ReviewerExtractor()

    def test_reviewed_name_without_by(self):
        # sha 208d87513f658f6fbf82cabcbb04142e7319fa55
        message = "patch by Mick Semb Wever; reviewed Štefan Miklošovič for CASSANDRA-21489"
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.patch_by == "Mick Semb Wever"
        assert attribution.reviewers == ("Štefan Miklošovič",)
        assert attribution.issue_keys == ("CASSANDRA-21489",)

    def test_authored_by_then_reviewed_by(self):
        # sha 05186d786974f3caf0491d5373b648c97c718c4a
        message = (
            "Authored by Lorina Poland (polandll); Reviewed by Branimir Lambov "
            "(blambov) for CASSANDRA-18236"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.patch_by == "Lorina Poland (polandll)"
        assert attribution.reviewers == ("Branimir Lambov (blambov)",)
        assert attribution.issue_keys == ("CASSANDRA-18236",)

    def test_does_not_match_lowercase_filler_word_as_a_name(self):
        # Guards against "reviewed a fix for X" being read as reviewer "a fix".
        message = "Reviewed a fix for CASSANDRA-100"
        assert self.extractor.extract(message) is None

    def test_extended_form_requires_for_clause(self):
        # No "for <issue>" clause at all -- must not synthesize a match.
        message = "reviewed Alice Author"
        assert self.extractor.extract(message) is None


class TestExtractIssueKeysPublicHelper:
    def test_finds_keys_independent_of_trailer(self):
        from project_health.collectors.reviewer_trailer import extract_issue_keys

        message = "Some commit about CASSANDRA-100 and CASSANDRA-101, no trailer here"
        assert extract_issue_keys(message) == ("CASSANDRA-100", "CASSANDRA-101")

    def test_empty_when_no_keys(self):
        from project_health.collectors.reviewer_trailer import extract_issue_keys

        assert extract_issue_keys("no ticket reference here") == ()


class TestPlaceholderReviewerFiltering:
    """Real apache/cassandra trailers with placeholder reviewer names (issue #18).

    Placeholder names (TBD, TBA, none, n/a, etc.) should be excluded from
    reviewers and tracked in placeholder_reviewers, to avoid polluting
    reviewer metrics with data-quality signals.
    """

    def setup_method(self):
        self.extractor = ReviewerExtractor()

    def test_tbd_only_returns_attribution_with_empty_reviewers(self):
        # Real message from apache/cassandra CASSANDRA-21430
        message = "patch by Francisco Guerrero; reviewed by TBD for CASSANDRA-21430"
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ()
        assert attribution.placeholder_reviewers == ("TBD",)
        assert attribution.issue_keys == ("CASSANDRA-21430",)

    def test_tbd_only_cassandra_21342_returns_attribution_with_empty_reviewers(self):
        # Real message from apache/cassandra CASSANDRA-21342
        message = "patch by Patrick McFadin; reviewed by TBD for CASSANDRA-21342"
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ()
        assert attribution.placeholder_reviewers == ("TBD",)

    def test_tbd_only_cassandra_20539_returns_attribution_with_empty_reviewers(self):
        # Real message from apache/cassandra CASSANDRA-20539
        message = (
            "Patch by Francisco Guerrerro, Doug Rohrer; reviewed by TBD "
            "for CASSANDRA-20539"
        )
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ()
        assert attribution.placeholder_reviewers == ("TBD",)

    def test_tbd_only_cassandra_20423_returns_attribution_with_empty_reviewers(self):
        # Real message from apache/cassandra CASSANDRA-20423
        message = "Patch by Dmitry Konstantinov; reviewed by TBD for CASSANDRA-20423"
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ()
        assert attribution.placeholder_reviewers == ("TBD",)

    def test_tbd_only_cassandra_11301_returns_attribution_with_empty_reviewers(self):
        # Real message from apache/cassandra CASSANDRA-11301
        message = "patch by Stefania Alborghetti; reviewed by TBD for CASSANDRA-11301"
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ()
        assert attribution.placeholder_reviewers == ("TBD",)

    def test_mixed_real_and_placeholder_reviewers_filters_placeholder(self):
        # Mixed case: one real reviewer and one placeholder
        message = "reviewed by Alice Author and TBD for CASSANDRA-1"
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Alice Author",)
        assert attribution.placeholder_reviewers == ("TBD",)
        assert attribution.issue_keys == ("CASSANDRA-1",)

    def test_placeholder_case_insensitive_tba(self):
        message = "patch by Bob; reviewed by Alice and tba for CASSANDRA-100"
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Alice",)
        assert attribution.placeholder_reviewers == ("tba",)

    def test_placeholder_case_insensitive_none(self):
        message = "patch by Bob; reviewed by Alice and NONE for CASSANDRA-100"
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Alice",)
        assert attribution.placeholder_reviewers == ("NONE",)

    def test_placeholder_nobody(self):
        message = "patch by Bob; reviewed by Alice and nobody for CASSANDRA-100"
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Alice",)
        assert attribution.placeholder_reviewers == ("nobody",)

    def test_placeholder_n_a(self):
        message = "patch by Bob; reviewed by Alice and n/a for CASSANDRA-100"
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Alice",)
        assert attribution.placeholder_reviewers == ("n/a",)

    def test_placeholder_na(self):
        message = "patch by Bob; reviewed by Alice and na for CASSANDRA-100"
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Alice",)
        assert attribution.placeholder_reviewers == ("na",)

    def test_placeholder_question_mark(self):
        message = "patch by Bob; reviewed by Alice and ? for CASSANDRA-100"
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Alice",)
        assert attribution.placeholder_reviewers == ("?",)

    def test_placeholder_unknown(self):
        message = "patch by Bob; reviewed by Alice and unknown for CASSANDRA-100"
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Alice",)
        assert attribution.placeholder_reviewers == ("unknown",)

    def test_multiple_placeholders_all_filtered(self):
        message = "patch by Bob; reviewed by TBD, TBA, none for CASSANDRA-100"
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ()
        assert attribution.placeholder_reviewers == ("TBD", "TBA", "none")

    def test_multiple_mixed_reviewers_and_placeholders(self):
        message = "patch by Bob; reviewed by Alice, TBD, Charlie, unknown for CASSANDRA-100"
        attribution = self.extractor.extract(message)
        assert attribution is not None
        assert attribution.reviewers == ("Alice", "Charlie")
        assert attribution.placeholder_reviewers == ("TBD", "unknown")
