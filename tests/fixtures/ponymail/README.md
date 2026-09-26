# Pony Mail fixtures

Real recorded responses from `lists.apache.org`'s Pony Mail API for testing purposes
(issue #33). All requests were anonymous, unauthenticated GETs.

## Files

- **stats_dev.json**: `stats.lua` response for `dev@cassandra.apache.org`.
  - URL: `https://lists.apache.org/api/stats.lua?list=dev&domain=cassandra.apache.org`
  - Recorded: 2026-09-25
  - `active_months` is trimmed to a handful of representative entries (the real response
    lists every month back to 2009-01, ~213 entries) — only `firstYear`/`firstMonth`/
    `lastYear`/`lastMonth` are read by `collectors/ponymail.py`; the full month histogram
    isn't used by this collector at all.

- **dev_2009-01.mbox**: `mbox.lua` response for `dev@cassandra.apache.org`, January 2009
  (the list's first active month — 1 message).
  - URL: `https://lists.apache.org/api/mbox.lua?list=dev&domain=cassandra.apache.org&date=2009-01`
  - Recorded: 2026-09-25

- **dev_2009-02.mbox**: `mbox.lua` response for `dev@cassandra.apache.org`, February 2009
  (3 messages: one standalone, plus a 2-message reply chain with real
  `In-Reply-To:`/`References:` headers — good, small thread-reconstruction coverage).
  - URL: `https://lists.apache.org/api/mbox.lua?list=dev&domain=cassandra.apache.org&date=2009-02`
  - Recorded: 2026-09-25

## D1/D16: bodies stripped before saving

**Every message body in both `.mbox` fixtures was stripped before being committed** —
each message's payload was replaced with the fixed placeholder text
`[body stripped for fixture -- D1/D16 metadata-only]` while every header
(`Message-ID`, `From`, `Date`, `Subject`, `In-Reply-To`, `References`, ...) was left
exactly as recorded. This repo's hard rule (DECISIONS.md D1/D16) is that no message body
is ever persisted anywhere — including a test fixture — so these fixtures never
contained the real body text in the first place, not just in what the collector reads
from them. `tests/test_ponymail_collector.py`'s
`test_no_body_or_subject_text_reaches_any_output_column` scans both fixture files (and
every string column of both output tables) for a substring of the real body text that
was in `dev_2009-01.mbox` before stripping, and fails if it's ever found again.
