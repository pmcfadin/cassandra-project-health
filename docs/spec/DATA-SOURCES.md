# Data Sources

Deliverable #4 of the research phase (`docs/research.md`). Consistent with `docs/spec/DECISIONS.md`.

**Verification method.** Every source below was hit live with `curl`, `gh api`, `gh api graphql`, or a `git clone --filter=blob:none` on **2026-09-25**, from a residential/dev network, no special auth beyond the already-authenticated `gh` CLI (account `pmcfadin`, scopes `gist, project, read:org, repo, workflow`). Exact endpoints and observed responses are recorded inline. Anything not tested live is marked **UNVERIFIED** and is a documented assumption, not a fact. Nothing here was cloned into the project repo — the git inspection used a partial clone (`--filter=blob:none --no-checkout`) in the session scratchpad, discarded after use.

Per D1, this document covers everything needed for Phase 1 (git, JIRA, GitHub PRs, mailing-list *metadata*, releases, ASF membership metadata) plus what Phase 2/2a/2b will need (mailing-list content, JIRA/PR comment content, Slack), so those phases aren't blocked on new research later.

---

## 1. Git — `github.com/apache/cassandra`

**Verified 2026-09-25.** Partial-cloned `https://github.com/apache/cassandra.git` (`--filter=blob:none --no-checkout`; `.git` ≈ 56 MB with blobs elided).

- **History depth.** `git rev-list --count HEAD` (trunk only) → **32,374 commits**; a fuller count across all branches (per a parallel probe run the same day, `docs/spec/data-probe.md`) → **33,429 commits**, **743 unique author emails**. First commit dated 2009-03-02, and it's a `git-svn-id: https://svn.apache.org/repos/asf/incubator/cassandra/trunk@749218 ...` trailer — the repo is an SVN-to-git conversion; pre-2009 provenance is whatever the SVN import preserved (author name/email, timestamp), not richer. This lines up with `projects.apache.org`'s `created: 2012-04-10` (DOAP record date) and the JIRA project's oldest issue, `CASSANDRA-1`, created 2009-03-07 — git, JIRA, and DOAP metadata are mutually consistent on project age.
- **`patch by X; reviewed by Y for CASSANDRA-N` convention.** An initial 30-commit spot sample (below) suggested a middling ~50-60% hit rate; a full-history pass (`docs/spec/data-probe.md`, corrected 2026-09-25) shows that estimate understated it badly because it didn't exclude merge commits. The real picture, `git log --no-merges` by year, share of non-merge commits whose message contains "reviewed by":

    | Year | Non-merge commits | % with "reviewed by" |
    |---|---|---|
    | 2009 | 1,025 | 56% |
    | 2010 | 1,801 | 43% |
    | 2011 | 2,472 | 45% |
    | 2012 | 1,692 | 57% |
    | 2013 | 1,793 | 46% |
    | 2014 | 1,851 | 57% |
    | 2015 | 1,838 | 66% |
    | 2016 | 1,252 | 71% |
    | 2017 | 646 | 77% |
    | 2018 | 405 | 83% |
    | 2019 | 245 | 78% |
    | 2020 | 602 | 81% |
    | 2021 | 588 | 82% |
    | 2022 | 568 | 87% |
    | 2023 | 626 | 84% |
    | 2024 | 519 | 79% |
    | 2025 | 589 | 85% |
    | 2026 (partial, through Sept) | 361 | 82% |

  **Reviewer-trailer coverage is near-universal from 2018 onward (79-87%) and much patchier before 2016 (43-57%)** — reviewer-concentration/reviewer-population metrics should establish their baseline period starting around 2017, and treat pre-2016 coverage as too sparse to trust for that specific metric (other metrics with less reliance on this trailer can still use the full 2009+ history).
  - Variants observed (30-commit spot check, both eras): `patch by X; reviewed by Y for CASSANDRA-N`, `Patch by X, reviewed by Y for CASSANDRA-N` (capital P, comma instead of semicolon), multi-author `patch by A and B; reviewed by C, D and E for CASSANDRA-N`, and a `Co-authored-by:`/`Co-Authored-By:` trailer (capitalization inconsistent) on a minority of commits (297/33,429 ≈ 0.9% project-wide per the full-history probe) for a second/third patch author. **This is a regex-parseable but non-guaranteed field** — a collector must treat absence as "unknown," not zero reviewers, and should keep the raw commit subject/body for audit per D2.3.
  - **Merge commits must always be excluded from these denominators.** Cassandra forward-merges each fix across active release branches (e.g. `cassandra-5.0` → `cassandra-6.0` → `trunk`), so a large share of total commit volume is `Merge branch ...` commits carrying no patch-by trailer of their own — not a sign the convention is weak, just an artifact of the branching model. The naive unfiltered count (all commits, merges included) put the trailer at only 35.9% project-wide (11,990/33,429) — a materially misleading number if used as-is; always filter with `--no-merges` first.
