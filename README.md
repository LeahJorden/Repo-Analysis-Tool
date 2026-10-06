# Repo Analysis Tool (RAT)

A web dashboard that ingests git repositories and analyses how they evolve —
per file, directory, repository, commit set and author.

> COMS3011A test project. **Milestone 4 (current codebase): ingestion, the
> metric computation engine, the query API and the dashboard.** The four
> filter axes (repository, author, file/directory, commit set), H̄, author
> merging and the full browser UI.

## Quickstart

Requirements: Python 3.10+ and the `git` binary on the `PATH`.

    ./run.sh

Then open <http://localhost:8000> — the dashboard. API documentation is at
`/docs`.

## Usage

1. **Clone from a remote URL** — paste e.g.
   `https://github.com/DaveGamble/cJSON.git`. A full (non-shallow) clone runs
   in a background thread; the page auto-refreshes while it is running.
2. **Upload a zip of a repository** — the archive must contain the repository's
   `.git` directory, or a `.git` file whose `gitdir:` target is also inside the
   archive. The repository may be nested inside a top-level folder.

Each repository moves through:

| status | meaning |
|---|---|
| `uploaded` | zip received, extraction in progress |
| `cloning` | `git clone` in progress |
| `ready` | working tree on disk, HEAD resolved |
| `failed` | see `status_detail` |

Ingestion happens in background threads, so the API stays responsive. If the
application restarts mid-ingestion, the affected repository is marked `failed`
and can be added again.

Ingesting the same source twice is allowed; the create response then carries
`warning: "duplicate_source"` plus the ids of the existing copies.

## Dashboard

The dashboard is served by FastAPI from `app/static/` at `/` — plain ES
modules plus one CSS file, with ECharts 5 vendored at
`app/static/vendor/echarts.min.js`. No build step, no framework, no CDN at
runtime. Every number on screen comes from the API; the UI never recomputes
metrics.

### Views

- **Home** — ingest (clone URL / zip upload) and the repository table with
  live ingestion and compute state.
- **Repository / directory** — metric cards (l+, l−, δ, λ, modifications, η, ρ)
  for the current commit set, four charts, and a sortable table of the
  immediate children; a directory drills in, a file opens the file view.
- **File** — file metrics, churn chart and a per-author ownership table.
- **Authors** — leaderboard (modifications / churn / ownership) and the merge
  panel: tick ≥ 2 identities → **Merge selected**; groups can be **Unmerged**
  (click twice to confirm).
- **Commit set** — |H| and totals for the selection, the churn-over-time chart
  and, in manual mode, the selected commit list.

### Filters and navigation

A persistent filter bar carries the **author** dropdown and the **commit set**
control (**All time**, **Date range** — inclusive UTC days — or **Pick
commits**, a searchable paginated multi-select), the chart **bucket** (day or
week, UTC, weeks start Monday) and a live summary of the current selection
(repo, object, |H|). The sidebar file tree and the breadcrumbs navigate the
repository without ever resetting the author or commit-set filters.

### Charts (ECharts, vendored locally)

Churn (l+ up / l− down) over time with day or week bucketing; top-N children
by churn for the current directory; author ownership (pie, merged identities
combined); and a directory **treemap** coloured by churn — clicking a tile
drills into the directory or file.

### Hash URLs

Every view is linkable; reloading or opening the URL in a new tab reproduces
the exact state (the **Copy link** button copies the current URL):

    #/repo/<id>                  overview
    #/repo/<id>/dir/<path>       directory (path URL-encoded)
    #/repo/<id>/file/<path>      file view
    #/repo/<id>/authors          authors + merge panel
    #/repo/<id>/commits          commit set

    ?author=<author_id>          author filter
    ?mode=all|range|list         commit set: all time / date range / manual list
    ?from=<epoch>&to=<epoch>     range bounds, [from, to) on committer date
    ?shas=<sha,sha,…>            manual list (mode=list)
    ?b=day|week                  chart bucketing

**Recompute** re-runs the computation; entering a commit SHA in the
reference-commit input first moves the analysis reference (from then on every
H̄/|H| counts up to that commit). Loading uses skeletons and spinners, compute
shows a live progress bar, an empty commit set renders zeros with a message
(never an error), and API failures raise a toast with **Retry**.

## API

| Method | Path | Description |
|---|---|---|
| POST | `/api/repos/upload` | multipart `file=` zip upload → `{repo_id, status}` |
| POST | `/api/repos/clone` | `{"url": ...}` → `{repo_id, status}` |
| GET | `/api/repos` | list all repositories |
| GET | `/api/repos/{id}` | fetch one repository |
| POST | `/api/repos/{id}/compute` | start (or restart) the metric computation — optional body `{"reference_commit": "<rev>"}` (below) → `{repo_id, state}` |
| GET | `/api/repos/{id}/compute/status` | `{state, processed, total, percent, error}` |

