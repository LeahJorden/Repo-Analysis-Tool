#!/usr/bin/env python3
"""Validate the P3 query layer (filters, H-bar, author merging) against the DB.

Read-only with respect to the computed metric tables: this checker calls the
same functions the REST endpoints call (app/query.py) directly against the
SQLite database - no server required - and asserts every query-semantics rule:

  * repository query == root-directory query (metrics, |H|, series)
  * sum over authors  == object metrics (churn, modifications) and the
    ownership shares sum to 1
  * sum over the immediate children of a directory == the directory itself
  * boundary: [T, T+1) includes the commit with committer date T
    (|H| >= 1), while [T, T) is empty with all-zero metrics
  * empty commit sets (empty list / unknown sha / empty range) yield zeros,
    never an error
  * zero division: an object with churn 0 reports churn_rate 0 and ownership 0
  * the commit picker (list_commits) agrees with the authors list and paging
  * the tree endpoint agrees with an independently computed directory listing
  * author scoping: author_scope covers the author's commits while the object
    metrics stay on the full H
  * merge lifecycle (optional, default on): merging two authors collapses the
    identity everywhere - counts, filters, churn, ownership - and unmerging
    restores it exactly; the group is always removed again (try/finally)

With --sha, each named commit is additionally cross-checked against live git
(``git show --numstat -z -M50%`` like the engine): the repository query in
list mode must equal the git numbers summed, and every changed file must match
its per-file query 1:1.

Usage:
    scripts/check_query.py REPO_ID [-n SAMPLE] [--sha SHA]... [--no-merge-check]
                          [--seed SEED] [--db PATH] [--repo-dir DIR]

Exit status: 0 = every check passed, 1 = failures, 2 = usage/setup problem.
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

CHILD_SUM_CAP = 25   # run the children-sum invariant only for dirs with <= N children
OWNERSHIP_TOL = 1e-4

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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Assert the P3 query semantics against the RAT database."
    )
    parser.add_argument("repo_id", type=int, help="repository id in the RAT database")
    parser.add_argument("-n", "--sample", type=int, default=4,
                        help="objects sampled per class for the invariants (default 4)")
    parser.add_argument("--sha", action="append", default=[],
                        help="cross-check this commit in list mode against live git (repeatable)")
    parser.add_argument("--no-merge-check", action="store_true",
                        help="skip the merge/unmerge lifecycle check")
    parser.add_argument("--seed", type=int, default=0, help="random seed (default 0)")
    parser.add_argument("--db", default=None,
                        help="SQLite database path (default: RAT_DB_PATH env or data/rat.db)")
    parser.add_argument("--repo-dir", default=None,
                        help="repository working tree (default: data/repos/<id>/src)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.db:
        os.environ["RAT_DB_PATH"] = args.db

    # Imported only now: app.config reads RAT_DB_PATH at import time.
    try:
        from fastapi import HTTPException

        from app import config, db, metrics, query
    except ModuleNotFoundError as error:
        print(f"[FAIL] {error} - run this checker with the virtualenv interpreter:"
              f" {ROOT_DIR}/venv/bin/python scripts/check_query.py ...")
        return 2

    repo = db.get_repo(args.repo_id)
    if repo is None:
        print(f"[FAIL] repository {args.repo_id} does not exist in {config.DB_PATH}")
        return 2
    if not repo["computed_at"]:
        print(f"[FAIL] repository {args.repo_id} has no computed metrics - run a compute first")
        return 2

    hbar = query.ensure_hbar(args.repo_id)
    print(f"check_query: repo #{repo['id']} '{repo['name']}' (db={config.DB_PATH})")
    print(f"  H-bar: ref={str(hbar['ref_sha'])[:12]} members={hbar['member_count']}"
          f" bfs={hbar['bfs_count']} (rebuilt={hbar['rebuilt']})")

    rng = random.Random(args.seed)

    def q(obj_type, path=None, mode="all", author=None, **commit_kw):
        commit_set = query.QueryCommitSet(mode=mode, **commit_kw)
        request = query.MetricsQuery(
            object=query.QueryObject(type=obj_type, path=path),
            commit_set=commit_set, author=author,
        )
        return query.query_metrics(args.repo_id, request)

    with db.get_conn() as conn:
        stored_commits = conn.execute(
            "SELECT COUNT(*) AS n FROM commits WHERE repo_id = ?", (args.repo_id,)
        ).fetchone()["n"]
        hbar_members = conn.execute(
            "SELECT COUNT(*) AS n FROM hbar_members WHERE repo_id = ?", (args.repo_id,)
        ).fetchone()["n"]

    print(f"  stored commits={stored_commits} hbar_members={hbar_members}")
    check("|H| (repository/all) == hbar member count",
          hbar["member_count"] == hbar_members, f"{hbar['member_count']} vs {hbar_members}")
    if hbar["member_count"] != stored_commits:
        print(f"  [warn] |H| != stored commits - reference set differs from the"
              f" extracted set (reference_commit={str(repo['reference_commit'])[:12]})")

    # ------------------------------------------------------------------
    # Shape: authors list, commit picker, tree
    # ------------------------------------------------------------------
    print("== shapes: authors / commits / tree ==")
    authors = query.list_authors(args.repo_id)
    total_by_author = sum(entry["commit_count"] for entry in authors["authors"])
    check("authors: canonical commit counts sum to the stored commit count",
          total_by_author == stored_commits, f"{total_by_author} vs {stored_commits}")

    commits_page = query.list_commits(args.repo_id, page=1, per_page=5)
    check("commits: total == |H|", commits_page["total"] == hbar["member_count"],
          f"{commits_page['total']} vs {hbar['member_count']}")
    check("commits: page shape (sha, short_sha, author, date, subject)",
          all(set(("sha", "short_sha", "author_id", "author_label", "committer_date",
                   "subject")) <= set(commit) and commit["short_sha"] == commit["sha"][:10]
              for commit in commits_page["commits"]))
    beyond = query.list_commits(args.repo_id, page=commits_page["total"] // 5 + 2, per_page=5)
    check("commits: page beyond the end is empty, total unchanged",
          beyond["commits"] == [] and beyond["total"] == commits_page["total"])

    # Tree vs an independent level-1 listing built from file paths.
    with db.get_conn() as conn:
        paths = [row["path"] for row in conn.execute(
            "SELECT DISTINCT path FROM file_changes WHERE repo_id = ?", (args.repo_id,))]
    expected_dirs, expected_files = set(), set()
    for path in paths:
        if "/" in path:
            expected_dirs.add(path.split("/", 1)[0])
        else:
            expected_files.add(path)
    root_tree = query.tree_children(args.repo_id, "")
    check("tree root: dirs+files match an independent listing",
          {d["name"] for d in root_tree["dirs"]} == expected_dirs
          and {f["name"] for f in root_tree["files"]} == expected_files,
          f"dirs {len(root_tree['dirs'])} vs {len(expected_dirs)},"
          f" files {len(root_tree['files'])} vs {len(expected_files)}")

    # ------------------------------------------------------------------
    # Object sampling for the invariants
    # ------------------------------------------------------------------
    with db.get_conn() as conn:
        dir_rows = conn.execute(
            "SELECT dir_path, SUM(added + removed) AS churn FROM dir_changes"
            " WHERE repo_id = ? GROUP BY dir_path ORDER BY churn DESC",
            (args.repo_id,),
        ).fetchall()
        file_rows = conn.execute(
            "SELECT path, SUM(added + removed) AS churn FROM file_changes"
            " WHERE repo_id = ? GROUP BY path ORDER BY churn DESC",
            (args.repo_id,),
        ).fetchall()
        zero_churn = conn.execute(
            "SELECT path FROM file_changes WHERE repo_id = ?"
            " GROUP BY path HAVING SUM(added + removed) = 0 LIMIT 1",
            (args.repo_id,),
        ).fetchone()

    dir_candidates = [row["dir_path"] for row in dir_rows]
    file_candidates = [row["path"] for row in file_rows]
    dir_sample = [""] + rng.sample(
        [d for d in dir_candidates if d != ""], min(args.sample, len(dir_candidates) - 1))
    file_sample = rng.sample(file_candidates, min(args.sample, len(file_candidates)))

    # ------------------------------------------------------------------
    # Invariants: repository == root, sum over authors, children sum
    # ------------------------------------------------------------------
    print("== invariants ==")
    repo_q = q("repository")
    root_q = q("directory", "")
    check("repository query == root directory query",
          repo_q["object_metrics"] == root_q["object_metrics"]
          and repo_q["scope"]["h_size"] == root_q["scope"]["h_size"]
          and repo_q["series"] == root_q["series"])

    for label, result in [("repository", repo_q)] + \
            [(f"directory '{path}'", q("directory", path)) for path in dir_sample] + \
            [(f"file '{path}'", q("file", path)) for path in file_sample]:
        object_metrics = result["object_metrics"]
        churn_sum = sum(entry["churn"] for entry in result["authors"])
        mods_sum = sum(entry["modifications"] for entry in result["authors"])
        own_sum = sum(entry["ownership"] for entry in result["authors"])
        check(f"{label}: sum over authors == object metrics",
              churn_sum == object_metrics["churn"]
              and mods_sum == object_metrics["modifications"],
              f"churn {churn_sum} vs {object_metrics['churn']},"
              f" mods {mods_sum} vs {object_metrics['modifications']}")
        check(f"{label}: ownership sums to 1",
              object_metrics["churn"] == 0 or abs(own_sum - 1.0) <= OWNERSHIP_TOL,
              f"sum={own_sum}")
        check(f"{label}: series is chronological",
              all(result["series"][i]["date"] <= result["series"][i + 1]["date"]
                  for i in range(len(result["series"]) - 1)))

    checked_children = 0
    for path in dir_sample:
        tree = query.tree_children(args.repo_id, path)
        if len(tree["dirs"]) + len(tree["files"]) > CHILD_SUM_CAP:
            continue
        sum_added = sum_removed = 0
        for child in tree["dirs"]:
            metrics_child = q("directory", child["path"])["object_metrics"]
            sum_added += metrics_child["added"]
            sum_removed += metrics_child["removed"]
        for child in tree["files"]:
            metrics_child = q("file", child["path"])["object_metrics"]
            sum_added += metrics_child["added"]
            sum_removed += metrics_child["removed"]
        parent = q("directory", path)["object_metrics"]
        check(f"directory '{path}': sum of immediate children == parent"
              f" ({len(tree['dirs'])} dirs + {len(tree['files'])} files)",
              (sum_added, sum_removed) == (parent["added"], parent["removed"]),
              f"children=({sum_added},{sum_removed})"
              f" parent=({parent['added']},{parent['removed']})")
        checked_children += 1
    if checked_children == 0:
        print("  [warn] no sampled directory was small enough for the children-sum invariant")

    # ------------------------------------------------------------------
    # Commit-set semantics: boundary, empty sets, list intersection
    # ------------------------------------------------------------------
    print("== commit-set semantics ==")
    with db.get_conn() as conn:
        newest = conn.execute(
            "SELECT sha, committer_date FROM commits WHERE repo_id = ?"
            " ORDER BY committer_date DESC, sha LIMIT 1", (args.repo_id,)
        ).fetchone()
    boundary_sha, boundary_t = newest["sha"], newest["committer_date"]

    in_range = q("repository", mode="range", from_=boundary_t, to=boundary_t + 1)
    check("boundary: [T, T+1) includes the commit with committer date T",
          in_range["scope"]["h_size"] >= 1, f"|H|={in_range['scope']['h_size']}")
    as_list = q("repository", mode="list", shas=[boundary_sha])
    check("boundary: list[sha] |H| == 1 and values non-zero",
          as_list["scope"]["h_size"] == 1
          and as_list["object_metrics"]["churn"] > 0
          and len(as_list["series"]) >= 1,
          f"|H|={as_list['scope']['h_size']} churn={as_list['object_metrics']['churn']}")

    empty_range = q("repository", mode="range", from_=boundary_t, to=boundary_t)
    empty_list = q("repository", mode="list", shas=[])
    unknown_sha = q("repository", mode="list", shas=["0" * 40])
    for label, result in [("range [T, T)", empty_range), ("list []", empty_list),
                          ("list [unknown sha]", unknown_sha)]:
        object_metrics = result["object_metrics"]
        check(f"empty set ({label}): |H|=0, all metrics 0, no authors/series",
              result["scope"]["h_size"] == 0
              and all(value == 0 for value in object_metrics.values())
              and result["authors"] == [] and result["series"] == [],
              f"|H|={result['scope']['h_size']} metrics={object_metrics}")
        check(f"empty set ({label}): eta == rho == 0",
              object_metrics["frequency"] == 0 and object_metrics["churn_rate"] == 0)

    if zero_churn is not None:
        zero_q = q("file", zero_churn["path"])
        check(f"zero division: file '{zero_churn['path']}' (churn 0) -> rho=0, omega=0",
              zero_q["object_metrics"]["churn"] == 0
              and zero_q["object_metrics"]["churn_rate"] == 0
              and all(entry["ownership"] == 0 for entry in zero_q["authors"]))
    else:
        print("  [warn] no zero-churn file found for the zero-division check")

    # ------------------------------------------------------------------
    # Author scoping
    # ------------------------------------------------------------------
    print("== author scoping ==")
    if authors["authors"]:
        top = authors["authors"][0]
        scoped = q("repository", author=top["author_id"])
        check("author scope: author_h_size == author's H-bar commit count",
              scoped["scope"]["author_h_size"] == top["commit_count"],
              f"{scoped['scope']['author_h_size']} vs {top['commit_count']}")
        check("author scope: object metrics stay on full H",
              scoped["object_metrics"] == repo_q["object_metrics"]
              and scoped["scope"]["h_size"] == repo_q["scope"]["h_size"])
        member = top["member_ids"][-1]
        via_member = q("repository", author=member)
        check("author scope: any group member resolves to the canonical identity",
              via_member["scope"]["author_id"] == top["author_id"]
              and via_member["author_scope"]["h_size"] == scoped["author_scope"]["h_size"])

    # ------------------------------------------------------------------
    # Merge lifecycle (always unmerged again, even on failure)
    # ------------------------------------------------------------------
    if not args.no_merge_check:
        print("== merge lifecycle ==")
        unmerged = [entry for entry in authors["authors"] if not entry["merged"]]
        if len(unmerged) >= 2:
            pick = sorted([unmerged[-1], unmerged[-2]], key=lambda e: e["author_id"])
            a1, a2 = pick[0], pick[1]
            baseline_count = len(authors["authors"])
            pre1 = q("repository", author=a1["author_id"])
            pre2 = q("repository", author=a2["author_id"])
            churn1 = pre1["author_scope"]["metrics"]["churn"]
            churn2 = pre2["author_scope"]["metrics"]["churn"]
            own1 = next(e for e in pre1["authors"]
                        if e["author_id"] == a1["author_id"])["ownership"]
            own2 = next(e for e in pre2["authors"]
                        if e["author_id"] == a2["author_id"])["ownership"]
            created_gid = None
            try:
                created = query.create_merge(args.repo_id, [a1["author_id"], a2["author_id"]],
                                             "check_query temporary merge")
                created_gid = created["merge"]["group_id"]
                merged = query.list_authors(args.repo_id)
                collapsed = [entry for entry in merged["authors"]
                             if entry["group_id"] == created_gid]
                check("merge: authors collapse by one identity",
                      len(merged["authors"]) == baseline_count - 1, f"{len(merged['authors'])}")
                check("merge: canonical entry carries both members and counts",
                      len(collapsed) == 1
                      and collapsed[0]["author_id"] == min(a1["author_id"], a2["author_id"])
                      and collapsed[0]["member_ids"] == sorted([a1["author_id"], a2["author_id"]])
                      and collapsed[0]["commit_count"] == a1["commit_count"] + a2["commit_count"],
                      f"{collapsed}")
                page = query.list_commits(args.repo_id, page=1, per_page=1,
                                          author_id=a1["author_id"])
                check("merge: commit filter total == c1 + c2",
                      page["total"] == a1["commit_count"] + a2["commit_count"],
                      f"{page['total']}")
                scoped = q("repository", author=a2["author_id"])
                check("merge: author scope combines both members",
                      scoped["scope"]["author_h_size"]
                      == a1["commit_count"] + a2["commit_count"],
                      f"{scoped['scope']['author_h_size']}")
                check("merge: author scope churn == churn1 + churn2",
                      scoped["author_scope"]["metrics"]["churn"] == churn1 + churn2,
                      f"{scoped['author_scope']['metrics']['churn']} vs {churn1 + churn2}")
                merged_entry = next(entry for entry in scoped["authors"]
                                    if entry["group_id"] == created_gid)
                check("merge: breakdown entry churn == churn1 + churn2",
                      merged_entry["churn"] == churn1 + churn2,
                      f"{merged_entry['churn']} vs {churn1 + churn2}")
                check("merge: breakdown ownership == own1 + own2",
                      abs(merged_entry["ownership"] - (own1 + own2)) <= OWNERSHIP_TOL,
                      f"{merged_entry['ownership']} vs {own1 + own2}")
            finally:
                if created_gid is not None:
                    query.delete_merge(args.repo_id, created_gid)
            restored = query.list_authors(args.repo_id)
            check("unmerge: identity count restored",
                  len(restored["authors"]) == baseline_count, f"{len(restored['authors'])}")
            check("unmerge: no groups remain from the check",
                  all(entry["group_id"] != created_gid for entry in restored["authors"]))
            again = q("repository", author=a1["author_id"])
            again_entry = next(e for e in again["authors"]
                               if e["author_id"] == a1["author_id"])
            check("unmerge: churn and ownership restored exactly",
                  again["author_scope"]["metrics"]["churn"] == churn1
                  and abs(again_entry["ownership"] - own1) <= OWNERSHIP_TOL,
                  f"churn {again['author_scope']['metrics']['churn']} vs {churn1}")
        else:
            print("  [warn] fewer than two unmerged authors - lifecycle check skipped")

    # ------------------------------------------------------------------
    # Error contracts
    # ------------------------------------------------------------------
    print("== error contracts ==")

    def expect_http(code: int, fn) -> tuple[bool, str]:
        try:
            fn()
            return False, "no error raised"
        except HTTPException as error:
            return error.status_code == code, f"got {error.status_code}"

    ok, detail = expect_http(400, lambda: q("file"))
    check("file without path -> 400", ok, detail)
    ok, detail = expect_http(400, lambda: q("repository", path="x"))
    check("repository with path -> 400", ok, detail)
    ok, detail = expect_http(400, lambda: q("directory", "../etc"))
    check("path traversal -> 400", ok, detail)
    ok, detail = expect_http(400, lambda: q("repository", author=2_000_000_000))
    check("unknown author -> 400", ok, detail)
    ok, detail = expect_http(400, lambda: query.query_metrics(args.repo_id,
          query.MetricsQuery(object=query.QueryObject(type="repository"),
                             commit_set=query.QueryCommitSet(mode="list",
                                                             shas=[f"{i:040x}" for i in range(2001)]))))
    check("oversized commit list -> 400", ok, detail)

    # ------------------------------------------------------------------
    # Optional: cross-check list mode against live git
    # ------------------------------------------------------------------
    src = Path(args.repo_dir) if args.repo_dir else config.repo_src_dir(args.repo_id)
    if args.sha:
        print("== list mode vs live git ==")
        if not (src / ".git").exists():
            print(f"  [warn] no working tree at {src} - skipping the git cross-checks")
        else:
            from app import ingest
            for sha_arg in args.sha:
                resolved = subprocess.run(
                    ["git", "-C", str(src), "rev-parse", sha_arg],
                    capture_output=True, text=True).stdout.strip()
                with db.get_conn() as conn:
                    stored = conn.execute(
                        "SELECT 1 FROM commits WHERE repo_id = ? AND sha = ?",
                        (args.repo_id, resolved)).fetchone()
                if not stored:
                    print(f"  [warn] {sha_arg[:12]} is not a stored non-merge commit - skipped")
                    continue
                process = subprocess.Popen(
                    ["git", "-C", str(src), "show", "--use-mailmap", "--no-merges",
                     "--root", "-M50%", "--numstat", "-z",
                     f"--pretty=tformat:{metrics.LOG_FORMAT}", resolved, "--"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env={**os.environ, **ingest.GIT_ENV})
                record = next(metrics.iter_log_commits(process.stdout))
                process.wait()
                live = {change.path: (change.added, change.removed)
                        for change in record.files}
                live_added = sum(a for a, _ in live.values())
                live_removed = sum(r for _, r in live.values())

                result = q("repository", mode="list", shas=[resolved])
                object_metrics = result["object_metrics"]
                check(f"{resolved[:12]}: list mode == git show summed "
                      f"(+{live_added}/-{live_removed}, {len(live)} files)",
                      (object_metrics["added"], object_metrics["removed"])
                      == (live_added, live_removed)
                      and result["scope"]["h_size"] == 1
                      and len(result["series"]) == 1,
                      f"api +{object_metrics['added']}/-{object_metrics['removed']}"
                      f" |H|={result['scope']['h_size']}")

                per_file_problems = 0
                for path, (added, removed) in sorted(live.items()):
                    file_metrics = q("file", path, mode="list", shas=[resolved])["object_metrics"]
                    if (file_metrics["added"], file_metrics["removed"]) != (added, removed):
                        per_file_problems += 1
                        print(f"       - file '{path}': api"
                              f" +{file_metrics['added']}/-{file_metrics['removed']}"
                              f" git +{added}/-{removed}")
                check(f"{resolved[:12]}: per-file rows match git 1:1 ({len(live)} files)",
                      per_file_problems == 0, f"{per_file_problems} mismatch(es)")

    print()
    if FAILURES:
        print(f"check_query: FAIL - {len(FAILURES)} of {CHECKS} checks failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"check_query: OK - {CHECKS} checks passed, all query invariants hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
