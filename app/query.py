"""Query layer (P3): filters, H-bar materialisation and author merging.

Everything here is read-only SQL over the tables the metric engine persisted
at compute time - zero git invocations at query time. Three concerns live in
this module:

* H-bar: the set of non-merge commits reachable from the repository's
  reference commit. It is materialised once into ``hbar_members`` (membership
  is the in_hbar flag) and reused until the reference commit or the computed
  data changes. The traversal walks the stored parent links (BFS); because
  merge commits are deliberately not stored by the engine, chains that pass
  through a merge are invisible to the BFS. Whenever that happens the stored
  set itself is authoritative: the engine extracted it with
  ``git log <reference_commit> --no-merges``, so it is exactly H-bar by git's
  own reachability. (The cache is invalidated whenever the reference commit
  changes, so a future re-reference must recompute - see README.)

* Author identity: raw ``authors`` rows plus persisted merge groups. A merge
  group collapses its members into one canonical identity everywhere
  (author list, filters, breakdowns, ownership). Groups survive restarts.

* Commit-set semantics: "all" = H-bar; "range" = [from, to) on committer
  date (from inclusive, to exclusive); "list" = the given SHAs intersected
  with H-bar. Empty commit sets yield zeros, never null/NaN.
"""
from __future__ import annotations

import threading
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import db

MAX_LIST_SHAS = 2000          # hard cap on manual commit lists
MAX_PER_PAGE = 200            # pagination cap for the commit picker
SHORT_SHA_LEN = 10

_HBAR_LOCK = threading.Lock()  # one rebuild at a time (uvicorn runs sync endpoints in threads)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class QueryObject(BaseModel):
    type: Literal["repository", "directory", "file"]
    path: str | None = None


class QueryCommitSet(BaseModel):
    mode: Literal["all", "range", "list"]
    from_: int | None = Field(default=None, alias="from")
    to: int | None = None
    shas: list[str] | None = None

    model_config = ConfigDict(populate_by_name=True)


class MetricsQuery(BaseModel):
    object: QueryObject
    commit_set: QueryCommitSet
    author: int | None = None


class MergeRequest(BaseModel):
    aliases: list[int]
    label: str | None = None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _round(value: float, digits: int = 6) -> float:
    return round(value, digits)


def _safe_rate(numerator: int, denominator: int) -> float:
    """Zero-division guard: 0 when the denominator is 0, never NaN/null."""
    if not denominator:
        return 0.0
    return _round(numerator / denominator)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_path(raw: str | None, *, allow_root: bool) -> str:
    """Normalise an object path: '' = repository root, never absolute or '..'."""
    path = (raw or "").strip().strip("/")
    if path in ("", "."):
        if allow_root:
            return ""
        raise HTTPException(status_code=400, detail="A path is required for this object type.")
    if ".." in path.split("/"):
        raise HTTPException(status_code=400, detail="Path segments may not be '..'.")
    return path


def _placeholders(count: int) -> str:
    return ",".join("?" * count)


# ---------------------------------------------------------------------------
# Canonical author identities (raw authors + merge groups)
# ---------------------------------------------------------------------------


def _identity_context(conn, repo_id: int) -> dict:
    """Load identities, groups and per-author commit counts in one pass."""
    authors = {
        row["id"]: dict(row)
        for row in conn.execute(
            "SELECT id, name, email FROM authors WHERE repo_id = ? ORDER BY id", (repo_id,)
        )
    }
    groups = {
        row["id"]: {"group_id": row["id"], "label": row["label"], "created_at": row["created_at"],
                    "member_ids": []}
        for row in conn.execute(
            "SELECT id, label, created_at FROM author_groups WHERE repo_id = ? ORDER BY id",
            (repo_id,),
        )
    }
    author_to_group: dict[int, int] = {}
    for row in conn.execute(
        "SELECT author_id, group_id FROM author_group_members WHERE repo_id = ?", (repo_id,)
    ):
        author_to_group[row["author_id"]] = row["group_id"]
        if row["group_id"] in groups:
            groups[row["group_id"]]["member_ids"].append(row["author_id"])
    counts = {
        row["author_id"]: row["n"]
        for row in conn.execute(
            "SELECT author_id, COUNT(*) AS n FROM commits WHERE repo_id = ? GROUP BY author_id",
            (repo_id,),
        )
    }
    return {"authors": authors, "groups": groups, "author_to_group": author_to_group,
            "counts": counts}