Supported URL forms: `https://`, `http://`, `ssh://`, `git://`, `file://` and
scp-like `git@host:path`. Authentication prompts are disabled
(`GIT_TERMINAL_PROMPT=0`) so unreachable/private repositories fail fast instead
of hanging.

## Metrics

Computation runs once per repository and is cached in SQLite — analysis
requests never re-run git. It streams a single
`git log --use-mailmap --no-merges --root -M50% --numstat -z` pass and stores
per-commit per-path rows, mirroring the assessment spec exactly:

- per commit and file: `l+`, `l-`, growth `δ = l+ − l-`, churn `λ = l+ + l-`
- binary files are flagged but never measured in lines; pure renames have zero
  increments; rename-plus-edit growth lands on the new path; the root commit
  is diffed against the empty tree
- directory rows are materialised for every ancestor of every changed path
  (`''` is the repository root); repository metrics are the root's roll-up
- commit-set and author metrics (modifications `n`, frequency `η`, churn rate
  `ρ`, ownership `ω`) are derived with pure SQL from these tables

`POST /api/repos/{id}/compute` runs in a background thread and reports
progress through `/compute/status`; a restart mid-computation marks the job
`failed` so it can simply be re-run. It optionally accepts a JSON body
`{"reference_commit": "<rev>"}` (dashboard milestone): the revision is
resolved with `git rev-parse --verify <rev>^{commit}` — 400 if unknown —
stored, and the re-compute that follows restarts H̄ at that commit.

### Verifying correctness (git is the ground truth)

    scripts/toy_repo_test.sh                 # builds a synthetic repo (root add, pure
                                             # rename, rename+edit, deletion, binary)
                                             # and asserts the spec on the computed DB
    scripts/check_metrics.py 1 -n 50         # recomputes N random non-merge commits
                                             # (plus root/rename/deletion commits) from
                                             # live `git show --numstat -M50%` and
                                             # compares 1:1 against the DB
    scripts/check_metrics.py 1 --sha <sha>   # force-check a specific commit
    scripts/check_query.py 1 --sha <sha>     # asserts the query semantics: repository
                                             # == root dir, sum over authors/children,
                                             # [from, to) boundary, empty sets -> zeros,
                                             # merge/unmerge round-trip; cross-checks
                                             # list mode against git show

Run the checkers with the virtualenv interpreter
(`venv/bin/python scripts/check_query.py ...`) — they import the app package.

## Query API

All computed metrics are exposed read-only through SQL over the stored tables —
no git process runs at query time. Responses are stable, documented JSON with
numbers (never strings); rates are rounded to 6 decimals.

| Method | Path | Description |
|---|---|---|
| GET | `/api/repos/{id}/authors` | canonical identities (`commit_count`, `merged`, `group_id`, `member_ids`) plus the `merges` list |
| GET | `/api/repos/{id}/authors/merges` | persisted merge groups |
| POST | `/api/repos/{id}/authors/merge` | `{"aliases": [author_id, ...], "label"}` → one identity |
| DELETE | `/api/repos/{id}/authors/merges/{group_id}` | dissolve a group (unmerge) |
| GET | `/api/repos/{id}/commits` | commit picker over H̄: `page`, `per_page` (≤ 200), `q` (subject/sha), `author`, `from` (inclusive), `to` (exclusive); rows carry `sha, short_sha, author_label, committer_date, subject` |
| GET | `/api/repos/{id}/tree?path=` | immediate `dirs` + `files` of a path (the H[D]/H[F] navigator) |
| POST | `/api/repos/{id}/metrics/query` | the metric query (body below) |
| POST | `/api/repos/{id}/metrics/children` | immediate children of a directory/root with the same metric fields — dashboard extension, read-only (below) |

### Metric query

    POST /api/repos/{id}/metrics/query
    {
      "object":     {"type": "repository" | "directory" | "file", "path": "..."},
      "commit_set": {"mode": "all" | "range" | "list", "from": ..., "to": ..., "shas": [...]},
      "author":     <author id>          // optional
    }

- **Commit sets.** `all` = H̄; `range` = `[from, to)` on committer date (`from`
  inclusive, `to` exclusive); `list` = the given SHAs ∩ H̄. `|H|` counts only
  reachable non-merge commits. H̄ is materialised once into `hbar_members`
  (traversal of the stored parent links, falling back to the extracted set
  whenever chains pass through unstored merge commits) and cached until the
  reference commit or the computed data changes — changing
  `reference_commit` therefore requires a re-compute.
