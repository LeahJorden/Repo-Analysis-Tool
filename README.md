# Repo Analysis Tool (RAT)

A web dashboard that ingests git repositories and analyses how they evolve —
per file, directory, repository, commit set and author.

> COMS3011A test project. **Milestone 3 (current codebase): repository
> ingestion, the metric computation engine and the query API — the four
> filter axes (repository, author, file/directory, commit set), H̄ and author
> merging.** Dashboard visualisations arrive in the final milestone.

## Quickstart

Requirements: Python 3.10+ and the `git` binary on the `PATH`.

    ./run.sh

Then open <http://localhost:8000> (API documentation is at `/docs`).

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

## API

| Method | Path | Description |
|---|---|---|
| POST | `/api/repos/upload` | multipart `file=` zip upload → `{repo_id, status}` |
| POST | `/api/repos/clone` | `{"url": ...}` → `{repo_id, status}` |
| GET | `/api/repos` | list all repositories |
| GET | `/api/repos/{id}` | fetch one repository |
| POST | `/api/repos/{id}/compute` | start (or restart) the metric computation → `{repo_id, state}` |
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
`failed` so it can simply be re-run.

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
- [ ] Dashboard visualisations

## AI usage declaration

*To be completed before submission — list the tools used and how they were
used.*
