#!/usr/bin/env python3
"""Metric-spec assertions for the toy fixture repository.

Run by scripts/toy_repo_test.sh after it builds the fixture. This script
computes the fixture's metrics into a throwaway database (RAT_DB_PATH must be
set) and asserts every spec rule the fixture exercises:

  * root commit diffed against the empty tree -> all additions
  * pure rename -> all increments zero, delta on the new path
  * rename + edit -> delta attributed to the new path only
  * deletion -> removals at the path, no additions
  * binary files flagged but never measured in lines
  * directory rows == recursive sums over immediate children (subdirs + files)

Usage: RAT_DB_PATH=/path/to/toy.db scripts/toy_check.py /path/to/toy_repo
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if ok:
        print(f"  [ OK ] {label}")
    else:
        FAILURES.append(label)
        print(f"  [FAIL] {label}" + (f" - {detail}" if detail else ""))


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    repo_path = Path(sys.argv[1]).resolve()
    if not (repo_path / ".git").exists():
        print(f"[FAIL] {repo_path} is not a git repository")
        return 2
    if not os.environ.get("RAT_DB_PATH"):
        print("RAT_DB_PATH must be set to a throwaway database path.")
        return 2

    from app import db, metrics

    db.init_db()
    head = git(repo_path, "rev-parse", "HEAD")
    shas = git(repo_path, "rev-list", "--reverse", "HEAD").splitlines()
    if len(shas) != 5:
        print(f"[FAIL] fixture must have exactly 5 commits, found {len(shas)}")
        return 2
    c1, c2, c3, c4, c5 = shas

    repo_id = db.create_repo("toy_fixture", "zip", "toy_repo_test.sh", "ready")
    db.update_repo(
        repo_id,
        status="ready",
        head_sha=head,
        reference_commit=head,
        default_branch="main",
    )
    processed = metrics.compute_repo(repo_id, src=repo_path)

    print(f"toy_check: repo #{repo_id}, head={head[:12]}, computed {processed} commits")
    print()

    with db.get_conn() as conn:
        # ------------------------------------------------------------
        # Shape of the computed data
        # ------------------------------------------------------------
        n_commits = conn.execute(
            "SELECT COUNT(*) AS n FROM commits WHERE repo_id = ?", (repo_id,)
        ).fetchone()["n"]
        check("commit count == 5", processed == 5 and n_commits == 5,
              f"processed={processed} db={n_commits}")

        job = db.get_compute_job(repo_id)
        check("compute job finished (state=done, 5/5)",
              job is not None and job["state"] == "done"
              and job["processed"] == 5 and job["total"] == 5,
              f"job={dict(job) if job else None}")

        def file_rows(sha):
            return {
                row["path"]: (row["added"], row["removed"],
                              row["renamed_from"], row["is_binary"])
                for row in conn.execute(
                    "SELECT path, added, removed, renamed_from, is_binary"
                    " FROM file_changes WHERE repo_id = ? AND commit_sha = ?",
                    (repo_id, sha),
                )
            }

        def dir_rows(sha):
            return {
                row["dir_path"]: (row["added"], row["removed"])
                for row in conn.execute(
                    "SELECT dir_path, added, removed FROM dir_changes"
                    " WHERE repo_id = ? AND commit_sha = ?",
                    (repo_id, sha),
                )
            }

        # ------------------------------------------------------------
        # Exact expected rows (added, removed, renamed_from, is_binary)
        # ------------------------------------------------------------
        expected_files = {
            c1: {"README.md": (3, 0, None, 0),
                 "src/a.txt": (2, 0, None, 0),
                 "assets/logo.png": (0, 0, None, 1)},
            c2: {"src/b.txt": (0, 0, "src/a.txt", 0)},
            c3: {"src/c.txt": (1, 0, "src/b.txt", 0)},
            c4: {"README.md": (0, 3, None, 0)},
            c5: {"assets/logo2.png": (0, 0, None, 1)},
        }
        expected_dirs = {
            c1: {"": (5, 0), "src": (2, 0), "assets": (0, 0)},
            c2: {"": (0, 0), "src": (0, 0)},
            c3: {"": (1, 0), "src": (1, 0)},
            c4: {"": (0, 3)},
            c5: {"": (0, 0), "assets": (0, 0)},
        }
        labels = {
            c1: "c1 root add", c2: "c2 pure rename", c3: "c3 rename+edit",
            c4: "c4 deletion", c5: "c5 binary add",
        }
        for sha in (c1, c2, c3, c4, c5):
            check(f"{labels[sha]}: file rows exact",
                  file_rows(sha) == expected_files[sha],
                  f"got {file_rows(sha)} want {expected_files[sha]}")
            check(f"{labels[sha]}: dir rows exact",
                  dir_rows(sha) == expected_dirs[sha],
                  f"got {dir_rows(sha)} want {expected_dirs[sha]}")

        # ------------------------------------------------------------
        # Named spec rules
        # ------------------------------------------------------------
        rows1, rows2, rows3, rows4 = file_rows(c1), file_rows(c2), file_rows(c3), file_rows(c4)
        check("root commit: all additions, no removals",
              sum(r[0] for r in rows1.values()) == 5
              and all(r[1] == 0 for r in rows1.values()),
              f"rows={rows1}")
        check("pure rename: all increments zero (delta = churn = 0)",
              all(r[0] == 0 and r[1] == 0 for r in rows2.values())
              and "src/b.txt" in rows2 and rows2["src/b.txt"][2] == "src/a.txt",
              f"rows={rows2}")
        check("rename+edit: delta only on the new path",
              set(rows3) == {"src/c.txt"} and rows3["src/c.txt"] == (1, 0, "src/b.txt", 0),
              f"rows={rows3}")
        check("deletion: removals only, at the path",
              set(rows4) == {"README.md"} and rows4["README.md"] == (0, 3, None, 0),
              f"rows={rows4}")
        binary_c1 = rows1.get("assets/logo.png") == (0, 0, None, 1)
        binary_c5 = file_rows(c5).get("assets/logo2.png") == (0, 0, None, 1)
        check("binary files: flagged, listed, zero lines", binary_c1 and binary_c5)

        polluted = conn.execute(
            "SELECT COUNT(*) AS n FROM file_changes WHERE repo_id = ? AND is_binary = 1"
            " AND (added <> 0 OR removed <> 0)", (repo_id,)
        ).fetchone()["n"]
        check("binary rows never contribute line counts", polluted == 0, f"{polluted} row(s)")

        # ------------------------------------------------------------
        # Parent chain and identities
        # ------------------------------------------------------------
        parents = {
            row["sha"]: row["parent_sha"]
            for row in conn.execute(
                "SELECT sha, parent_sha FROM commits WHERE repo_id = ?", (repo_id,)
            )
        }
        check("parent chain c5->c4->c3->c2->c1, root has none",
              parents == {c1: None, c2: c1, c3: c2, c4: c3, c5: c4},
              f"got { {k[:7]: (v or 'none')[:7] for k, v in parents.items()} }")

        author = conn.execute(
            "SELECT name, email FROM authors WHERE repo_id = ?", (repo_id,)
        ).fetchone()
        check("author identity captured",
              author is not None
              and (author["name"], author["email"]) == ("Toy Tester", "toy@example.com"),
              f"got {dict(author) if author else None}")

        # ------------------------------------------------------------
        # Repository totals and the immediate-children invariant (SQL)
        # ------------------------------------------------------------
        totals = {
            row["dir_path"]: (row["added"], row["removed"])
            for row in conn.execute(
                "SELECT dir_path, SUM(added) AS added, SUM(removed) AS removed"
                " FROM dir_changes WHERE repo_id = ? GROUP BY dir_path", (repo_id,)
            )
        }
        check("repo totals: root=(6,3), src=(3,0), assets=(0,0)",
              totals == {"": (6, 3), "src": (3, 0), "assets": (0, 0)},
              f"got {totals}")

        # For any directory d: lambda_d == sum over immediate children, where
        # children = immediate subdirectories + files directly inside d. The
        # sums are computed by SQLite; the parent path comes from the rtrim
        # trick (strip the path's non-slash characters from the right).
        all_dir_rows = {
            (row["commit_sha"], row["dir_path"]): (row["added"], row["removed"])
            for row in conn.execute(
                "SELECT commit_sha, dir_path, added, removed FROM dir_changes"
                " WHERE repo_id = ?", (repo_id,)
            )
        }
        subdir_sums = {
            (row["commit_sha"], row["parent"]): (row["added"], row["removed"])
            for row in conn.execute(
                "SELECT commit_sha,"
                " CASE WHEN dir_path = '' THEN NULL"
                " ELSE rtrim(rtrim(dir_path, replace(dir_path, '/', '')), '/') END"
                "   AS parent,"
                " SUM(added) AS added, SUM(removed) AS removed"
                " FROM dir_changes WHERE repo_id = ?"
                " GROUP BY commit_sha, parent", (repo_id,)
            ) if row["parent"] is not None
        }
        direct_sums = {
            (row["commit_sha"], row["parent"]): (row["added"], row["removed"])
            for row in conn.execute(
                "SELECT commit_sha,"
                " CASE WHEN instr(path, '/') = 0 THEN ''"
                " ELSE rtrim(rtrim(path, replace(path, '/', '')), '/') END"
                "   AS parent,"
                " SUM(added) AS added, SUM(removed) AS removed"
                " FROM file_changes WHERE repo_id = ?"
                " GROUP BY commit_sha, parent", (repo_id,)
            )
        }

        violations = []
        for (sha, directory), (added, removed) in all_dir_rows.items():
            child_sub = subdir_sums.get((sha, directory), (0, 0))
            child_files = direct_sums.get((sha, directory), (0, 0))
            expected_added = child_sub[0] + child_files[0]
            expected_removed = child_sub[1] + child_files[1]
            if (added, removed) != (expected_added, expected_removed):
                violations.append(
                    f"{sha[:7]} {directory!r}: dir={added}/{removed} "
                    f"children={expected_added}/{expected_removed}"
                )
        check("lambda_d = sum over immediate children (subdirs + direct files)",
              not violations, "; ".join(violations[:5]))

    print()
    if FAILURES:
        print(f"toy_check: {len(FAILURES)} of {CHECKS} assertions FAILED")
        return 1
    print(f"toy_check: all {CHECKS} assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