- **Response.** `scope.h_size`; `object_metrics` (`added`, `removed`, `growth`,
  `churn`, `modifications`, `frequency`, `churn_rate`); `authors` — per
  canonical identity: `modifications`, `churn`, `ownership` (share of the
  object's churn); `series` (`sha`, `date`, `added`, `removed` per commit) for
  charting. When a filter author is given, an extra `author_scope` block
  carries that author's own `h_size`, `metrics` and `series`: object metrics
  and the main series always cover the full H = commit_set ∩ H̄, while
  `author_scope` covers H filtered to the author's commits (`scope.note` says
  exactly this in every response). Filtering by the repository root `''` is
  identical to the repository object.
- **Zero division.** `|H| = 0` → `η = 0` and `ρ = 0`; total churn `λ = 0` →
  `ω = 0`. Empty commit sets return all zeros with HTTP 200 — never
  `null`/`NaN`/error.

### Child rows (dashboard extension)

    POST /api/repos/{id}/metrics/children
    {"object": ..., "commit_set": ..., "author": <author id, optional>}

Returns `children`: one row per immediate child of the object — `name`, `path`,
`type` (`directory` | `file`), `added`, `removed`, `growth`, `churn`,
`modifications`, `frequency`, `churn_rate` — sorted by churn. It is read-only
(the same aggregation as `/metrics/query` over the same H) and exists purely
so the directory table and the charts need one round-trip instead of one
query per child (160+ on large repositories). As with object metrics, the
rows always cover the full H = commit_set ∩ H̄; a given author is resolved and
echoed for parity but does not restrict the rows — author-relative numbers
come from `author_scope` on the main query. Added in the dashboard milestone;
the other endpoints in this table are unchanged.

### Author merging

`.mailmap` identities are applied during extraction; manual merges are
persisted (`author_groups` + `author_group_members`) and survive restarts.
A group acts as one identity everywhere: the authors list, `author` filters on
`/commits` and `/metrics/query`, per-author breakdowns and ownership. The
canonical `author_id` is the lowest member id; the group carries a `label`
(default: the members' names joined with `+`). Merging unions any groups the
listed authors already belong to; `DELETE` dissolves the whole group and
restores every member as its own identity.

### Performance

Queries are pure SQL over indexed tables (`idx_dir_changes_path`,
`idx_commits_repo_date`, covering indexes on `commits`/`hbar_members`); none of
them invokes git. Measured after compute on the redis repository (11,874
commits): repository-scoped queries ≈ 100–200 ms, file-scoped ≈ 20–40 ms — all
within the 300 ms budget.

## Data layout

    data/rat.db              SQLite (WAL) — repositories, computed metrics,
                             author-merge groups and the H̄ cache
    data/repos/<id>/src/     ingested repository (treated read-only by analysis)
    data/tmp/                scratch space for uploads/extraction (auto-cleaned)

`data/` and `venv/` are git-ignored. Nothing in `data/` is required at runtime;
a fresh checkout re-creates it on first start.

## Configuration

`app/config.py` holds paths and limits: 2 GiB upload cap, 4 GiB uncompressed
zip-bomb guard, git metadata timeout (60 s), clone timeout (1 h).

## Roadmap

- [x] Metric extraction & computation (file / directory / repository / commit set / author)
- [x] Filtering (repository, author, file/directory, commit set)
- [x] Author merging (`.mailmap` + manual)
- [x] Dashboard (views, filters, drill-down, charts, merge UI)

## AI usage declaration

An AI coding assistant (Qoder, integrated in the IDE) was used as a
pair-programming tool throughout the project, under the author's direction
and review:

- **Planning** — turning the assessment brief into the milestone plan
  (scaffold & ingestion → metric engine → query API → dashboard) and keeping
  the locked architecture consistent across milestones.
- **Implementation** — drafting and iterating on the FastAPI + SQLite
  backend (`app/`) and the vanilla-JS dashboard (`app/static/`). Every
  generated change was reviewed, edited and accepted by the author before
  commit.
- **Verification tooling** — writing the ground-truth checkers
  (`scripts/check_metrics.py`, `scripts/check_query.py`,
  `scripts/toy_repo_test.sh`) that recompute sampled commits from live
  `git log --numstat -M50%` output and compare them 1:1 with the database.
- **Testing** — driving browser click-throughs of the dashboard journey on
  the cJSON and redis repositories (console/network inspection) and fixing
  the issues found.

Every metric the tool reports is validated against git itself (the ground
truth) by the scripts above, independently of any AI-generated output.
