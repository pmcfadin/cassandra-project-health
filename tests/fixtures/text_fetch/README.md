# Text-fetch fixtures (issue #43)

These fixtures record the **response shape** this project's live checks found for
Pony Mail's month-digest endpoint and ASF JIRA's comment-listing endpoint (see
`src/project_health/classify/text_fetch.py`'s module docstring for the verified URLs).
Every text field (`from`, `subject`, `body`, `message-id`, author names, comment bodies)
is **synthetic**, invented for this fixture, never a real dev@ message or JIRA comment.
Only the JSON shape (field names, nesting, field types) reflects a real, live-verified
response, per this issue's requirement to "record the real response shape... but replace
every text field with synthetic text before saving any fixture."

- **ponymail_month_dev_2026-09.json**: shape of
  `GET /api/stats.lua?list=dev&domain=<domain>&d=YYYY-MM` (Pony Mail month digest).
  All `@example.com`/`@example.org` addresses, all bodies invented. Includes one
  message with quoting/signature to exercise `preprocess.strip_quoted_text`/
  `strip_signature`, one with a stack trace and a personal-attack-shaped sentence
  (still synthetic) to exercise `strip_stack_traces`, and one automated-sender-shaped
  message (`[bot]`, "(JIRA)" display name) to exercise `is_automated_sender`.
- **jira_comments_EXAMPLE-1.json**: shape of
  `GET /rest/api/2/issue/{key}/comment` (JIRA comment listing). Includes one comment
  with JIRA wiki markup (`h2.`, `{code}`, `{{monospace}}`) and one automated-sender-shaped
  comment (`githubbot`/"ASF GitHub Bot").

`tests/test_text_fetch.py::test_fixtures_contain_no_real_looking_email_addresses` scans
every fixture file in this directory for anything matching an email-address pattern and
asserts every match ends in `@example.com`, `@example.org`, or `@example.net` -- the
project's convention for "this is definitely synthetic," per this issue's requirement to
test the fixtures for real-looking addresses.
