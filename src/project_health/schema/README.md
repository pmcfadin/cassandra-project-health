# Normalized table schemas

This package (`project_health.schema`) holds the pyarrow schema for every M0
normalized table (`docs/spec/ARCHITECTURE.md` §3) and a `validate(table_name,
table)` function that fails loudly — raising `SchemaValidationError` — on a
missing column, a wrong column type, or a null in a non-nullable column.
`project_health.storage.write_partition` and `read_table` both call it, so no
data crosses the collector → Parquet boundary without matching its declared
shape (ARCHITECTURE.md §9).

Identifiers (`*_id`, `*_key`) are stored as `pa.string()` (e.g. a UUID's
canonical string form); timestamps are UTC-aware (`pa.timestamp("us",
tz="UTC")`) except where a table calls for a plain date.

Every fact table (`contribution_event`, `file_change_event`, `review_event`,
`issue`) carries a `source_snapshot_id` column, tracing each row back to the
collector run that produced it (ARCHITECTURE.md §3, §5). Identity tables
(`person_identity`, `identity_link`) and the run/metric-registry tables do
not — they aren't sourced from a single collection run.

`file_change_event` (issue #53, `truck_factor`) is one row per (commit, file)
pair from `git log --no-merges --name-status` — the same commit range/
watermark as `contribution_event`, just exploded to file granularity, with
generated/vendored paths dropped per `projects/<id>.yaml`'s
`truck_factor.excluded_path_globs`. It never reads file *contents* (only the
path and the status letter git reports), so it works against a blobless
clone the same way `contribution_event`'s collection does.

See `tables.py` for the full column list of every table.

## Raw identifiers vs. resolved identity

Collectors (#4, #5) run before identity resolution (#6), so a fact-table row
can't be written with a resolved `identity_id` yet — and per ARCHITECTURE.md
§3 / D2 ("nothing changes silently"), raw collected data is immutable, so
identity resolution must be re-runnable over it after the fact (a new
heuristic, a manual `identity_overrides.yaml` entry, etc. can change who a
raw identifier resolves to, without ever mutating the raw row itself).

For that reason, every fact table carries **both**:

- a nullable `*_identity_id` column — filled in by identity resolution (or a
  later re-run of it), `null` as written by a collector;
- the **raw identifier(s)** the collector actually observed, always
  populated and non-nullable where a source always provides one:
  - `contribution_event`: `author_raw_type` / `author_raw_value` (e.g.
    `git_email` / `alice@example.org`), plus optional `author_display_name`.
  - `file_change_event`: same `author_raw_type` / `author_raw_value` shape as
    `contribution_event` (it's the same commit's author) — never a distinct
    identity resolution path of its own.
  - `review_event`: `reviewer_raw_type` / `reviewer_raw_value` (commit
    trailers use `git_name`, JIRA reviewer fields use `jira_username`), plus
    optional `author_raw_type` / `author_raw_value` for the patch author when
    the source records one.
  - `issue`: `reporter_raw` / `assignee_raw` (raw JIRA usernames; nullable —
    not every issue has both set).

Metrics resolve identities by joining a fact table's raw identifier column(s)
against `identity_link.source_value` (matched on `identity_link.source_type`)
to get to `identity_link.identity_id`, rather than relying on `*_identity_id`
being pre-populated — that join is what lets identity resolution improve
(or get corrected via `identity_overrides.yaml`) without any collector
re-running or any raw row being rewritten. `identity_link.source_type`
includes `git_name` for exactly this reason (commit-trailer reviewer names
aren't email addresses).

## Output contract: `metric_value`

`metric_value` is the contract every metric (task #7) writes to and the site
generator (task #8) reads from — the one table both sides of the pipeline
must agree on without either reading the other's code. One row per (metric,
definition version, window).

| Column | Type | Nullable | Meaning |
|---|---|---|---|
| `metric_id` | string | no | Which metric, e.g. `active_contributors_monthly` (METRICS.md). |
| `definition_version` | string | no | The `metric_definition_version.version` that produced this row (ARCHITECTURE.md §4.4) — never overwritten; a definition bump adds new rows under a new version. |
| `window_start` | date | no | Inclusive start of the metric's reporting window. |
| `window_end` | date | no | Inclusive end of the metric's reporting window. |
| `value` | float64 | yes | The computed value. `null` whenever `flag != 'ok'`. |
| `n` | int64 | no | Population size behind this value (METRICS.md §0.6 — the number the minimum-sample-size floor is checked against). |
| `flag` | string | no | `'ok'` or `'insufficient_data'` (METRICS.md §0.6: below the metric's stated floor, the dashboard renders "insufficient data" instead of a number, and no status is computed for that period). |
| `run_id` | string | no | FK to `run_manifest.run_id` — the run that computed this row. |
| `computed_at` | timestamp (UTC) | no | When this row was computed. |
| `details_json` | string | yes | Free-form JSON with anything metric- or site-specific that doesn't need its own column — e.g. per-source breakdowns, exclusion counts, or (until they get dedicated columns) the richer provenance fields ARCHITECTURE.md §5 describes (`dimension`, `tier`, `exclusions`, `source_snapshot_ids`, `pipeline_code_sha`). |

`flag` is the load-bearing column for METRICS.md §0.6's minimum-sample-size
rule: a metric task computes `n` for its window, and if `n` is below that
metric's floor, it writes `flag = 'insufficient_data'` and `value = null`
rather than a number — the site must never render a value whose row has
`flag != 'ok'`.
