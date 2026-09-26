"""Tests for project_health.classify.preprocess (issue #43).

Every example text in this file is invented for the test, per COMMUNITY-HEALTH.md
§1.1's "quotations: default none" rule and this issue's synthetic-text requirement --
none of it is a real dev@ message or JIRA comment. No network access anywhere in this
module (pure string functions).
"""

from __future__ import annotations

from project_health.classify.preprocess import (
    CODE_PLACEHOLDER,
    STACKTRACE_PLACEHOLDER,
    collapse_log_dumps,
    is_automated_sender,
    normalize_whitespace,
    preprocess_text,
    strip_code_blocks,
    strip_jira_quoted_text,
    strip_jira_wiki_markup,
    strip_quoted_text,
    strip_signature,
    strip_stack_traces,
)


class TestStripQuotedText:
    def test_strips_gt_prefixed_lines(self):
        text = "New content here.\n> old quoted line one\n> old quoted line two\nMore new content."
        result = strip_quoted_text(text)
        assert "> old quoted" not in result
        assert "New content here." in result
        assert "More new content." in result

    def test_strips_nested_gt_quoting(self):
        text = "reply\n>> deeply quoted\n> quoted\nend"
        result = strip_quoted_text(text)
        assert ">>" not in result
        assert ">" not in result

    def test_strips_on_wrote_block_and_everything_after(self):
        text = (
            "I agree with this.\n\n"
            "On Mon, Jan 5, 2026 at 3:00 PM, Jamie Example <jamie@example.com> wrote:\n"
            "> the original point\n"
            "> more of the original point\n"
        )
        result = strip_quoted_text(text)
        assert "I agree with this." in result
        assert "wrote:" not in result
        assert "the original point" not in result

    def test_strips_outlook_style_header_block_and_everything_after(self):
        text = (
            "Sure, that works for me.\n\n"
            "From: Jamie Example <jamie@example.com>\n"
            "Sent: Monday, January 5, 2026 3:00 PM\n"
            "To: dev@example.org\n"
            "Subject: RE: Example thread\n\n"
            "This is the original message text that Outlook pasted inline.\n"
        )
        result = strip_quoted_text(text)
        assert "Sure, that works for me." in result
        assert "original message text" not in result
        assert "Sent:" not in result

    def test_leaves_plain_text_with_no_quoting_untouched(self):
        text = "Just a plain message with no quoting at all."
        assert strip_quoted_text(text) == text

    def test_strips_wrapped_attribution_line_spanning_two_lines(self):
        # Regression (fixup cycle 1, coordinator review): mail clients
        # routinely wrap "On ... wrote:" across lines when the name/address
        # is long -- the attribution (including another person's name and
        # email) must still be dropped, not left in.
        text = (
            "+1, ship it.\n\n"
            "On Tue, Mar 4, 2025 at 9:12 AM Bob Rev <bob@example.org>\n"
            "wrote:\n"
            "> I think this is ready."
        )
        result = strip_quoted_text(text)
        assert result.strip() == "+1, ship it."
        assert "Bob Rev" not in result
        assert "bob@example.org" not in result
        assert "wrote:" not in result
        assert "I think this is ready" not in result

    def test_strips_german_am_schrieb_attribution(self):
        text = (
            "Klingt gut.\n\n"
            "Am 4. März 2025 um 09:12 schrieb Bob Rev <bob@example.org>:\n"
            "> Ist das schon bereit?"
        )
        result = strip_quoted_text(text)
        assert result.strip() == "Klingt gut."
        assert "schrieb" not in result
        assert "Bob Rev" not in result

    def test_strips_french_le_a_ecrit_attribution(self):
        text = (
            "Ça me va.\n\n"
            "Le 4 mars 2025 à 09:12, Bob Rev <bob@example.org> a écrit :\n"
            "> Est-ce pret ?"
        )
        result = strip_quoted_text(text)
        assert result.strip() == "Ça me va."
        assert "a écrit" not in result
        assert "Bob Rev" not in result

    def test_does_not_strip_a_plain_sentence_starting_with_on(self):
        text = "On balance I think this is the right call, no attribution here."
        assert strip_quoted_text(text) == text


class TestStripSignature:
    def test_strips_rfc3676_signature_delimiter_and_everything_after(self):
        text = "Thanks for looking at this.\n-- \nJamie Example\nExample Corp\n"
        result = strip_signature(text)
        assert "Thanks for looking at this." in result
        assert "Example Corp" not in result
        assert "Jamie Example" not in result

    def test_strips_bare_double_dash_delimiter(self):
        text = "See you there.\n--\nJamie\n"
        result = strip_signature(text)
        assert "Jamie" not in result

    def test_strips_mobile_footer_line(self):
        text = "quick reply\nSent from my iPhone\nmore text after"
        result = strip_signature(text)
        assert "Sent from my iPhone" not in result

    def test_does_not_strip_a_double_dash_inside_a_sentence(self):
        text = "The flag is --verbose -- it prints extra output."
        assert strip_signature(text) == text


