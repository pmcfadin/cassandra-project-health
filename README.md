# data

Orphan branch holding cassandra-project-health's collected data
(ARCHITECTURE.md §4.2). Not part of `main`'s history.

```
raw/<source>/<table>/date=YYYY-MM-DD/part-*.parquet   # append-only, one partition per run
snapshots/<run_id>/metrics.parquet                     # full computed-metric output for that run
manifests/<run_id>.json                                 # run manifest (ARCHITECTURE.md §5)
state/                                                  # per-source watermarks + last-good-run pointers
```

Written by `.github/workflows/nightly.yml`. Do not edit by hand.
