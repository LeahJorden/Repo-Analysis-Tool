"""Metric computation engine: one-pass git extraction + spec-exact metrics.

The engine streams a single ``git log --numstat -z`` pass over the repository,
persists per-commit per-path changes (files plus materialised directory
roll-ups) into SQLite, and tracks progress in ``compute_jobs``. Every later
metric query (filters, aggregations, author breakdowns) is pure SQL over these
tables - git is only ever read once per (re)compute.

Extraction format (verified empirically against git 2.43):

    \\x01 SHA \\x01 parents \\x01 author name \\x01 author email \\x01 ctime \\x01 subject \\x02
    NUL-terminated numstat records follow:
      3\\t0\\tREADME.md                -> added, removed, path
      -\\t-\\tassets/logo.png          -> binary (no line metrics)
      0\\t0\\t                         -> rename: next two records are old path, new path
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator

from . import config, db, ingest

LOG_FORMAT = "%x01%H%x01%P%x01%an%x01%ae%x01%ct%x01%s%x02"
NUMSTAT_RE = re.compile(rb"^(\d+)\t(\d+)\t(.*)$", re.DOTALL)
BINARY_RE = re.compile(rb"^-\t-\t(.*)$", re.DOTALL)

BATCH_COMMITS = 5000  # commits per insert transaction / progress update
READ_CHUNK_BYTES = 1 << 20


@dataclass
class FileChange:
    path: str
    added: int
    removed: int
    renamed_from: str | None = None
    is_binary: bool = False


@dataclass
class CommitRecord:
    sha: str
    parents: list[str]
    author_name: str
    author_email: str
    committer_date: int
    subject: str
    files: list[FileChange] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Streaming parser for `git log --numstat -z` output
# ---------------------------------------------------------------------------


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _read_chunks(stream: BinaryIO, size: int = READ_CHUNK_BYTES) -> Iterator[bytes]:
    """Yield NUL-separated chunks from a binary stream, incrementally."""
    carry = b""
    while True:
        data = stream.read(size)
        if not data:
            break
        parts = (carry + data).split(b"\x00")
        carry = parts.pop()
        yield from parts
    if carry:
        yield carry


def _parse_header(raw: bytes) -> CommitRecord:
    if raw.startswith(b"\x01"):
        raw = raw[1:]
    fields = raw.split(b"\x01")
    if len(fields) < 6:
        raise ValueError(f"malformed commit header: {raw[:120]!r}")
    sha, parents, name, email, committer_date, *subject_parts = fields
    return CommitRecord(
        sha=_decode(sha),
        parents=[_decode(p) for p in parents.split(b" ") if p],
        author_name=_decode(name),
        author_email=_decode(email),
        committer_date=int(committer_date or b"0"),
        subject=_decode(b"\x01".join(subject_parts)),
    )


def iter_log_commits(stream: BinaryIO) -> Iterator[CommitRecord]:
    """Parse a commit stream produced by :func:`build_log_command`."""
    current: CommitRecord | None = None
    rename_state: list | None = None  # [added, removed, is_binary, old_path_or_None]
    for raw in _read_chunks(stream):
        chunk = raw.lstrip(b"\n")
        if not chunk:
            continue
        if rename_state is not None:
            if rename_state[3] is None:
                rename_state[3] = _decode(chunk)
            else:
                current.files.append(
                    FileChange(
                        path=_decode(chunk),
                        added=rename_state[0],
                        removed=rename_state[1],
                        renamed_from=rename_state[3],
                        is_binary=rename_state[2],
                    )
                )
                rename_state = None
            continue
        if b"\x02" in chunk:
            left, _, rest = chunk.partition(b"\x02")
            if current is not None:
                yield current
            current = _parse_header(left)
            rest = rest.lstrip(b"\n")
            if not rest:
                continue
            chunk = rest
        if current is None:
            raise ValueError(f"numstat record before any commit header: {chunk[:60]!r}")
        match = NUMSTAT_RE.match(chunk)
        if match:
            added, removed = int(match.group(1)), int(match.group(2))
            path_bytes, is_binary = match.group(3), False
        else:
            bin_match = BINARY_RE.match(chunk)
            if not bin_match:
                raise ValueError(f"unparsable numstat record: {chunk[:80]!r}")
            added = removed = 0
            path_bytes, is_binary = bin_match.group(1), True
        if path_bytes == b"":
            rename_state = [added, removed, is_binary, None]
        else:
            current.files.append(
                FileChange(path=_decode(path_bytes), added=added, removed=removed, is_binary=is_binary)
            )
    if rename_state is not None:
        raise ValueError("truncated rename record at end of stream")
    if current is not None:
        yield current


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def build_log_command(src: Path, ref: str) -> list[str]:
    return [
        "git", "-C", str(src), "log",
        "--use-mailmap", "--no-merges", "--root", "-M50%",
        "--numstat", "-z",
        f"--pretty=tformat:{LOG_FORMAT}",
        ref, "--",
    ]


def count_commits(src: Path, ref: str) -> int:
    result = ingest.run_git(["rev-list", "--count", "--no-merges", ref, "--"], cwd=src)
    if result.returncode != 0:
        raise RuntimeError(
            "git rev-list failed: " + (result.stderr.strip() or "unknown error")
        )
    return int(result.stdout.strip() or 0)


def _ancestor_dirs(path: str) -> list[str]:
    """All directories containing (at any depth) the given file path.

    The repository root is represented by the empty string. A file at the
    repository root returns just [""].
    """
    result = [""]
    parts = path.split("/")
    for i in range(1, len(parts)):
        result.append("/".join(parts[:i]))
    return result


def _accumulate_changes(record: CommitRecord, file_rows: list, dir_rows: list) -> None:
    dir_totals: dict[str, list[int]] = {}
    for change in record.files:
        file_rows.append(
            (
                record.sha,
                change.path,
                change.added,
                change.removed,
                change.renamed_from,
                1 if change.is_binary else 0,
            )
        )
        for directory in _ancestor_dirs(change.path):
            entry = dir_totals.get(directory)
            if entry is None:
                dir_totals[directory] = [change.added, change.removed]
            else:
                entry[0] += change.added
                entry[1] += change.removed
    for directory, (added, removed) in dir_totals.items():
        dir_rows.append((record.sha, directory, added, removed))


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


def compute_repo(repo_id: int, src: Path | None = None, ref: str | None = None) -> int:
    """Extract and persist all metric rows for one repository.

    ``src`` overrides the repository path (used by verification scripts) and
    ``ref`` overrides the reference commit. Returns the number of commits
    processed. Failures are recorded on the compute job and re-raised.
    """
    repo = db.get_repo(repo_id)
    if repo is None:
        raise ValueError(f"repository {repo_id} does not exist")
    src = Path(src) if src else config.repo_src_dir(repo_id)
    ref = ref or repo["reference_commit"] or repo["head_sha"] or "HEAD"

    db.start_compute_job(repo_id, 0)
    try:
        db.clear_repo_metrics(repo_id)
        total = count_commits(src, ref)
        db.update_compute_progress(repo_id, 0, total)

        env = {**os.environ, **ingest.GIT_ENV}
        config.TMP_DIR.mkdir(parents=True, exist_ok=True)
        err_path = config.TMP_DIR / f"compute_{repo_id}.log"

        processed = 0
        pending_commits: list = []
        pending_files: list = []
        pending_dirs: list = []
        pending_authors: set = set()

        with open(err_path, "w+b") as err_file:
            process = subprocess.Popen(
                build_log_command(src, ref),
                stdout=subprocess.PIPE,
                stderr=err_file,
                env=env,
            )
            try:
                for record in iter_log_commits(process.stdout):
                    pending_commits.append(
                        (
                            record.sha,
                            record.parents[0] if record.parents else None,
                            record.author_name,
                            record.author_email,
                            record.committer_date,
                            record.subject,
                        )
                    )
                    pending_authors.add((record.author_name, record.author_email))
                    _accumulate_changes(record, pending_files, pending_dirs)
                    processed += 1
                    if processed % BATCH_COMMITS == 0:
                        db.insert_metric_batch(
                            repo_id, pending_authors, pending_commits, pending_files, pending_dirs
                        )
                        pending_commits, pending_files, pending_dirs = [], [], []
                        pending_authors = set()
                        db.update_compute_progress(repo_id, processed)
                returncode = process.wait()
                err_file.seek(0)
                stderr_text = err_file.read().decode("utf-8", "replace").strip()[:500]
            except Exception:
                process.kill()
                process.wait()
                raise
            finally:
                process.stdout.close()

        db.insert_metric_batch(
            repo_id, pending_authors, pending_commits, pending_files, pending_dirs
        )
        db.update_compute_progress(repo_id, processed)

        if returncode != 0:
            raise RuntimeError(f"git log failed (exit {returncode}): {stderr_text or 'unknown error'}")
        if processed != total:
            raise RuntimeError(
                f"expected {total} commits from git rev-list but parsed {processed}"
            )

        db.finish_compute_job(repo_id, None)
        return processed
    except Exception as exc:
        db.finish_compute_job(repo_id, f"{type(exc).__name__}: {exc}")
        raise


def compute_repo_bg(repo_id: int) -> None:
    """Background-thread wrapper: records failure on the job row, never raises."""
    try:
        compute_repo(repo_id)
    except Exception as exc:  # noqa: BLE001 - already recorded on the job row
        print(f"[rat] compute failed for repository {repo_id}: {exc}")
