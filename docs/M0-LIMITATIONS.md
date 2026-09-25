# M0 Limitations

The M0 prototype has working data collection and basic metrics. Known limitations for this release:

## Identity Resolution

Identity resolution is naive: a reviewer may appear as both a JIRA username and a commit trailer name, and these are not automatically merged. Handles and full names are also kept separate. This results in overcounting unique reviewers when the same person contributes under multiple identities.

Manual identity merging is supported via `identity_overrides.yaml` on a case-by-case basis with human review in pull requests (see `ARCHITECTURE.md` §3). Cross-source identity merging (e.g., linking a git email to a JIRA username) is not yet automated.

## No Baseline Status

Baseline status (comparing current metrics to historical trend) is not yet implemented. The dashboard shows raw metrics only. Baseline status is specified in `docs/spec/SCORING.md` and planned for M3.

## No Monthly Editions

The pipeline produces snapshots from nightly collection runs only. Frozen, citable monthly reports under `/reports/YYYY-MM/` (see `DECISIONS.md` D5) are not yet published. This is planned for a future release.

## JIRA Data Limitations

- **Limited field scope**: Only issue metadata is collected: summary, status, status category, priority, issue type, creation date, update date, resolution date, reporter, assignee, and the two reviewer custom fields. Comments and changelog entries are not collected.
- **Activity signal**: Staleness is computed from `issue.updated_at` only. Historical activity data accumulates only from nightly snapshots, not retroactively. This means early snapshots may show stale issues that were actually active before snapshotting began.

## Reviewer and Contributor Data

- **Reliable from 2017 onward**: Commit-trailer and JIRA reviewer data are considered reliable from 2017-01-01 onward. Coverage is patchier before this date.
- **Placeholder reviewers dropped**: Placeholder values like "TBD" in reviewer fields are excluded and counted separately.
- **new_contributors_monthly sample floor**: This metric is mostly blank because the sample floor (minimum threshold) is applied to headcounts, not individual dates. See [issue #27](https://github.com/pmcfadin/cassandra-project-health/issues/27) for details.

## Architecture References

For detailed design decisions and planned extensions, see:

- `docs/spec/ARCHITECTURE.md` — system design, data model, identity resolution
- `docs/spec/DECISIONS.md` — design decisions and trade-offs
- `docs/spec/METRICS.md` — metric definitions and calculations
- `docs/spec/SCORING.md` — baseline scoring (planned for M3+)
- `docs/spec/ROADMAP.md` — feature roadmap through M3
