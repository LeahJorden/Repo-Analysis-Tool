"""SQLite access layer: schema, connections and repository row helpers."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL,
    source_type      TEXT    NOT NULL CHECK (source_type IN ('zip', 'url')),
    source_ref       TEXT    NOT NULL,
    default_branch   TEXT,
    head_sha         TEXT,
    reference_commit TEXT,
    status           TEXT    NOT NULL DEFAULT 'uploaded'
                     CHECK (status IN ('uploaded', 'cloning', 'ready', 'failed')),
    status_detail    TEXT,
    created_at       TEXT    NOT NULL,
    computed_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_repos_status ON repos (status);
CREATE INDEX IF NOT EXISTS idx_repos_source ON repos (source_type, source_ref);

CREATE TABLE IF NOT EXISTS authors (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repos (id) ON DELETE CASCADE,
    name    TEXT    NOT NULL,
    email   TEXT    NOT NULL,
    UNIQUE (repo_id, name, email)
);

CREATE TABLE IF NOT EXISTS commits (
    repo_id        INTEGER NOT NULL REFERENCES repos (id) ON DELETE CASCADE,
    sha            TEXT    NOT NULL,
    parent_sha     TEXT,
    author_id      INTEGER NOT NULL REFERENCES authors (id),
    committer_date INTEGER NOT NULL,
    subject        TEXT,
    PRIMARY KEY (repo_id, sha)
);

CREATE INDEX IF NOT EXISTS idx_commits_repo_date ON commits (repo_id, committer_date);

CREATE TABLE IF NOT EXISTS file_changes (
    repo_id      INTEGER NOT NULL,
    commit_sha   TEXT    NOT NULL,
    path         TEXT    NOT NULL,
    added        INTEGER NOT NULL DEFAULT 0,
    removed      INTEGER NOT NULL DEFAULT 0,
    renamed_from TEXT,
    is_binary    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_file_changes_commit ON file_changes (repo_id, commit_sha);
CREATE INDEX IF NOT EXISTS idx_file_changes_path ON file_changes (repo_id, path);

CREATE TABLE IF NOT EXISTS dir_changes (
    repo_id    INTEGER NOT NULL,
    commit_sha TEXT    NOT NULL,
    dir_path   TEXT    NOT NULL,
    added      INTEGER NOT NULL DEFAULT 0,
    removed    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_dir_changes_commit ON dir_changes (repo_id, commit_sha);
CREATE INDEX IF NOT EXISTS idx_dir_changes_path ON dir_changes (repo_id, dir_path);

CREATE TABLE IF NOT EXISTS compute_jobs (
    repo_id     INTEGER PRIMARY KEY REFERENCES repos (id) ON DELETE CASCADE,
    state       TEXT    NOT NULL DEFAULT 'idle',
    processed   INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    started_at  TEXT,
    finished_at TEXT
);

-- ---------------------------------------------------------------------------
-- Query layer (P3): author merge groups + cached H-bar reachability
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS author_groups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id    INTEGER NOT NULL REFERENCES repos (id) ON DELETE CASCADE,
    label      TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

-- One author belongs to at most one merge group per repository.
CREATE TABLE IF NOT EXISTS author_group_members (
    repo_id   INTEGER NOT NULL REFERENCES repos (id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES authors (id) ON DELETE CASCADE,
    group_id  INTEGER NOT NULL REFERENCES author_groups (id) ON DELETE CASCADE,
    PRIMARY KEY (repo_id, author_id)
);

CREATE INDEX IF NOT EXISTS idx_author_group_members_group ON author_group_members (group_id);

-- Materialised H-bar: the non-merge commits reachable from the reference
-- commit (membership == the in_hbar flag). Rebuilt when the reference commit
-- or the computed data changes; never consulted against git at query time.
CREATE TABLE IF NOT EXISTS hbar_members (
    repo_id INTEGER NOT NULL REFERENCES repos (id) ON DELETE CASCADE,
    sha     TEXT    NOT NULL,
    PRIMARY KEY (repo_id, sha)
);

CREATE TABLE IF NOT EXISTS hbar_cache (
    repo_id      INTEGER PRIMARY KEY REFERENCES repos (id) ON DELETE CASCADE,
    ref_sha      TEXT,
    computed_at  TEXT    NOT NULL,
    member_count INTEGER NOT NULL DEFAULT 0,
    bfs_count    INTEGER NOT NULL DEFAULT 0
);
"""

ROW_FIELDS = (
    "id", "name", "source_type", "source_ref", "default_branch", "head_sha",
    "reference_commit", "status", "status_detail", "created_at", "computed_at",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dirs() -> None:
    for directory in (
        config.DATA_DIR,
        config.REPOS_DIR,
        config.TMP_DIR,
        config.UPLOAD_TMP_DIR,
        config.EXTRACT_TMP_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    ensure_dirs()
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


def reset_stale_repos() -> int:
    """Ingestions interrupted by a restart can never finish; mark them failed."""
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE repos SET status = 'failed',"
            " status_detail = 'Ingestion interrupted by an application restart'"
            " WHERE status IN ('uploaded', 'cloning')"
        )
        return cursor.rowcount


def repo_to_dict(row: sqlite3.Row) -> dict:
    return {field: row[field] for field in ROW_FIELDS}


def create_repo(name: str, source_type: str, source_ref: str, status: str) -> int:
    with get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO repos (name, source_type, source_ref, status, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, source_type, source_ref, status, utc_now_iso()),
        )
        return int(cursor.lastrowid)