class TestStripCodeBlocks:
    def test_replaces_fenced_code_block(self):
        text = "Here is the fix:\n```\ndef f():\n    return 1\n```\nThat should do it."
        result = strip_code_blocks(text)
        assert CODE_PLACEHOLDER in result
        assert "def f()" not in result
        assert "Here is the fix:" in result
        assert "That should do it." in result

    def test_replaces_jira_code_block_with_language(self):
        text = "Repro:\n{code:java}\nThrow new RuntimeException();\n{code}\ndone"
        result = strip_code_blocks(text)
        assert CODE_PLACEHOLDER in result
        assert "RuntimeException" not in result

    def test_replaces_jira_noformat_block(self):
        text = "output:\n{noformat}\nraw   unformatted    text\n{noformat}\nend"
        result = strip_code_blocks(text)
        assert CODE_PLACEHOLDER in result
        assert "unformatted" not in result

    def test_leaves_prose_with_no_code_untouched(self):
        text = "Just discussing the design, no code here."
        assert strip_code_blocks(text) == text


class TestStripStackTraces:
    def test_replaces_python_traceback(self):
        text = (
            "Got this failure:\n"
            "Traceback (most recent call last):\n"
            '  File "example.py", line 12, in run\n'
            "    do_thing()\n"
            '  File "example.py", line 4, in do_thing\n'
            "ValueError: synthetic example failure\n"
            "Any ideas?"
        )
        result = strip_stack_traces(text)
        assert STACKTRACE_PLACEHOLDER in result
        assert "do_thing" not in result
        assert "Got this failure:" in result
        assert "Any ideas?" in result

    def test_replaces_java_stack_trace_with_caused_by(self):
        text = (
            "Exception in thread \"main\" java.lang.RuntimeException: boom\n"
            "\tat com.example.Thing.run(Thing.java:10)\n"
            "\tat com.example.Main.main(Main.java:5)\n"
            "Caused by: java.lang.NullPointerException\n"
            "\tat com.example.Thing.helper(Thing.java:20)\n"
            "\t... 2 more\n"
            "end of message"
        )
        result = strip_stack_traces(text)
        assert STACKTRACE_PLACEHOLDER in result
        assert "com.example" not in result
        assert "end of message" in result

    def test_leaves_prose_mentioning_exceptions_without_a_trace_untouched(self):
        text = "This throws a RuntimeException in some cases, we should fix it."
        assert strip_stack_traces(text) == text

    def test_replaces_short_headerless_trace(self):
        # Regression (fixup cycle 1, coordinator review): a bare exception
        # class line followed by frame lines, with none of the usual
        # "Traceback"/"Exception in thread"/"Caused by:" markers, was
        # previously left untouched.
        text = (
            "java.lang.NullPointerException\n"
            "\tat org.apache.cassandra.db.Foo.bar(Foo.java:12)\n"
            "\tat org.apache.cassandra.db.Baz.qux(Baz.java:34)"
        )
        result = strip_stack_traces(text)
        assert result == STACKTRACE_PLACEHOLDER
        assert "org.apache.cassandra" not in result

    def test_replaces_two_consecutive_frame_lines_with_no_header_at_all(self):
        text = (
            "context before\n"
            "\tat org.apache.cassandra.db.Foo.bar(Foo.java:12)\n"
            "\tat org.apache.cassandra.db.Baz.qux(Baz.java:34)\n"
            "context after"
        )
        result = strip_stack_traces(text)
        assert STACKTRACE_PLACEHOLDER in result
        assert "context before" in result
        assert "context after" in result
        assert "org.apache.cassandra" not in result

    def test_does_not_replace_a_single_isolated_frame_line(self):
        # A lone frame-shaped line with no exception header before it and no
        # second consecutive frame line after it is left alone -- collapsing
        # requires either a header+frame or 2+ consecutive frames.
        text = (
            "Some prose here.\n"
            "\tat org.apache.cassandra.db.Foo.bar(Foo.java:12)\n"
            "More ordinary prose that continues."
        )
        assert strip_stack_traces(text) == text


class TestCollapseLogDumps:
    def test_collapses_long_run_of_timestamped_lines(self):
        lines = [f"2026-01-01 10:00:{i:02d},000 INFO example log line {i}" for i in range(8)]
        text = "before\n" + "\n".join(lines) + "\nafter"
        result = collapse_log_dumps(text)
        assert CODE_PLACEHOLDER in result
        assert "example log line" not in result
        assert "before" in result
        assert "after" in result

    def test_leaves_a_couple_of_log_lines_untouched(self):
        text = "context\n2026-01-01 10:00:00,000 INFO one line of log for context\nmore context"
        result = collapse_log_dumps(text, min_lines=5)
        assert CODE_PLACEHOLDER not in result
        assert "one line of log" in result