- **`.mailmap`.** Not present at `HEAD` (`git show HEAD:.mailmap` → `fatal: path '.mailmap' does not exist`). Identity dedup across git email addresses is **not solved upstream**; the collector must do its own normalization (see §11).
- **Bot commits.** `git log --format='%an <%ae>'` deduped and grepped for `bot|dependabot|github-actions|\[bot\]` → **zero matches** across the full history. All 742 unique author emails are human. GitHub Actions workflows exist (`.github/workflows/build-scripts.yaml`, `code-check.yaml`, `jenkins-check.yaml`) but none of them push commits back to the repo. Bot filtering for git history is currently a non-issue for this project (revisit if that changes).
- **`.asf.yaml`** (present at repo root) is worth collecting once: it sets `enabled_merge_buttons: {squash: false, merge: false, rebase: true}` — meaning GitHub PRs are merged via rebase, so **PR-merge commits and the corresponding trunk commit are usually the same SHA**, which is useful for joining GitHub PR data to git commit data without fuzzy matching.
- **gitbox.apache.org.** `curl -I https://gitbox.apache.org/repos/asf?p=cassandra.git` → `302` redirect straight to `https://github.com/apache/cassandra`. Gitbox is **not** a separate authoritative mirror to reconcile against for this project — GitHub is canonical. (This corrects the "and gitbox" framing in the task brief; worth flagging to the architecture doc.)
- **Access/auth.** Anonymous, unauthenticated, unlimited by any rate-limit header observed (standard `git clone`/`fetch` over HTTPS, no ToS friction beyond GitHub's general terms).
- **Incremental strategy.** `git fetch` nightly against a persistent shallow-or-partial local clone (or a cached `.git` restored from a Release asset per the architecture doc's storage plan), then `git log <last-cached-sha>..HEAD` for new commits. Watermark = last-processed commit SHA per branch (trunk plus active release branches, e.g. `cassandra-5.0`, `cassandra-4.1`).

## 2. ASF JIRA REST API — `issues.apache.org/jira`, project `CASSANDRA`

**Verified 2026-09-25** against `https://issues.apache.org/jira/rest/api/2/*` (Jira **Server** 8.20.10, per `serverInfo` — not Jira Cloud, so the v3/Cloud REST docs and Cloud rate-limit docs do **not** apply; this is the older Server/Data-Center REST v2 surface).

- **`GET /rest/api/2/serverInfo`** → 200, confirms `deploymentType: Server`, version 8.20.10.
- **`GET /rest/api/2/project/CASSANDRA`** → 200, returns project metadata plus a `components` list (Accord, Analytics Library, Build, CI, Client/cpp-driver, …) usable for sub-area breakdowns.
- **`GET /rest/api/2/search?jql=project=CASSANDRA&maxResults=0`** → `{"total": 21483}` — **21,483 issues** as of 2026-09-25. Oldest (`ORDER BY created ASC`) is `CASSANDRA-1`, created 2009-03-07.
- **Pagination.** Standard `startAt`/`maxResults`; Atlassian's own guidance (per current developer docs) caps a single search at 1000 results and recommends 100–200 per page for performance, even though the field itself doesn't hard-reject larger values.
- **Incremental queries.** `jql=project=CASSANDRA AND updated>=-1d` works as expected (returned 12 issues updated in the trailing 24h at test time) — this is the watermark field: store `max(fields.updated)` seen and query `updated >= "<ISO date>"` (or the JQL relative syntax) on each run.
- **Changelog + comments expansion.** `GET /rest/api/2/search?...&expand=changelog&fields=key,summary,comment` returns full comment bodies and (with `expand=changelog`) the field-change history in one call — this is how status transitions (e.g., time-to-first-response, time-to-resolution) and full comment text (needed for Phase 2a) get collected without N+1 requests.
- **Rate limits / anonymous access.** No `X-RateLimit-*` headers were present on any response; `X-AUSERNAME: anonymous` confirms the endpoint is fully usable without an ASF JIRA account. Apache Infra's general rate-limiting policy (documented at `infra.apache.org/blog/rate-limiting-on-apache-services`) applies at the infrastructure/CDN layer — abusive traffic gets `HTTP 429` with a short (documented as "generally within two minutes") cool-down; this is IP-block-based, not per-endpoint-published. **Practical implication:** page politely (a few hundred ms between pages, no parallel fan-out), check for 429, back off — a nightly incremental job (a few dozen issues changed per day, per the `updated>=-1d` sample) will never come close to tripping it.
- **Reviewer/Reviewers custom fields.** `GET /rest/api/2/field` lists several review-shaped custom fields on this instance: `customfield_10022` ("Reviewer"), `customfield_12314135`/`customfield_12314141` ("Reviewer"/"Reviewers"), `customfield_12313420` ("Reviewers"), plus process fields like `customfield_12313422` ("Enable Automatic Patch Review") and `customfield_12313520` ("Review Patch?"). A spot check on one issue (CASSANDRA-21671, `customfield_10022` → `null`) understated this — field *presence* on one issue doesn't tell you field *usage* project-wide. Checking population with `jql=project=CASSANDRA AND cf[N] is not EMPTY` for each field id gives the real picture:

    | Field id | Name | CASSANDRA issues populated |
    |---|---|---|
    | `customfield_12313420` | Reviewers (multi-user) | **11,125** of 21,483 (≈52%) |
    | `customfield_10022` | Reviewer (single user) | **7,104** of 21,483 (≈33%) |
    | `customfield_12314135` / `customfield_12314141` | Reviewer / Reviewers | 0 for CASSANDRA — these are shared instance-wide custom fields used by other ASF projects, not Cassandra |

  Values are structured, not free text — e.g. `{"name":"mck","key":"michaelsembwever","displayName":"Michael Semb Wever"}` — carrying the **JIRA username** (often, not always, identical to the person's ASF id) plus a display name. **Both populated fields are genuine, well-used reviewer-identity sources** — `customfield_12313420` (multi-user "Reviewers") is actually the more heavily used of the two and should be the primary JIRA-side reviewer signal, with `customfield_10022` as a secondary/legacy one. Together with the git commit-trailer data above, this gives **two independent reviewer-identity sources that can cross-validate each other** — a reviewer named in both the JIRA field and the corresponding commit's `reviewed by` trailer is a high-confidence signal; a mismatch or single-source-only name is worth flagging rather than trusting blindly.
- **Licensing/ToS.** Apache-hosted infrastructure; content is governed by the ASF's public-forum/privacy policies (see §11 below) rather than a separate Atlassian ToS, since this is ASF's own self-hosted Jira Server instance, not Atlassian Cloud.
- **Incremental strategy.** Watermark = `updated` timestamp of the most-recently-seen issue; nightly `jql=project=CASSANDRA AND updated>="<watermark>"&expand=changelog&fields=*all` (or an explicit field list) paged at ~100/request.

## 3. GitHub REST + GraphQL — `apache/cassandra` and related repos

**Verified 2026-09-25** via `gh api` (REST) and `gh api graphql`, authenticated as `pmcfadin` (OAuth token via `gh auth login`, scopes `gist, project, read:org, repo, workflow`).

- **`apache/cassandra` repo metadata.** `has_issues: false`, `has_discussions: false`, `has_projects: false` — confirmed live: **GitHub Issues and Discussions are disabled**; JIRA is the sole issue tracker, exactly as D-facts in `DECISIONS.md` assume. `default_branch: trunk`. `open_issues_count: 547` is actually open-PR count given Issues is off. Repo created 2009-05-21 (this is the GitHub mirror's creation date, not the project's — git history itself goes back to the same March 2009 SVN commits, so this is just when ASF Infra stood up the GitHub mirror).
- **PRs.** `search/issues?q=repo:apache/cassandra+is:pr` → **5,207 total** PRs (open+closed) as of test time. Earliest PRs (`pulls?state=all&sort=created&direction=asc`) date to 2011-09-28 (#4) — PR usage predates the JIRA-primary/GitHub-secondary norm and has clearly grown as a review surface over time; it is a genuine subset of review activity, not the full picture (per D-facts).
- **Releases vs. tags.** `GET /repos/apache/cassandra/releases` → **0** GitHub Releases published. `GET /repos/apache/cassandra/tags` (paginated) → **338 tags**. Cassandra does not use GitHub's Releases feature at all; **git tags are the release signal on GitHub**, and the authoritative release artifacts live on `downloads.apache.org`/`archive.apache.org` (§5), not GitHub Releases.
- **GraphQL.** Confirmed working: sample query against `repository(owner:"apache", name:"cassandra") { pullRequests { ... reviews { ... } } }` returned real data (reviews array was empty for the two most-recent PRs sampled — plausible for just-opened PRs). Review **threads** (line-level comments) are also queryable (`pullRequest.reviewThreads.nodes.comments`) — tested, returned an empty array for the sampled PR (again plausible; no line comments yet on that PR). Both endpoints are live and correctly shaped; volume wasn't exhaustively sampled beyond confirming the schema returns real objects.
- **Discussions.** `hasDiscussionsEnabled` via GraphQL → `false`, confirming the REST result.
- **Rate limits.**
  - Live-checked via `gh api rate_limit` (PAT/OAuth token, i.e. this session's `gh` auth): `core: 5000/hr`, `graphql: 5000/hr` (point-based, e.g. `rateLimit { cost }` showed `cost: 1` for a trivial query — complex queries cost more per GitHub's documented point formula), `search: 30/min`.
  - **GITHUB_TOKEN in Actions** — not directly testable from this session (no Actions run available), so this is **UNVERIFIED by direct test**, but per current GitHub Actions documentation (`docs.github.com/en/actions/reference/limits`, checked via web search 2026-09-25): the built-in `GITHUB_TOKEN` is capped at **1,000 requests/hour per repository** (15,000/hr on GitHub Enterprise Cloud). A nightly collection job doing incremental PR/review pulls against one repo should stay well under that, but a **classic PAT is recommended over the bare `GITHUB_TOKEN`** for the nightly job specifically because it gets the full 5,000/hr budget and can read across the multiple `apache/*` repos below in one run without per-repo bucket exhaustion.
- **Related repos — existence check** (`gh api repos/apache/<name>`, all 2026-09-25):

  | Repo | Exists | Issues | Discussions | Last push (test time) |
  |---|---|---|---|---|
  | `apache/cassandra` | yes | disabled | disabled | 2026-09-25 |
  | `apache/cassandra-dtest` | yes | disabled | disabled | 2026-08-31 |
  | `apache/cassandra-java-driver` | yes | disabled | disabled | 2026-09-25 |
  | `apache/cassandra-website` | yes | disabled | disabled | 2026-09-24 |
  | `apache/cassandra-sidecar` | yes | disabled | disabled | 2026-09-10 |
  | `apache/cassandra-analytics` | yes | disabled | disabled | 2026-09-18 |
  | `apache/cassandra-accord` | yes | disabled | disabled | 2026-09-15 |
  | `apache/java-driver` | **no** (404) | — | — | — |

  All active Cassandra-adjacent repos are on GitHub Issues=off, Discussions=off — none of them use GitHub Issues/Discussions as a tracker; JIRA `CASSANDRA` project's components list (§2) covers Accord and Analytics, suggesting those sub-projects may file JIRA issues under the main project rather than per-repo trackers. Not independently confirmed which JIRA project(s) `cassandra-sidecar` and `cassandra-java-driver` use — flag as **UNVERIFIED, follow-up needed** before building per-repo adapters for those.

  A companion probe (`docs/spec/data-probe.md`, same day) additionally confirms `apache/cassandra-gocql-driver`, `apache/cassandra-builds`, and `apache/cassandra-in-jvm-dtest-api` also exist — not independently re-checked in this session, listed here so the architecture doc has the fuller candidate set when deciding which repos a Cassandra adapter should pull from.
- **Identity fields available.** `GET /users/{login}` exposes a free-text `company` field. Spot-checked two real contributors: `michaelsembwever` → `company: "https://apache.org/"`, `email: "mck@apache.org"`; `blambov` → `company: "datastax"`, `email: null`. Confirms D6's approach (seed org affiliation from `company` + email domain) is viable but noisy — `company` is self-reported free text (one is a URL, one is a lowercase brand name, casing/format is not normalized), which is exactly why D6 calls for a curated `affiliations.yaml` rather than trusting this field directly.
- **Contributors scale.** `GET /repos/apache/cassandra/contributors?per_page=1&anon=1` pagination `Link` header shows `page=692` as last page → roughly **~691 unique contributors** (including anonymous/pre-GitHub-account commits) by GitHub's own reckoning — a useful sanity check against the 742 unique git author *emails* found directly in git log (close but not identical, as expected: GitHub's contributor dedup uses its own login-matching, not raw email).
- **Licensing/ToS.** GitHub's Acceptable Use Policies (checked 2026-09-25) distinguish scraping (discouraged/restricted) from API usage (governed by API Terms, GitHub ToS §H) and explicitly permit "researchers ... scrape public, non-personal information from GitHub for research purposes" — this project's use (API-only, public repos, aggregate project-health metrics, no resale, no spam) fits within acceptable use. Any personal data pulled (names, emails, `company` field) must be handled per GitHub's Privacy Statement — practically, this means not republishing raw personal identifiers beyond what's already public on the profile, and respecting D2.4/D2.5 (aggregates only, uncertainty shown).
- **Incremental strategy.** REST `since`/GraphQL cursor pagination on `pullRequests(after: $cursor)`; watermark = last-seen PR `updatedAt` per repo, plus a periodic full re-scan (e.g. monthly) to catch retroactive edits (title/label changes, re-opened PRs) that an `updatedAt`-only cursor could miss if the field isn't bumped consistently — GitHub's `updatedAt` semantics for PRs are known to be inconsistent across event types, so treat pure incremental cursors as best-effort and validate periodically against a full count.

## 4. Mailing lists — `lists.apache.org` (Pony Mail API)

**Verified 2026-09-25** against `https://lists.apache.org/api/*`.

- **`stats.lua`** — `GET /api/stats.lua?list=dev&domain=cassandra.apache.org` → 200, JSON with `firstYear: 2009, firstMonth: 1, lastYear: 2026, lastMonth: 9` and a full `active_months` histogram (message counts per month back to 2009-01, e.g. `2012-05: 568` messages, showing genuine long-run volume data, not just a stub). Repeated for **`user@cassandra.apache.org`** and **`commits@cassandra.apache.org`**: both also return `firstYear: 2009, firstMonth: 3` through `lastYear: 2026, lastMonth: 9` — all three lists have continuous archives back to the project's earliest months.
- **`mbox.lua`** — `GET /api/mbox.lua?list=dev&domain=cassandra.apache.org&date=2026-08` → `200`, `Content-Type: application/mbox`, `Content-Disposition: attachment; filename=dev_cassandra_apache_org_2026-08.mbox`, downloaded a real 1.9 MB mbox file for that month with 122 `From ` message boundaries. Confirmed the messages carry standard `Message-ID:` and `In-Reply-To:` headers (spot-checked several), which is what thread reconstruction needs.
- **`list.lua`** — `GET /api/list.lua?domain=cassandra.apache.org` → returned `"API Endpoint not found!"` (plain text, not JSON) in this test. Either the endpoint path/params differ from what was guessed or it's deprecated; **do not rely on it** without further investigation — use `stats.lua` (which works and is more useful anyway) to discover list activity instead.
- **`thread.lua` / `email.lua`** — not exercised against a specific message/thread ID in this session (would need a captured `Message-ID` fed back in as a URL-encoded param); the mbox route alone is sufficient for full-content nightly collection and was prioritized. **UNVERIFIED**: exact behavior of `thread.lua`/`email.lua` single-message/thread lookups — reasonable to assume they work (they're the standard, documented Pony Mail JSON endpoints used by the lists.apache.org UI itself), but not independently confirmed here. Recommend a follow-up smoke test before Phase 2a build-out, not before Phase 1 (Phase 1 only needs sender/timestamp/thread-structure metadata, which the mbox headers alone provide without needing `thread.lua`).
- **Thread reconstruction.** Via `In-Reply-To:`/`References:` headers in the mbox output (standard RFC 5322), confirmed present on real messages. This is how Phase 1's "thread structure" requirement (D1: sender, timestamp, thread structure — not content) gets built without touching message bodies.
- **Edits/deletions.** Not tested (would require finding a known-edited message); per ASF's public archive policy (see §11), the general position is that posted mail is permanently and publicly archived — moderators can suppress spam/abuse before it's archived, but the working assumption (not independently verified against a real edit/delete event in this session) is that once archived, messages are not typically altered. Flag as **UNVERIFIED** assumption for the architecture doc: build the collector to detect and log a changed body-hash on reprocessing rather than assuming immutability.
- **Auth.** No authentication required for any of the tested endpoints; content is public (mailing lists are public per ASF policy, see §11).
- **Rate limits.** No explicit Pony-Mail-specific limit documented; falls under the same Apache Infra CDN-level rate-limiting as JIRA (§2) — poll politely, one request at a time, no parallel fan-out.
- **Incremental strategy.** `mbox.lua` is naturally month-bucketed — nightly collection re-pulls the current month's mbox (cheap, re-downloads the whole month but catches any late-archived messages) and never needs to re-pull closed months. Watermark = last fully-processed month + a re-check of the current month each run.

## 5. Releases/tags — git tags, GitHub, `downloads.apache.org`, `archive.apache.org`

**Verified 2026-09-25.**

- **Git tags.** 338 tags (§3).
- **GitHub Releases.** 0 (§3) — not used.
- **`downloads.apache.org/cassandra/`** → 200, directory listing shows current-generation releases across active lines: `3.0.32, 3.11.19, 4.0.21, 4.1.12, 5.0.9` (mirrors only carry recent releases per ASF mirroring policy — old releases are pruned from the live mirror network).
- **`archive.apache.org/dist/cassandra/`** → 200, full historical directory listing starting at `0.3.0`, `0.4.0`, `0.4.1`, `0.4.2`, `0.5.0`, … — this is the **permanent historical archive** (ASF guarantees archive.apache.org never prunes), and is the correct source for full release-history/cadence analysis rather than the rotating `downloads.apache.org` mirror.
- **Auth/licensing.** Fully public, static file listings, no auth, no documented rate limit beyond general politeness (these are plain Apache HTTP directory listings, not an API).
- **Incremental strategy.** Directory listing diff against a cached list of known version directories; new directory = new release. Combine with git tag `tagger date` and (where available) the `CHANGES.txt`/`NEWS.txt` in-repo for release-note text and exact cut date.

## 6. ASF metadata — committer/PMC membership, join dates

**Verified 2026-09-25.**

- **`projects.apache.org/json/projects/cassandra.json`** → 200, DOAP-derived record: PMC contact (`dev@cassandra.apache.org`), `created: 2012-04-10`, bug tracker link, repository link. Useful for a project-level "about" card but **no per-person membership data**.
- **`whimsy.apache.org/public/committee-info.json`** → 200, 705 KB, public, no auth. Contains a `committees.cassandra` object with `established: "02/2010"` and a full `roster` keyed by ASF id, each entry carrying `{name, date}` — e.g. `"absurdfarce": {"name": "Bret McGuire", "date": "2025-05-16"}`, `"aleksey": {"name": "Aleksey Yeschenko", "date": "2014-08-07"}`. **This is exactly the PMC-join-date data the brief asked for, and it is public with no login required** — a materially better find than assuming it needs Whimsy-authenticated access. Note: this is the **PMC** roster; it does not by itself distinguish "committer" from "PMC member" (Cassandra, like most ASF projects, has committers who are not PMC members) — that distinction needs a separate committer list, which was not located as a clean public JSON in this session (Whimsy's committer-vs-PMC roster views are generally behind ASF-committer LDAP auth) — **UNVERIFIED / follow-up**: whether a public committer-only (non-PMC) roster with join dates exists, or whether commit/JIRA-resolution history has to serve as the proxy for "became a committer."
- **`whimsy.apache.org/public/public_ldap_people.json`** → 200, public, 1.3 MB, no auth. Per-person records keyed by ASF id with `{name, createTimestamp}` — e.g. `"jbellis": {"name": "Jonathan Ellis", "createTimestamp": "20090326020005Z"}`. This is **ASF-account creation date**, not "joined Cassandra" date — useful as a person's earliest-possible ASF-wide involvement, and as the anchor for resolving `ASF id → display name`, but not a project-specific join date on its own.
- **`reporter.apache.org/api/pmc/cassandra`** → **`401 Unauthorized`**, `WWW-Authenticate: Basic realm="ASF Committers"` — confirmed **gated to ASF committer credentials**, not usable by this project's public nightly job. Do not plan on this source.
- **Net assessment:** committer/PMC identity + PMC join dates are available from **public, unauthenticated Whimsy JSON** (`committee-info.json` + `public_ldap_people.json`); reporter.apache.org is not available; a clean public non-PMC-committer roster with dates was not directly confirmed in this session's own testing. A companion probe run the same day (`docs/spec/data-probe.md`) did hit `whimsy.apache.org/public/public_ldap_projects.json` and reports Cassandra as `podling: "graduated"` with **49 PMC members ("Owners") and 101 total members (committers + PMC)**, plus a note that the endpoint exposes `modifyTimestamp`/roster-change history but not clean per-member join dates without further per-member queries. Take the specific counts (49/101) as a second-hand data point pending this project's own direct verification, not yet independently re-confirmed here — but it corroborates that `public_ldap_projects.json` is the right endpoint to check next for the committer-vs-PMC distinction §6 above flags as open.

## 7. ASF Slack (`the-asf.slack.com`) — Phase 2b, gated

**Not live-tested** (no bot/account credentials exist for this yet, and D1 explicitly gates this phase behind PMC consensus + Infra approval — testing live access before that approval would be premature). Based on `infra.apache.org/slack.html` and related Infra guidance (checked via web search 2026-09-25):

- Access model: ASF Infra provisions and administers `the-asf.slack.com`; individual ASF-affiliated people and some public participants can join public project channels (e.g. `#cassandra`, `#cassandra-dev`) via an invite/request flow, not open self-signup.
- Infra's stated policy is that **private channels are never made public**, even in aggregate/export form, without explicit unanimous consent of channel members — public channels are the only ones a bot/export could target.
- To collect data programmatically, a Slack app/bot would need to be requested through Infra (per D1: "ASF Infra approval of a read-only bot on public channels"), scoped to read-only on the two named public channels, with a bot token (`xoxb-...`) issued to the project, not a personal user token.
- **Why gated (Phase 2b, not earlier):** this requires organizational approval this project doesn't have yet (PMC + Infra), the data is conversational/personal in a way that needs the classification pipeline (not raw text) before it can be stored per D1's "raw Slack text never committed" rule, and there's no way to verify the actual API shape/rate limits without the bot already being provisioned — so this section is necessarily a plan, not a verified capability, until that approval exists.

## 8. Organizational affiliation sources

Per D6, this is a curated + heuristic approach, not a single API — nothing to "verify" as an endpoint beyond what's already confirmed above:

- **Email domain** — available wherever a real (non-`noreply`) commit or profile email exists; git log shows plenty of corporate domains alongside `@users.noreply.github.com` GitHub-privacy-relay addresses (confirmed in the §1 author sample — e.g. `@apple.com` addresses appear directly in commit trailers/co-author lines).
- **GitHub `company` field** — confirmed populated and usable but free-text/unnormalized (§3).
- **`affiliations.yaml`** — repo-local, reviewed, dated-range file per D6; not a live data source to verify, it's this project's own artifact. Design note for the architecture doc: seed it from the two sources above, human-review before merge, PR-based updates from contributors themselves.

## 9. Optional/other sources

**All verified 2026-09-25:**

- **Planet Cassandra** (`planetcassandra.org`) — `200 OK` (Cloudflare/Netlify-fronted). Site is live; not evaluated for a structured feed/API in this session (would need a follow-up check for RSS or similar before treating it as a collectible source — currently just confirmed reachable).
- **Stack Overflow tag** — `api.stackexchange.com/2.3/tags/cassandra/info?site=stackoverflow` → 200, `{"count": 20870}` — the public StackExchange API works anonymously (300 quota/request batch shown in response, standard public-API throttling, no key required for light use; a registered `key` and/or OAuth raises the daily quota for heavier use).
- **CEP wiki** (`cwiki.apache.org` Confluence) — `GET /confluence/rest/api/content/search?cql=space=CASSANDRA and title~"CEP"` → 200, real results (e.g. "CEP-38: CQL Management API", "CEP-64: Proxy Execution Support"). `GET /confluence/rest/api/space/CASSANDRA` → 200. Anonymous read access to the Confluence REST API works for this space.

## 10. Rate-limit / access summary table

| Source | Auth for read | Confirmed rate limit | History depth (verified) | Incremental key |
|---|---|---|---|---|
| Git (`github.com/apache/cassandra`) | none | none observed | 32,374 commits, to 2009-03 | commit SHA per branch |
| ASF JIRA REST (`issues.apache.org`) | none (anonymous) | none published; Infra CDN 429s on abuse | 21,483 issues, to 2009-03 (`CASSANDRA-1`) | `updated >=` watermark |
| GitHub REST (PAT) | PAT via `gh` | 5,000 req/hr | 5,207 PRs, to 2011-09 | `updatedAt` cursor + periodic full re-scan |
| GitHub REST (`GITHUB_TOKEN` in Actions) | built-in | 1,000 req/hr/repo (documented, not live-tested) | same | same |
| GitHub GraphQL | PAT via `gh` | 5,000 points/hr | same | cursor pagination |
| GitHub Search API | PAT via `gh` | 30 req/min | — | n/a |
| Pony Mail (`lists.apache.org`) | none | none published (Infra CDN-level) | dev/user/commits@ all to 2009 | month bucket + full current-month re-pull |
| `downloads.apache.org` | none | none (static mirror) | current releases only | directory diff |
| `archive.apache.org/dist` | none | none (static) | full history, from 0.3.0 | directory diff |
| Whimsy `committee-info.json` | none | none observed | PMC roster with join dates | full re-pull (705 KB, cheap) |
| Whimsy `public_ldap_people.json` | none | none observed | ASF-id creation timestamps | full re-pull (1.3 MB, cheap) |
| `reporter.apache.org` | **ASF committer login required** | n/a — 401 | not accessible | not usable |
| ASF Slack | gated, Phase 2b | unknown, not provisioned | n/a | n/a (future bot token) |
| StackExchange API | none (light use) | ~300/request-batch shown; higher with key | full SO history via tag | not planned for Phase 1 |
| cwiki Confluence REST | none | not observed | full CEP history | full or `lastModified` filter |

## 11. Licensing, ToS, and privacy constraints

- **ASF public-forum/privacy policy** (`apache.org/foundation/public-archives.html`, `privacy.apache.org/policies/privacy-policy-public.html`, checked 2026-09-25): mail sent to ASF public lists is considered published and archived indefinitely; the sender has no expectation of later deletion, though `privacy@apache.org` can be petitioned. This underwrites Phase 1's use of mailing-list *metadata* and Phase 2a's later use of full content — both are explicitly sanctioned uses of public archive data, not a gray area.
- **GitHub Acceptable Use / ToS** (checked 2026-09-25): API-based collection of public repo/PR/review data for research/aggregate-metrics purposes is within GitHub's stated acceptable use; the constraint is on *use* of personal data collected this way (no spam, no resale, respect the GitHub Privacy Statement) — consistent with D2.4/D2.5 (aggregates only, no per-person scores published).
- **Atlassian / ASF JIRA**: this is ASF's self-hosted Jira **Server**, governed by ASF's own policies (same privacy/public-archive framework as mailing lists), not Atlassian Cloud's separate commercial ToS — no Atlassian-specific constraint applies here.
- **ASF Slack**: governed by Infra's private-channel-never-public policy; the two target channels (`#cassandra`, `#cassandra-dev`) are public by design, but any bot access still requires explicit Infra provisioning (§7) — this is a process/access constraint, not a stated legal one.
- **Privacy concerns common to all sources**: real names, email addresses, and (via JIRA/GitHub profiles) sometimes location/company are all directly exposed by these APIs. This project's own rules (D2.4, D2.5, D6) are stricter than what the upstream sources require — no per-person toxicity/affiliation scores, `unknown` stays `unknown`, small samples suppressed — and should be treated as binding regardless of what the source ToS would technically permit.

## 12. Identity resolution

What each source exposes, verified where noted:

| Source | Identifier(s) exposed | Reliability as a merge key |
|---|---|---|
| Git commits | author name + email (free text, no verification) | Weak alone — same person often has 3-4 different emails (confirmed: e.g. Bernardo Botella Corbi appears under at least 4 distinct email addresses in the git log sample: personal, two different `@apple.com` variants, and a `contacto@` alias) |
| Git commit `Co-authored-by:`/`patch by`/`reviewed by` trailers | free-text display names, not IDs | Weak alone — no stable key, string-matching only |
| GitHub | `login` (stable, unique), `id` (stable numeric), profile `name`/`email`/`company` (self-reported, may be blank or a `@users.noreply.github.com` privacy relay) | `login`/`id` is a **strong, stable key** for anything done through the GitHub UI/API. `users.noreply.github.com` addresses embed the GitHub `id` or `login` (confirmed pattern in the git log sample: `adutra@users.noreply.github.com`, `30613161+Amritmatti@users.noreply.github.com`) — **this specific pattern is a reliable, mechanical git-email ↔ GitHub-login merge** wherever it appears; plain personal emails in commits are not mechanically linkable to a GitHub login without a matching profile email or manual mapping. |
| ASF JIRA | `name`/`key` (JIRA username, stable), `displayName` (free text) | JIRA username is a stable key within JIRA. Confirmed **not guaranteed identical to the ASF id** used by Whimsy/LDAP (they're usually the same string in practice, e.g. `mck` appears as both a JIRA username and an ASF id, but this project should verify the specific mapping, not assume it, since ASF JIRA accounts can predate or diverge from a person's ASF id) |
| Whimsy `public_ldap_people.json` | ASF id (key), `name`, `createTimestamp` | Strong, authoritative key for anyone with an ASF account, but does **not** cover non-ASF-account contributors (most drive-by GitHub PR authors have no ASF id at all) |
| Whimsy `committee-info.json` | ASF id (roster key), `name`, PMC-join `date` | Same ASF-id space as above, PMC-only subset |
| Mailing lists | `From:` header (display name + email, self-reported, unverified), `Message-ID`, `In-Reply-To` | Weak as an identity key (free text, spoofable in principle, and the same person frequently posts from different addresses over 15+ years) — strong for **thread** reconstruction, weak for **person** identity |
| Stack Overflow / Planet Cassandra | account-specific, unrelated identifier spaces | Not mergeable with the above without a person self-declaring the link; out of scope for Phase 1 |

**What's reliably mergeable without human review:**
- GitHub `login`/`id` ↔ `@users.noreply.github.com` git commit emails (mechanical, confirmed pattern in real data).
- Any two records sharing an exact `@apache.org` email address (JIRA, git commits, GitHub profile) are almost certainly the same ASF-id-holder — `@apache.org` addresses are issued per ASF id.

**What must stay uncertain / requires the curated approach:**
- A bare personal email in a git commit (e.g. `jon@jonhaddad.com`) has no mechanical link to a GitHub login or JIRA username unless that same string appears verbatim in another source's profile field. This is exactly the class of merge D2.5 says must not be silently made — a below-confidence-threshold identity stays unmerged and is surfaced (not guessed) per D2.5.
- Display-name-only matching (e.g. "Michael Semb Wever" appearing as a JIRA `displayName`, a git commit author, and a Whimsy roster `name`) is a reasonable *prompt* for a human-reviewed mapping but should not auto-merge — common names collide, and transliteration/formatting varies (the git log sample alone shows the same person's name rendered with and without a middle name across different commits).

**How CHAOSS/GrimoireLab handles this (for comparison, not adoption without adaptation):** GrimoireLab's SortingHat (checked via web search 2026-09-25, `github.com/chaoss/grimoirelab-sortinghat`) models every raw identity (an email, a username, a name string from a specific source) as an `Identity` record, groups them under an `Individual` whose primary key is derived from a hash of the first-attached identity, and lets identities be re-parented to merge multiple raw identities into one Individual — but the actual matching/merging is a mix of automated heuristics (exact email match, etc.) and **explicit human curation** through its `profiles`/`merge` tooling, not a fully automatic fuzzy-match pipeline. That maps directly onto this project's own approach: automatic merge only on mechanically-certain keys (exact email match, the noreply-GitHub pattern above), everything else queued for the same kind of human-reviewed, dated-range curation D6 already specifies for `affiliations.yaml` — the identity map should probably live alongside it as a sibling curated file, reviewed the same way.

## 13. Required access / secrets

What the nightly GitHub Action actually needs, based on everything verified above:

- **`GITHUB_TOKEN`** (built-in, no setup) — sufficient for light read-only REST/GraphQL calls against `apache/cassandra` and the handful of related repos in §3, **if** total calls per run stay under ~1,000/hr per repo. Given this project queries ~7 repos, the built-in token's per-repo bucket is likely fine for PR/review pulls scoped one repo at a time.
- **A classic PAT stored as a repo secret** — recommended in practice, not strictly required: gives the full 5,000/hr shared budget across all `apache/*` repos in one run instead of juggling seven separate 1,000/hr buckets, and is needed regardless if any step queries a repo the `GITHUB_TOKEN` doesn't have implicit access to (cross-repo GraphQL queries in one call, for instance). Scope: public read-only (`public_repo` or equivalent read scopes); no write scopes needed for Phase 1.
- **No secret needed** for: git clone/fetch, ASF JIRA REST, Pony Mail, `downloads.apache.org`/`archive.apache.org`, Whimsy `committee-info.json`/`public_ldap_people.json`, StackExchange tag info, cwiki Confluence REST — all confirmed anonymous-accessible above.
- **Not obtainable / not needed for Phase 1**: `reporter.apache.org` (401, ASF-committer-only — don't plan around it).
- **Phase 2 addition**: an Anthropic API key (for the classification pipeline — model/prompt/schema-versioned per `docs/research.md`'s classification-architecture requirement), stored as a repo secret, used only in-run and never logged with raw message content.
- **Phase 2b addition**: a Slack bot token (`xoxb-...`) for the-asf.slack.com, obtainable only after PMC consensus + Infra approval per D1 — does not exist yet and its exact scope can't be finalized until that approval process defines what Infra is willing to provision.
