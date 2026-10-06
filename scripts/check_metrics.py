#!/usr/bin/env python3
"""Validate the computed metrics of a repository against live git.

The checker re-derives, for a sample of non-merge commits, the per-file
added/removed numbers straight from ``git show --numstat -M50%`` and compares
them - together with the directory roll-ups - against the rows the metric
engine persisted in SQLite. The root commit as well as a few rename and
deletion commits are always included; the rest of the sample is random.

Usage:
    scripts/check_metrics.py REPO_ID [-n N] [--sha SHA]... [--seed SEED] [--db PATH]

The repository is looked up in the RAT database (RAT_DB_PATH or data/rat.db)
and read from data/repos/<id>/src.

Exit status: 0 = every checked commit matches git exactly, 1 = mismatches,
2 = usage or setup problem.
"""
from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

RENAME_CAP = 5    # always check up to this many rename commits
DELETION_CAP = 5  # always check up to this many deletion commits


def ancestor_dirs(path: str) -> list[str]:
    """Every directory containing ``path``, root first ('' = repository root).

    Deliberately an independent implementation of the engine's roll-up rule,
    so a bug in the engine cannot mask itself here.
    """
    parts = path.split("/")
    return [""] + ["/".join(parts[:index]) for index in range(1, len(parts))]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare computed metric rows against live git for a repository."
    )
    parser.add_argument("repo_id", type=int, help="repository id in the RAT database")
    parser.add_argument(
        "-n", "--sample", type=int, default=50,
        help="number of random commits to check in addition to the mandated ones (default 50)",
    )
    parser.add_argument(
        "--sha", action="append", default=[],
        help="always check this commit (repeatable; full sha or unambiguous prefix)",
    )
    parser.add_argument("--seed", type=int, default=0, help="random seed (default 0)")
    parser.add_argument(
        "--db", default=None,
        help="SQLite database path (default: RAT_DB_PATH env or data/rat.db)",
    )
    return parser.parse_args(argv)