class TestStripJiraQuotedText:
    def test_strips_quote_block_leaving_the_quoters_own_words(self):
        # Regression (fixup cycle 1, coordinator review): a {quote} block is
        # someone else's text; a quoter objecting to a hostile comment by
        # quoting it back must not inherit that comment's labels.
        text = "{quote}Why not just retry?{quote}\nBecause retries amplify load."
        result = strip_jira_quoted_text(text)
        assert "Why not just retry?" not in result
        assert "Because retries amplify load." in result

    def test_strips_bq_line_entirely(self):
        text = "bq. This was already discussed and rejected.\nI still disagree with that."
        result = strip_jira_quoted_text(text)
        assert "already discussed and rejected" not in result
        assert "I still disagree with that." in result

    def test_leaves_text_with_no_quoting_untouched(self):
        text = "Just a plain JIRA comment with no quoting."
        assert strip_jira_quoted_text(text) == text


class TestStripJiraWikiMarkup:
    def test_strips_heading_markup(self):
        assert strip_jira_wiki_markup("h2. Repro steps\ntext") == "Repro steps\ntext"

    def test_strips_bold_and_italic_markup(self):
        result = strip_jira_wiki_markup("This is *important* and _also relevant_.")
        assert result == "This is important and also relevant."

    def test_strips_monospace_markup(self):
        assert strip_jira_wiki_markup("See {{example.log}} for details.") == (
            "See example.log for details."
        )

    def test_strips_link_markup_keeping_link_text(self):
        assert strip_jira_wiki_markup("See [the docs|https://example.com/docs].") == (
            "See the docs."
        )

    def test_strips_bullet_markup(self):
        result = strip_jira_wiki_markup("* first item\n* second item")
        assert result == "first item\nsecond item"


class TestNormalizeWhitespace:
    def test_collapses_long_blank_line_runs(self):
        text = "first\n\n\n\n\nsecond"
        assert normalize_whitespace(text) == "first\n\nsecond"

    def test_strips_trailing_whitespace_per_line(self):
        text = "first   \nsecond\t\n"
        assert normalize_whitespace(text) == "first\nsecond"

    def test_trims_leading_and_trailing_whitespace(self):
        assert normalize_whitespace("\n\n  hello  \n\n") == "hello"


class TestPreprocessTextPipeline:
    def test_full_pipeline_on_a_quoted_signed_message_with_code(self):
        text = (
            "I don't think this benchmark holds up under load.\n\n"
            "```\nrun_benchmark()\n```\n\n"
            "On Mon, Jan 5, 2026 at 3:00 PM, Jamie Example <jamie@example.com> wrote:\n"
            "> the original benchmark claim\n"
            "-- \nCasey Example\n"
        )
        result = preprocess_text(text, "mailing_list")
        assert "I don't think this benchmark holds up under load." in result
        assert CODE_PLACEHOLDER in result
        assert "wrote:" not in result
        assert "original benchmark claim" not in result
        assert "Casey Example" not in result

    def test_jira_source_strips_wiki_markup(self):
        text = "h2. Repro\n\n*Steps:*\n{code}\nrun();\n{code}"
        result = preprocess_text(text, "jira_comment")
        assert "h2." not in result
        assert "*Steps*" not in result
        assert "Steps:" in result
        assert CODE_PLACEHOLDER in result

    def test_mailing_list_source_does_not_strip_wiki_markup(self):
        text = "I *really* like this idea."
        result = preprocess_text(text, "mailing_list")
        assert "*really*" in result

    def test_jira_source_strips_quote_blocks(self):
        text = "{quote}Why not just retry?{quote}\nBecause retries amplify load."
        result = preprocess_text(text, "jira_comment")
        assert "Why not just retry?" not in result
        assert "Because retries amplify load." in result

    def test_mailing_list_source_does_not_strip_jira_quote_syntax(self):
        # {quote} syntax is JIRA-only; a mailing-list message that happens to
        # contain literal curly braces should not be treated as a quote.
        text = "I set {quote}=true and it broke.\nHere's why."
        result = preprocess_text(text, "mailing_list")
        assert "{quote}=true" in result

    def test_rejects_unknown_source(self):
        import pytest

        with pytest.raises(ValueError):
            preprocess_text("hello", "slack")

    def test_handles_none_text(self):
        assert preprocess_text(None, "mailing_list") == ""


class TestIsAutomatedSender:
    def test_matches_bracket_bot_pattern(self):
        assert is_automated_sender("Example Bot <bot@example.com>", [r"(?i)\[bot\]"]) is False
        assert is_automated_sender("dependabot[bot]", [r"(?i)\[bot\]"]) is True

    def test_matches_dict_pattern_shape(self):
        patterns = [{"regex": r"(?i)^svn-role$"}]
        assert is_automated_sender("svn-role", patterns) is True
        assert is_automated_sender("areallcommitter", patterns) is False

    def test_matches_object_with_regex_attribute(self):
        class _P:
            regex = r"(?i)noreply@example\.com$"

        assert is_automated_sender("noreply@example.com", [_P()]) is True

    def test_none_sender_is_never_automated(self):
        assert is_automated_sender(None, [r".*"]) is False

    def test_no_patterns_never_matches(self):
        assert is_automated_sender("anyone@example.com", []) is False