def _canonical(ctx: dict, author_id: int) -> dict:
    """Canonical descriptor for a raw author id.

    Returns {author_id, group_id, label, merged, member_ids, names, emails}.
    The canonical ``author_id`` is the lowest member id of the identity.
    """
    gid = ctx["author_to_group"].get(author_id)
    if gid is not None and gid in ctx["groups"]:
        group = ctx["groups"][gid]
        member_ids = sorted(group["member_ids"])
        names, emails = [], []
        for mid in member_ids:
            info = ctx["authors"].get(mid)
            if info is not None:
                if info["name"] not in names:
                    names.append(info["name"])
                if info["email"] not in emails:
                    emails.append(info["email"])
        return {"author_id": member_ids[0], "group_id": gid, "label": group["label"],
                "merged": True, "member_ids": member_ids, "names": names, "emails": emails}
    info = ctx["authors"].get(author_id)
    if info is None:
        return {"author_id": author_id, "group_id": None, "label": f"author {author_id}",
                "merged": False, "member_ids": [author_id], "names": [], "emails": []}
    return {"author_id": author_id, "group_id": None,
            "label": f"{info['name']} <{info['email']}>", "merged": False,
            "member_ids": [author_id], "names": [info["name"]], "emails": [info["email"]]}


def _resolve_author(repo_id: int, author_id: int) -> dict:
    """Resolve a filter author id (any group member) to its canonical identity."""
    with db.get_conn() as conn:
        ctx = _identity_context(conn, repo_id)
    if author_id not in ctx["authors"]:
        raise HTTPException(
            status_code=400,
            detail=f"Author {author_id} does not belong to repository {repo_id}.",
        )
    return _canonical(ctx, author_id)


def list_authors(repo_id: int) -> dict:
    """Canonical identities with commit counts, plus the merge groups."""
    with db.get_conn() as conn:
        ctx = _identity_context(conn, repo_id)

    seen: set[int] = set()
    entries: list[dict] = []
    for author_id in ctx["authors"]:
        canonical = _canonical(ctx, author_id)
        rep = canonical["author_id"]
        if rep in seen:
            continue
        seen.add(rep)
        commit_count = sum(ctx["counts"].get(mid, 0) for mid in canonical["member_ids"])
        entries.append({
            "author_id": rep,
            "label": canonical["label"],
            "names": canonical["names"],
            "emails": canonical["emails"],
            "commit_count": commit_count,
            "merged": canonical["merged"],
            "group_id": canonical["group_id"],
            "member_ids": canonical["member_ids"],
        })
    entries.sort(key=lambda entry: (-entry["commit_count"], entry["label"]))

    return {"repo_id": repo_id, "authors": entries, "merges": _merges_payload(ctx)}


def _merges_payload(ctx: dict) -> list[dict]:
    merges = []
    for gid in sorted(ctx["groups"]):
        group = ctx["groups"][gid]
        members = []
        for mid in sorted(group["member_ids"]):
            info = ctx["authors"].get(mid)
            members.append({
                "author_id": mid,
                "name": info["name"] if info else None,
                "email": info["email"] if info else None,
                "commit_count": ctx["counts"].get(mid, 0),
            })
        merges.append({
            "group_id": gid,
            "label": group["label"],
            "created_at": group["created_at"],
            "member_ids": sorted(group["member_ids"]),
            "members": members,
            "commit_count": sum(ctx["counts"].get(mid, 0) for mid in group["member_ids"]),
        })
    return merges


