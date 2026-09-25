# Runbook

Operational procedures for cassandra-project-health.

## Triggering the Nightly Pipeline Manually

The nightly pipeline (`nightly.yml`) runs automatically on a cron schedule (UTC 06:17 daily). To trigger a run manually:

```bash
gh workflow run nightly.yml --ref main
```

After triggering, monitor the run in GitHub Actions. The workflow:

1. Clones the main repo and checks out the `data` branch (or creates it on first run)
2. Runs the full pipeline: `project-health run --project projects/cassandra.yaml ...`
3. Commits collected data (raw partitions, snapshots, manifests, watermarks) to the `data` branch
4. Deploys the static dashboard to GitHub Pages (only if metrics computation succeeded)

## Re-Enabling a Disabled Workflow

GitHub disables scheduled workflows in public repos after 60 days without any repository activity. To re-enable:

```bash
gh workflow enable nightly.yml
```

The workflow will resume at the next scheduled time (06:17 UTC).

## Inspecting the Latest Run

After a run completes, inspect the run manifest on the `data` branch:

```bash
git fetch origin data
LATEST=$(git ls-tree -r --name-only origin/data | grep '^manifests/' | sort | tail -1)
git show "origin/data:$LATEST" | jq .
```

The manifest includes:

- `run_id`: unique identifier for this run (format: `YYYY-MM-DDTHHMMSSZ-<shortsha>`)
- `trigger`: how the run was initiated (`schedule` or `workflow_dispatch` from GitHub Actions, or `manual` by default locally)
- `started_at` / `completed_at`: run start/end times (ISO 8601)
- `pipeline_code_sha`: git commit SHA of the pipeline code
- `sources`: per-source status with error details (if any)
- `status`: one of `ok` (all succeeded), `degraded` (metrics missing), or `failed` (collection error)
- `metrics_computed`: list of computed metrics
- `metrics_missing`: registered metrics that produced no rows (if `status: degraded`)
- `data_branch_commit`: commit SHA of the data branch write (if successful)
- `site_deploy_status`: status of the Pages deployment (if attempted)

## Handling Failed Sources

When a source collection fails after retries, the manifest marks it with `status: failed` under that source. The pipeline preserves data reliability by:

1. Recording the failure in the source's entry (with error details)
2. Pointing `last_good_snapshot` to the most recent successful run for that source
3. Reusing that source's previous data for metric computation
4. **Continuing the run** — a failed source does not affect the overall run status (`status` remains `ok` if metrics succeed), and the dashboard still deploys with a staleness badge for that source

To respond to a failed source:

1. **Check the error** in the manifest: `git fetch origin data && git show origin/data:manifests/RUNID.json | jq '.sources.<source>'`
2. **Verify the data branch** commit succeeded: the raw data from the last good run is still available
3. **Fix the issue** (e.g., update credentials, resolve network/rate-limit problems)
4. **Manually trigger** another run: `gh workflow run nightly.yml --ref main`

The next successful run will record a new `last_good_snapshot` for that source and update the staleness badge on the live dashboard.

## Monitoring Pipeline Health

- **GitHub Actions**: https://github.com/pmcfadin/cassandra-project-health/actions/workflows/nightly.yml
- **Data branch commits**: `git fetch origin data && git log origin/data --oneline | head -20`
- **Latest status**: `git fetch origin data && LATEST=$(git ls-tree -r --name-only origin/data | grep '^manifests/' | sort | tail -1) && git show "origin/data:$LATEST" | jq '.status'`