def get_repo(repo_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    return repo_to_dict(row) if row else None


def list_repos() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM repos ORDER BY id DESC").fetchall()
    return [repo_to_dict(row) for row in rows]


def find_duplicate_repos(source_type: str, source_ref: str) -> list[int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM repos WHERE source_type = ? AND source_ref = ?",
            (source_type, source_ref),
        ).fetchall()
    return [int(row["id"]) for row in rows]


def update_repo(repo_id: int, **fields) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE repos SET {assignments} WHERE id = ?",
            (*fields.values(), repo_id),
        )


def mark_failed(repo_id: int, detail: str) -> None:
    update_repo(repo_id, status="failed", status_detail=detail[:1000])


# ---------------------------------------------------------------------------
# Metric storage (compute engine)
# ---------------------------------------------------------------------------


def clear_repo_metrics(repo_id: int) -> None:
    """Wipe computed rows before a (re)compute, so partial runs never linger."""
    with get_conn() as conn:
        conn.execute("DELETE FROM file_changes WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM dir_changes WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM commits WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM authors WHERE repo_id = ?", (repo_id,))


def insert_metric_batch(repo_id: int, author_keys, commit_rows, file_rows, dir_rows) -> None:
    """Persist one extraction batch in a single transaction.

    commit_rows: (sha, parent_sha, author_name, author_email, committer_date, subject)
    file_rows:   (commit_sha, path, added, removed, renamed_from, is_binary)
    dir_rows:    (commit_sha, dir_path, added, removed)
    """
    if not (commit_rows or file_rows or dir_rows):
        return
    with get_conn() as conn:
        if author_keys:
            conn.executemany(
                "INSERT OR IGNORE INTO authors (repo_id, name, email) VALUES (?, ?, ?)",
                [(repo_id, name, email) for (name, email) in author_keys],
            )
        if commit_rows:
            author_ids = {
                (row["name"], row["email"]): row["id"]
                for row in conn.execute(
                    "SELECT id, name, email FROM authors WHERE repo_id = ?", (repo_id,)
                )
            }
            conn.executemany(
                "INSERT OR REPLACE INTO commits"
                " (repo_id, sha, parent_sha, author_id, committer_date, subject)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (repo_id, sha, parent, author_ids[(name, email)], committer_date, subject)
                    for (sha, parent, name, email, committer_date, subject) in commit_rows
                ],
            )
        if file_rows:
            conn.executemany(
                "INSERT INTO file_changes"
                " (repo_id, commit_sha, path, added, removed, renamed_from, is_binary)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(repo_id, *row) for row in file_rows],
            )
        if dir_rows:
            conn.executemany(
                "INSERT INTO dir_changes (repo_id, commit_sha, dir_path, added, removed)"
                " VALUES (?, ?, ?, ?, ?)",
                [(repo_id, *row) for row in dir_rows],
            )


def start_compute_job(repo_id: int, total: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO compute_jobs (repo_id, state, processed, total, error, started_at,"
            " finished_at) VALUES (?, 'running', 0, ?, NULL, ?, NULL)"
            " ON CONFLICT (repo_id) DO UPDATE SET"
            " state = 'running', processed = 0, total = excluded.total, error = NULL,"
            " started_at = excluded.started_at, finished_at = NULL",
            (repo_id, total, utc_now_iso()),
        )


def update_compute_progress(repo_id: int, processed: int, total: int | None = None) -> None:
    with get_conn() as conn:
        if total is None:
            conn.execute(
                "UPDATE compute_jobs SET processed = ? WHERE repo_id = ?", (processed, repo_id)
            )
        else:
            conn.execute(
                "UPDATE compute_jobs SET processed = ?, total = ? WHERE repo_id = ?",
                (processed, total, repo_id),
            )


def finish_compute_job(repo_id: int, error: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE compute_jobs SET state = ?, error = ?, finished_at = ? WHERE repo_id = ?",
            ("failed" if error else "done", error, utc_now_iso(), repo_id),
        )
        if not error:
            conn.execute(
                "UPDATE repos SET computed_at = ? WHERE id = ?", (utc_now_iso(), repo_id)
            )


def get_compute_job(repo_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM compute_jobs WHERE repo_id = ?", (repo_id,)
        ).fetchone()
    return dict(row) if row else None


def reset_running_computes() -> int:
    """Computation cannot survive a restart; mark them failed for a clean retry."""
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE compute_jobs SET state = 'failed',"
            " error = 'Compute interrupted by an application restart', finished_at = ?"
            " WHERE state = 'running'",
            (utc_now_iso(),),
        )
        return cursor.rowcount