def list_merges(repo_id: int) -> dict:
    with db.get_conn() as conn:
        ctx = _identity_context(conn, repo_id)
    return {"repo_id": repo_id, "merges": _merges_payload(ctx)}


def create_merge(repo_id: int, aliases: list[int], label: str | None) -> dict:
    """Create or extend a merge group; every listed author joins one identity."""
    if not aliases:
        raise HTTPException(status_code=400, detail="'aliases' must list at least two author ids.")
    unique = list(dict.fromkeys(int(alias) for alias in aliases))
    if len(unique) < 2:
        raise HTTPException(status_code=400, detail="A merge needs at least two distinct author ids.")

    with db.get_conn() as conn:
        ctx = _identity_context(conn, repo_id)
        missing = [alias for alias in unique if alias not in ctx["authors"]]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Authors {missing} do not belong to repository {repo_id}.",
            )

        touched = {ctx["author_to_group"][alias] for alias in unique if alias in ctx["author_to_group"]}
        if touched:
            target_gid = min(touched)
            if len(touched) > 1:
                placeholders = _placeholders(len(touched))
                conn.execute(
                    f"UPDATE author_group_members SET group_id = ?"
                    f" WHERE repo_id = ? AND group_id IN ({placeholders})",
                    (target_gid, repo_id, *sorted(touched)),
                )
                conn.execute(
                    f"DELETE FROM author_groups WHERE repo_id = ? AND id IN ({placeholders})"
                    f" AND id != ?",
                    (repo_id, *sorted(touched), target_gid),
                )
        else:
            cursor = conn.execute(
                "INSERT INTO author_groups (repo_id, label, created_at) VALUES (?, ?, ?)",
                (repo_id, "pending", db.utc_now_iso()),
            )
            target_gid = int(cursor.lastrowid)

        conn.executemany(
            "INSERT OR REPLACE INTO author_group_members (repo_id, author_id, group_id)"
            " VALUES (?, ?, ?)",
            [(repo_id, alias, target_gid) for alias in unique],
        )

        group = conn.execute(
            "SELECT id, label, created_at FROM author_groups WHERE id = ?", (target_gid,)
        ).fetchone()
        member_ids = sorted(
            row["author_id"]
            for row in conn.execute(
                "SELECT author_id FROM author_group_members WHERE repo_id = ? AND group_id = ?",
                (repo_id, target_gid),
            )
        )
        final_label = (label or "").strip()
        if not final_label:
            names = []
            for mid in member_ids:
                info = ctx["authors"].get(mid)
                if info is not None and info["name"] not in names:
                    names.append(info["name"])
            final_label = " + ".join(names) or f"merge {target_gid}"
        final_label = final_label[:120]
        conn.execute(
            "UPDATE author_groups SET label = ? WHERE id = ?", (final_label, target_gid)
        )

        ctx = _identity_context(conn, repo_id)
        payload = next(entry for entry in _merges_payload(ctx) if entry["group_id"] == target_gid)

    return {"repo_id": repo_id, "merge": payload}


