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
