# Project Health

Automated health reporting for Apache Cassandra and other open-source projects. Tracks contributor sustainability, reviewer capacity, responsiveness, organizational diversity, and release cadence via git, GitHub, ASF JIRA, and mailing-list metadata (Phase 1); mailing-list/JIRA/PR content and ASF Slack are added later, gated behind classifier validation and PMC/Infra approval respectively (Phase 2, see `docs/spec/DECISIONS.md` D1).

**Status:** M0 prototype — first working implementation with data collection and basic metrics.

See [docs/M0-LIMITATIONS.md](docs/M0-LIMITATIONS.md) for known limitations. For architecture and design decisions, see [docs/spec/](docs/spec/).

**Live dashboard:** https://pmcfadin.github.io/cassandra-project-health/

## Local Quickstart

Requires Python 3.12+.

```bash
# Clone the repository
git clone https://github.com/pmcfadin/cassandra-project-health.git
cd cassandra-project-health

# Create and activate a virtual environment
python3 -m venv venv
. venv/bin/activate

# Install in development mode
pip install -e .[dev]

# Run the pipeline on a scratch data directory
mkdir scratch-data scratch-work
project-health run \
  --project projects/cassandra.yaml \
  --data-dir scratch-data \
  --workdir scratch-work
```

The command completes in ~2 minutes on first run (full git and JIRA crawl). Output includes collected metrics, run manifest, and status.

**Exit codes:**
- Exit 0: `status: ok` (all registered metrics produced data). A failed source does not change this: it is marked `failed` in the manifest, its last good data is reused, and the site shows a staleness badge for it.
- Exit 1: `status: degraded` (a registered metric produced no rows) or `status: failed` (metrics computation error)

## Data

Collected data persists on the orphan `data` branch (not in main's history):

```
data/raw/<source>/<table>/date=YYYY-MM-DD/part-*.parquet    # append-only, one partition per run
data/snapshots/<run_id>/metrics.parquet                      # full computed-metric output
data/manifests/<run_id>.json                                 # run manifest with status, sources, errors
data/state/                                                  # per-source watermarks and last-good-snapshot pointers
```

Inspect the latest manifest on the `data` branch:

```bash
git fetch origin data
LATEST=$(git ls-tree -r --name-only origin/data | grep '^manifests/' | sort | tail -1)
git show "origin/data:$LATEST" | jq .
```

## Testing

```bash
pytest                  # Run all tests
pytest -k test_name     # Run a specific test
ruff check .            # Lint the codebase
```

Tests use fixtures and do not hit the network. See [docs/spec/](docs/spec/) for detailed architecture and [docs/RUNBOOK.md](docs/RUNBOOK.md) for operational procedures.