def delete_merge(repo_id: int, group_id: int) -> dict:
    """Unmerge: dissolve a group and restore every member as its own identity."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM author_groups WHERE repo_id = ? AND id = ?", (repo_id, group_id)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Merge group not found.")
        member_ids = [
            r["author_id"]
            for r in conn.execute(
                "SELECT author_id FROM author_group_members WHERE repo_id = ? AND group_id = ?",
                (repo_id, group_id),
            )
        ]
        conn.execute(
            "DELETE FROM author_group_members WHERE repo_id = ? AND group_id = ?",
            (repo_id, group_id),
        )
        conn.execute(
            "DELETE FROM author_groups WHERE repo_id = ? AND id = ?", (repo_id, group_id)
        )
    return {"repo_id": repo_id, "removed_group_id": group_id,
            "unmerged_author_ids": sorted(member_ids)}


# ---------------------------------------------------------------------------
# H-bar (reachable non-merge commits from the reference commit)
# ---------------------------------------------------------------------------


def ensure_hbar(repo_id: int) -> dict:
    """Materialise H-bar once per (reference commit, computed data) state."""
    with _HBAR_LOCK:
        repo = db.get_repo(repo_id)
        if repo is None or not repo["computed_at"]:
            return {"ref_sha": None, "member_count": 0, "bfs_count": 0, "rebuilt": False}
        ref = repo["reference_commit"] or repo["head_sha"]
        if not ref:
            return {"ref_sha": None, "member_count": 0, "bfs_count": 0, "rebuilt": False}

        with db.get_conn() as conn:
            cache = conn.execute(
                "SELECT * FROM hbar_cache WHERE repo_id = ?", (repo_id,)
            ).fetchone()
            if (
                cache is not None
                and cache["ref_sha"] == ref
                and cache["computed_at"] >= repo["computed_at"]
            ):
                return {"ref_sha": ref, "member_count": cache["member_count"],
                        "bfs_count": cache["bfs_count"], "rebuilt": False}

            parents = {
                row["sha"]: row["parent_sha"]
                for row in conn.execute(
                    "SELECT sha, parent_sha FROM commits WHERE repo_id = ?", (repo_id,)
                )
            }
            # BFS over stored parent links; only edges inside the stored set
            # can be followed (merge commits are intentionally not stored).
            reachable: set[str] = set()
            stack = [ref] if ref in parents else []
            while stack:
                sha = stack.pop()
                if sha in reachable:
                    continue
                reachable.add(sha)
                parent = parents.get(sha)
                if parent and parent in parents:
                    stack.append(parent)
            bfs_count = len(reachable)
            # Whenever the BFS cannot cover the stored set, chains run through
            # unstored merge parents. The stored set itself is then the exact
            # H-bar: the engine extracted it with `git log <ref> --no-merges`.
            members = sorted(parents) if bfs_count != len(parents) else sorted(reachable)

            conn.execute("DELETE FROM hbar_members WHERE repo_id = ?", (repo_id,))
            conn.executemany(
                "INSERT INTO hbar_members (repo_id, sha) VALUES (?, ?)",
                [(repo_id, sha) for sha in members],
            )
            conn.execute(
                "INSERT INTO hbar_cache (repo_id, ref_sha, computed_at, member_count, bfs_count)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (repo_id) DO UPDATE SET ref_sha = excluded.ref_sha,"
                " computed_at = excluded.computed_at, member_count = excluded.member_count,"
                " bfs_count = excluded.bfs_count",
                (repo_id, ref, db.utc_now_iso(), len(members), bfs_count),
            )
        return {"ref_sha": ref, "member_count": len(members), "bfs_count": bfs_count,
                "rebuilt": True}


# ---------------------------------------------------------------------------
# Commit-set CTE shared by every aggregate
# ---------------------------------------------------------------------------


def _h_cte(repo_id: int, commit_set: QueryCommitSet, member_ids: list[int] | None = None) -> tuple[str, list]:
    """A ``WITH h AS (...)`` clause selecting commit_set ∩ H-bar (∩ author)."""
    conditions = ["c.repo_id = ?", "m.repo_id = c.repo_id", "m.sha = c.sha"]
    params: list = [repo_id]
    if commit_set.mode == "range":
        if commit_set.from_ is not None:
            conditions.append("c.committer_date >= ?")
            params.append(commit_set.from_)
        if commit_set.to is not None:
            conditions.append("c.committer_date < ?")  # 'to' exclusive per spec
            params.append(commit_set.to)
    elif commit_set.mode == "list":
        shas = list(dict.fromkeys(commit_set.shas or []))
        if len(shas) > MAX_LIST_SHAS:
            raise HTTPException(
                status_code=400,
                detail=f"A manual commit list may hold at most {MAX_LIST_SHAS} SHAs.",
            )
        if not shas:
            conditions.append("0")  # empty list -> empty H, zeros
        else:
            conditions.append(f"c.sha IN ({_placeholders(len(shas))})")
            params.extend(shas)
    if member_ids:
        conditions.append(f"c.author_id IN ({_placeholders(len(member_ids))})")
        params.extend(member_ids)
    sql = (
        "WITH h AS (SELECT c.sha, c.committer_date, c.author_id FROM commits c"
        " JOIN hbar_members m ON m.repo_id = c.repo_id AND m.sha = c.sha"
        f" WHERE {' AND '.join(conditions)})"
    )
    return sql, params


# ---------------------------------------------------------------------------
# GET /api/repos/{id}/commits - manual commit picker
# ---------------------------------------------------------------------------


def list_commits(
    repo_id: int,
    page: int = 1,
    per_page: int = 50,
    q: str | None = None,
    author_id: int | None = None,
    date_from: int | None = None,
    date_to: int | None = None,
) -> dict:
    ensure_hbar(repo_id)
    page = max(1, int(page))
    per_page = min(max(1, int(per_page)), MAX_PER_PAGE)

    where = ["c.repo_id = ?", "m.repo_id = c.repo_id", "m.sha = c.sha"]
    params: list = [repo_id]
    if q:
        pattern = f"%{_escape_like(q)}%"
        where.append("(c.subject LIKE ? ESCAPE '\\' OR c.sha LIKE ? ESCAPE '\\')")
        params.extend([pattern, f"{_escape_like(q)}%"])
    canonical_filter = None
    if author_id is not None:
        canonical_filter = _resolve_author(repo_id, author_id)
        member_ids = canonical_filter["member_ids"]
        where.append(f"c.author_id IN ({_placeholders(len(member_ids))})")
        params.extend(member_ids)
    if date_from is not None:
        where.append("c.committer_date >= ?")
        params.append(date_from)
    if date_to is not None:
        where.append("c.committer_date < ?")  # 'to' exclusive per spec
        params.append(date_to)
    where_sql = " AND ".join(where)

    with db.get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM commits c"
            f" JOIN hbar_members m ON m.repo_id = c.repo_id AND m.sha = c.sha"
            f" WHERE {where_sql}",
            params,
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT c.sha, c.committer_date, c.subject, c.author_id, a.name, a.email"
            f" FROM commits c"
            f" JOIN hbar_members m ON m.repo_id = c.repo_id AND m.sha = c.sha"
            f" JOIN authors a ON a.id = c.author_id"
            f" WHERE {where_sql}"
            f" ORDER BY c.committer_date DESC, c.sha"
            f" LIMIT ? OFFSET ?",
            [*params, per_page, (page - 1) * per_page],
        ).fetchall()
        ctx = _identity_context(conn, repo_id)

    commits = []
    for row in rows:
        canonical = _canonical(ctx, row["author_id"])
        commits.append({
            "sha": row["sha"],
            "short_sha": row["sha"][:SHORT_SHA_LEN],
            "author_id": canonical["author_id"],
            "author_label": canonical["label"],
            "author_name": row["name"],
            "author_email": row["email"],
            "merged": canonical["merged"],
            "group_id": canonical["group_id"],
            "committer_date": row["committer_date"],
            "subject": row["subject"] or "",
        })
    return {
        "repo_id": repo_id,
        "page": page,
        "per_page": per_page,
        "total": total,
        "author_filter": (None if canonical_filter is None else {
            "author_id": canonical_filter["author_id"],
            "label": canonical_filter["label"],
            "merged": canonical_filter["merged"],
            "member_ids": canonical_filter["member_ids"],
        }),
        "commits": commits,
    }


# ---------------------------------------------------------------------------
# GET /api/repos/{id}/tree - navigator over H[D] / H[F]
# ---------------------------------------------------------------------------


def tree_children(repo_id: int, path: str = "") -> dict:
    ensure_hbar(repo_id)
    normalized = (path or "").strip().strip("/")
    if ".." in normalized.split("/"):
        raise HTTPException(status_code=400, detail="Path must be relative and may not contain '..'.")

    with db.get_conn() as conn:
        exists = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM dir_changes WHERE repo_id = ? AND dir_path = ?)"
            " AS e",
            (repo_id, normalized),
        ).fetchone()["e"]
        if normalized == "":
            dir_rows = conn.execute(
                "SELECT DISTINCT dir_path FROM dir_changes"
                " WHERE repo_id = ? AND instr(dir_path, '/') = 0 AND dir_path != ''",
                (repo_id,),
            ).fetchall()
            file_rows = conn.execute(
                "SELECT DISTINCT path FROM file_changes"
                " WHERE repo_id = ? AND instr(path, '/') = 0",
                (repo_id,),
            ).fetchall()
        else:
            pattern = f"{_escape_like(normalized)}/%"
            tail = len(normalized) + 2  # position right after "path/"
            dir_rows = conn.execute(
                "SELECT DISTINCT dir_path FROM dir_changes"
                " WHERE repo_id = ? AND dir_path LIKE ? ESCAPE '\\'"
                " AND instr(substr(dir_path, ?), '/') = 0",
                (repo_id, pattern, tail),
            ).fetchall()
            file_rows = conn.execute(
                "SELECT DISTINCT path FROM file_changes"
                " WHERE repo_id = ? AND path LIKE ? ESCAPE '\\'"
                " AND instr(substr(path, ?), '/') = 0",
                (repo_id, pattern, tail),
            ).fetchall()

    dirs = sorted(
        ({"name": row["dir_path"].rsplit("/", 1)[-1], "path": row["dir_path"]}
         for row in dir_rows),
        key=lambda entry: entry["name"],
    )
    files = sorted(
        ({"name": row["path"].rsplit("/", 1)[-1], "path": row["path"]}
         for row in file_rows),
        key=lambda entry: entry["name"],
    )
    return {"repo_id": repo_id, "path": normalized, "exists": bool(exists),
            "dirs": dirs, "files": files}


# ---------------------------------------------------------------------------
# POST /api/repos/{id}/metrics/query - the metric query itself
# ---------------------------------------------------------------------------


def query_metrics(repo_id: int, request: MetricsQuery) -> dict:
    ensure_hbar(repo_id)
    obj = request.object
    commit_set = request.commit_set

    if obj.type == "repository":
        if obj.path not in (None, ""):
            raise HTTPException(
                status_code=400,
                detail="A repository object must not carry a path.",
            )
        table = "dir_changes"
        object_condition = "ch.dir_path = ?"
        object_path = ""
    elif obj.type == "directory":
        object_path = _normalize_path(obj.path, allow_root=True)
        table = "dir_changes"
        object_condition = "ch.dir_path = ?"
    else:  # file
        object_path = _normalize_path(obj.path, allow_root=False)
        table = "file_changes"
        object_condition = "ch.path = ?"

    canonical_filter = None
    member_ids = None
    if request.author is not None:
        canonical_filter = _resolve_author(repo_id, request.author)
        member_ids = canonical_filter["member_ids"]

    h_sql, h_params = _h_cte(repo_id, commit_set)
    if member_ids:
        h_author_sql, h_author_params = _h_cte(repo_id, commit_set, member_ids)
    else:
        h_author_sql, h_author_params = h_sql, h_params

    def aggregate(conn, h_clause: str, h_clause_params: list) -> tuple[int, dict, list]:
        base = (h_clause +
                f" SELECT ch.added AS added, ch.removed AS removed, h.author_id AS author_id,"
                f" ch.commit_sha AS sha, h.committer_date AS date"
                f" FROM {table} ch JOIN h ON h.sha = ch.commit_sha"
                f" WHERE ch.repo_id = ? AND {object_condition}")
        rows = conn.execute(base, [*h_clause_params, repo_id, object_path]).fetchall()
        totals = {"added": 0, "removed": 0, "modifications": 0}
        per_author: dict[int, dict] = {}
        series = []
        for row in rows:
            added, removed = row["added"], row["removed"]
            totals["added"] += added
            totals["removed"] += removed
            if added + removed > 0:
                totals["modifications"] += 1
            entry = per_author.setdefault(row["author_id"], {"modifications": 0, "churn": 0})
            entry["churn"] += added + removed
            if added + removed > 0:
                entry["modifications"] += 1
            series.append({"sha": row["sha"], "date": row["date"],
                           "added": added, "removed": removed})
        series.sort(key=lambda item: (item["date"], item["sha"]))
        return _count(conn, h_clause, h_clause_params), totals, per_author, series

    def _count(conn, h_clause: str, h_clause_params: list) -> int:
        return conn.execute(
            h_clause + " SELECT COUNT(*) AS n FROM h", h_clause_params
        ).fetchone()["n"]

    def metrics_payload(h_size: int, totals: dict) -> dict:
        added, removed = totals["added"], totals["removed"]
        churn = added + removed
        modifications = totals["modifications"]
        return {
            "added": added,
            "removed": removed,
            "growth": added - removed,
            "churn": churn,
            "modifications": modifications,
            "frequency": _safe_rate(modifications, h_size),
            "churn_rate": _safe_rate(churn, h_size),
        }

    with db.get_conn() as conn:
        h_size = _count(conn, h_sql, h_params)
        _, totals, per_author, series = aggregate(conn, h_sql, h_params)
        ctx = _identity_context(conn, repo_id)

        author_scope = None
        if canonical_filter is not None:
            author_h_size, author_totals, _, author_series = aggregate(
                conn, h_author_sql, h_author_params
            )
            author_scope = {
                "author_id": canonical_filter["author_id"],
                "group_id": canonical_filter["group_id"],
                "label": canonical_filter["label"],
                "merged": canonical_filter["merged"],
                "member_ids": canonical_filter["member_ids"],
                "h_size": author_h_size,
                "metrics": metrics_payload(author_h_size, author_totals),
                "series": author_series,
            }

    total_churn = totals["added"] + totals["removed"]
    canon: dict[int, dict] = {}
    for raw_author_id, entry in per_author.items():
        canonical = _canonical(ctx, raw_author_id)
        key = canonical["group_id"] if canonical["group_id"] is not None else canonical["author_id"]
        bucket = canon.setdefault(key, {
            "author_id": canonical["author_id"],
            "group_id": canonical["group_id"],
            "label": canonical["label"],
            "merged": canonical["merged"],
            "member_ids": canonical["member_ids"],
            "modifications": 0,
            "churn": 0,
        })
        bucket["modifications"] += entry["modifications"]
        bucket["churn"] += entry["churn"]
    authors = [
        {**bucket, "ownership": _safe_rate(bucket["churn"], total_churn)}
        for bucket in canon.values()
    ]
    authors.sort(key=lambda entry: (-entry["churn"], entry["label"]))

    return {
        "repo_id": repo_id,
        "object": {"type": obj.type, "path": object_path if obj.type != "repository" else ""},
        "commit_set": {
            "mode": commit_set.mode,
            "from": commit_set.from_,
            "to": commit_set.to,
            "shas_count": len(set(commit_set.shas or [])) if commit_set.mode == "list" else None,
        },
        "scope": {
            "h_size": h_size,
            "author_id": None if canonical_filter is None else canonical_filter["author_id"],
            "author_label": None if canonical_filter is None else canonical_filter["label"],
            "author_h_size": None if author_scope is None else author_scope["h_size"],
            "note": (
                "object_metrics, authors and series cover H = commit_set \u2229 H\u0304 "
                "(all non-merge commits reachable from the reference commit). "
                "author_scope, when present, covers H filtered to the selected author's "
                "commits."
            ),
        },
        "object_metrics": metrics_payload(h_size, totals),
        "authors": authors,
        "series": series,
        "author_scope": author_scope,
    }


# ---------------------------------------------------------------------------
# POST /api/repos/{id}/metrics/children - one-pass child table for the UI
# ---------------------------------------------------------------------------


class ChildrenQuery(BaseModel):
    object: QueryObject
    commit_set: QueryCommitSet
    author: int | None = None


def _child_payload(row: dict, kind: str, h_size: int) -> dict:
    added, removed = row["added"], row["removed"]
    churn = added + removed
    return {
        "name": row["path"].rsplit("/", 1)[-1],
        "path": row["path"],
        "type": kind,
        "added": added,
        "removed": removed,
        "growth": added - removed,
        "churn": churn,
        "modifications": row["modifications"],
        "frequency": _safe_rate(row["modifications"], h_size),
        "churn_rate": _safe_rate(churn, h_size),
    }


def query_children(repo_id: int, request: ChildrenQuery) -> dict:
    """Metrics for every immediate child (dirs + files) of a directory.

    Added for the dashboard milestone: the directory table, the top-N bar
    chart and the treemap all need one row per child; one /metrics/query per
    child would be N round-trips (160+ on large repositories). This runs the
    same aggregation as a single pass per table. Read-only; as with every
    object metric the rows always cover the full H - the author filter, when
    set, is resolved and echoed for parity but does not restrict the rows.
    """
    ensure_hbar(repo_id)
    obj = request.object
    if obj.type == "file":
        raise HTTPException(
            status_code=400,
            detail="Children exist only for directories or the repository.",
        )
    path = _normalize_path(obj.path, allow_root=True)  # '' = repository root

    canonical_filter = None
    if request.author is not None:
        canonical_filter = _resolve_author(repo_id, request.author)

    h_sql, h_params = _h_cte(repo_id, request.commit_set)

    def child_rows(conn, table: str, column: str) -> list[dict]:
        if path == "":
            # '' is the parent itself (dir_changes carries a row for it) - exclude it.
            scope_sql = (
                f"ch.repo_id = ? AND ch.{column} != '' AND instr(ch.{column}, '/') = 0"
            )
            scope_params: list = [repo_id]
        else:
            scope_sql = (
                f"ch.repo_id = ? AND ch.{column} LIKE ? ESCAPE '\\'"
                f" AND instr(substr(ch.{column}, ?), '/') = 0"
            )
            scope_params = [repo_id, f"{_escape_like(path)}/%", len(path) + 2]
        sql = (
            h_sql
            + f" SELECT ch.{column} AS path, SUM(ch.added) AS added,"
            f" SUM(ch.removed) AS removed,"
            f" SUM(CASE WHEN ch.added + ch.removed > 0 THEN 1 ELSE 0 END) AS modifications"
            f" FROM {table} ch JOIN h ON h.sha = ch.commit_sha"
            f" WHERE {scope_sql} GROUP BY ch.{column}"
        )
        return [dict(row) for row in conn.execute(sql, [*h_params, *scope_params])]

    with db.get_conn() as conn:
        h_size = conn.execute(
            h_sql + " SELECT COUNT(*) AS n FROM h", h_params
        ).fetchone()["n"]
        rows = [_child_payload(row, "directory", h_size)
                for row in child_rows(conn, "dir_changes", "dir_path")]
        rows += [_child_payload(row, "file", h_size)
                 for row in child_rows(conn, "file_changes", "path")]
    rows.sort(key=lambda entry: (-entry["churn"], entry["path"]))

    return {
        "repo_id": repo_id,
        "object": {"type": obj.type, "path": path},
        "commit_set": {
            "mode": request.commit_set.mode,
            "from": request.commit_set.from_,
            "to": request.commit_set.to,
            "shas_count": (len(set(request.commit_set.shas or []))
                           if request.commit_set.mode == "list" else None),
        },
        "scope": {
            "h_size": h_size,
            "author_id": None if canonical_filter is None else canonical_filter["author_id"],
            "author_label": None if canonical_filter is None else canonical_filter["label"],
            "note": (
                "children rows always cover the full H = commit_set \u2229 H\u0304; "
                "the author filter is reported for parity and does not restrict the rows."
            ),
        },
        "children": rows,
    }