def live_commit(src: Path, sha: str, metrics, ingest):
    """Read one commit the same way the engine does, but straight from git."""
    command = [
        "git", "-C", str(src), "show",
        "--use-mailmap", "--no-merges", "--root", "-M50%",
        "--numstat", "-z",
        f"--pretty=tformat:{metrics.LOG_FORMAT}",
        sha, "--",
    ]
    env = {**os.environ, **ingest.GIT_ENV}
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )
    records = list(metrics.iter_log_commits(process.stdout))
    stderr_text = process.stderr.read().decode("utf-8", "replace").strip()
    returncode = process.wait()
    if returncode != 0:
        raise RuntimeError(
            f"git show {sha[:12]} failed (exit {returncode}): {stderr_text[:300]}"
        )
    if len(records) != 1:
        raise RuntimeError(
            f"expected exactly one commit record from git show {sha[:12]}, got {len(records)}"
        )
    return records[0]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.db:
        os.environ["RAT_DB_PATH"] = args.db

    # Imported only now: app.config reads RAT_DB_PATH at import time.
    from app import config, db, ingest, metrics

    repo = db.get_repo(args.repo_id)
    if repo is None:
        print(f"[FAIL] repository {args.repo_id} does not exist in {config.DB_PATH}")
        return 2
    src = config.repo_src_dir(args.repo_id)
    if not (src / ".git").exists():
        print(f"[FAIL] repository working tree not found at {src}")
        return 2
    ref = repo["reference_commit"] or repo["head_sha"] or "HEAD"

    print(f"check_metrics: repo #{repo['id']} '{repo['name']}' (db={config.DB_PATH})")
    print(f"  src={src}")
    print(f"  ref={ref}")

    failures = 0

    live_total = metrics.count_commits(src, ref)
    with db.get_conn() as conn:
        db_total = conn.execute(
            "SELECT COUNT(*) AS n FROM commits WHERE repo_id = ?", (args.repo_id,)
        ).fetchone()["n"]
    if db_total == live_total:
        print(f"  [ OK ] commit count: db={db_total} live={live_total}")
    else:
        failures += 1
        print(f"[FAIL] commit count: db={db_total} live={live_total} (git rev-list --count)")

    # ------------------------------------------------------------------
    # Pick the sample: root commits + a few rename/deletion commits + random
    # ------------------------------------------------------------------
    with db.get_conn() as conn:
        commit_rows = conn.execute(
            "SELECT sha, parent_sha FROM commits WHERE repo_id = ?", (args.repo_id,)
        ).fetchall()
        rename_shas = sorted({
            row[0] for row in conn.execute(
                "SELECT DISTINCT commit_sha FROM file_changes"
                " WHERE repo_id = ? AND renamed_from IS NOT NULL",
                (args.repo_id,),
            )
        })
        deletion_shas = sorted({
            row[0] for row in conn.execute(
                "SELECT DISTINCT commit_sha FROM file_changes"
                " WHERE repo_id = ? AND added = 0 AND removed > 0",
                (args.repo_id,),
            )
        })

    sha_to_parent = {row["sha"]: row["parent_sha"] for row in commit_rows}
    if not sha_to_parent:
        print("[FAIL] no commits in the database - run a compute first")
        return 1

    rng = random.Random(args.seed)
    root_shas = sorted(sha for sha, parent in sha_to_parent.items() if parent is None)
    rename_pick = rng.sample(rename_shas, min(RENAME_CAP, len(rename_shas)))
    deletion_pick = rng.sample(deletion_shas, min(DELETION_CAP, len(deletion_shas)))

    explicit: list[str] = []
    for wanted in args.sha:
        matches = [
            sha for sha in sha_to_parent if sha == wanted or sha.startswith(wanted)
        ]
        if len(matches) != 1:
            print(f"[FAIL] --sha {wanted}: {'ambiguous' if matches else 'no such'} commit "
                  "in the database (non-merge commits only)")
            return 2
        explicit.append(matches[0])

    mandated = list(dict.fromkeys(root_shas + rename_pick + deletion_pick + explicit))
    rest = [sha for sha in sorted(sha_to_parent) if sha not in set(mandated)]
    random_pick = rng.sample(rest, min(max(args.sample, 0), len(rest)))
    selected = mandated + random_pick

    print(
        f"  checking {len(selected)} commits: {len(root_shas)} root, "
        f"{len(rename_pick)} rename, {len(deletion_pick)} deletion, "
        f"{len(explicit)} explicit, {len(random_pick)} random (seed={args.seed})"
    )
    print()

    # ------------------------------------------------------------------
    # Per-commit comparison against live git
    # ------------------------------------------------------------------
    for sha in selected:
        try:
            live = live_commit(src, sha, metrics, ingest)
        except Exception as exc:  # noqa: BLE001 - reported as a failed commit
            failures += 1
            print(f"[FAIL] {sha[:12]} could not be read from live git: {exc}")
            continue

        with db.get_conn() as conn:
            commit = conn.execute(
                "SELECT c.parent_sha, c.committer_date, c.subject, a.name, a.email"
                " FROM commits c JOIN authors a ON a.id = c.author_id"
                " WHERE c.repo_id = ? AND c.sha = ?",
                (args.repo_id, sha),
            ).fetchone()
            file_rows = conn.execute(
                "SELECT path, added, removed, renamed_from, is_binary FROM file_changes"
                " WHERE repo_id = ? AND commit_sha = ?",
                (args.repo_id, sha),
            ).fetchall()
            dir_rows = conn.execute(
                "SELECT dir_path, added, removed FROM dir_changes"
                " WHERE repo_id = ? AND commit_sha = ?",
                (args.repo_id, sha),
            ).fetchall()

        problems: list[str] = []
        if commit is None:
            problems.append("commit row missing from the database")
            commit = {"parent_sha": None, "committer_date": None, "subject": "",
                      "name": None, "email": None}

        live_parent = live.parents[0] if live.parents else None
        if commit["parent_sha"] != live_parent:
            problems.append(
                f"parent: db={(commit['parent_sha'] or 'none')[:12]} "
                f"live={(live_parent or 'none')[:12]}"
            )
        if commit["committer_date"] != live.committer_date:
            problems.append(
                f"committer_date: db={commit['committer_date']} live={live.committer_date}"
            )
        if (commit["name"], commit["email"]) != (live.author_name, live.author_email):
            problems.append(
                f"author: db={commit['name']!r}/{commit['email']!r} "
                f"live={live.author_name!r}/{live.author_email!r}"
            )

        db_files: dict[str, tuple] = {}
        for row in file_rows:
            if row["path"] in db_files:
                problems.append(f"duplicate file_changes row for {row['path']!r}")
            db_files[row["path"]] = (
                row["added"], row["removed"], row["renamed_from"], row["is_binary"],
            )
        live_files = {
            change.path: (
                change.added, change.removed, change.renamed_from,
                1 if change.is_binary else 0,
            )
            for change in live.files
        }
        for path in sorted(set(db_files) | set(live_files)):
            if path not in db_files:
                problems.append(
                    f"file_changes[{path!r}]: missing from DB (live={live_files[path]})"
                )
            elif path not in live_files:
                problems.append(
                    f"file_changes[{path!r}]: extra DB row (db={db_files[path]})"
                )
            elif db_files[path] != live_files[path]:
                problems.append(
                    f"file_changes[{path!r}]: db={db_files[path]} live={live_files[path]}"
                )

        expected_dirs: dict[str, list[int]] = {}
        for change in live.files:
            for directory in ancestor_dirs(change.path):
                acc = expected_dirs.setdefault(directory, [0, 0])
                acc[0] += change.added
                acc[1] += change.removed
        db_dirs = {row["dir_path"]: (row["added"], row["removed"]) for row in dir_rows}
        for directory in sorted(set(db_dirs) | set(expected_dirs)):
            actual = db_dirs.get(directory)
            expected = tuple(expected_dirs.get(directory, [0, 0]))
            if actual != expected:
                problems.append(
                    f"dir_changes[{directory!r}]: db={actual} live={expected}"
                )

        subject = (live.subject or "")[:48]
        if problems:
            failures += 1
            print(f"[FAIL] {sha[:12]} {subject}")
            for line in problems:
                print(f"       - {line}")
        else:
            added = sum(change.added for change in live.files)
            removed = sum(change.removed for change in live.files)
            print(
                f"[ OK ] {sha[:12]} files={len(live.files)} "
                f"+{added}/-{removed} {subject}"
            )

    # ------------------------------------------------------------------
    # Database-wide guards
    # ------------------------------------------------------------------
    with db.get_conn() as conn:
        binary_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM file_changes WHERE repo_id = ? AND is_binary = 1",
            (args.repo_id,),
        ).fetchone()["n"]
        polluted = conn.execute(
            "SELECT COUNT(*) AS n FROM file_changes WHERE repo_id = ? AND is_binary = 1"
            " AND (added <> 0 OR removed <> 0)",
            (args.repo_id,),
        ).fetchone()["n"]
    if polluted:
        failures += 1
        print(f"[FAIL] {polluted} binary row(s) carry non-zero line counts")
    else:
        print(f"  [ OK ] binary rows carry zero line counts ({binary_rows} flagged)")

    print()
    if failures:
        print(f"check_metrics: FAIL - {failures} mismatch(es) across "
              f"{len(selected)} checked commit(s)")
        return 1
    print(f"check_metrics: OK - sampled {len(selected)} commits, "
          "100% match against live git")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
