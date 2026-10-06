"""Repository ingestion: zip uploads and remote clone URLs.

Both flows create the repo row first (status 'uploaded' / 'cloning') and then
finish in a background thread, so the API never blocks the uvicorn worker on
large archives or long clones.
"""
from __future__ import annotations

import os
import posixpath
import re
import shutil
import stat
import subprocess
import threading
import zipfile
from pathlib import Path

from . import config, db

# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------

GIT_ENV = {
    # never hang waiting for credentials on a non-interactive clone
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "echo",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
}


def run_git(
    args: list[str],
    cwd: Path | None = None,
    timeout: int = config.GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(GIT_ENV)
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _probe_repo(src: Path) -> tuple[str, str | None]:
    """Return (head_sha, default_branch) for a cloned/extracted repository."""
    head = run_git(["rev-parse", "HEAD"], cwd=src)
    if head.returncode != 0:
        raise RuntimeError(
            "The repository was ingested but 'git rev-parse HEAD' failed: "
            + (head.stderr.strip() or "unknown error")
        )
    branch = run_git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=src)
    default_branch = branch.stdout.strip() or None
    if default_branch is None:
        abbrev = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=src)
        value = abbrev.stdout.strip()
        default_branch = value if value and value != "HEAD" else None
    return head.stdout.strip(), default_branch


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------

_URL_SCHEME_RE = re.compile(r"^(https?|ssh|git|file)://", re.IGNORECASE)
_SCP_URL_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:")


def is_supported_url(url: str) -> bool:
    return bool(_URL_SCHEME_RE.match(url) or _SCP_URL_RE.match(url))


def name_from_url(url: str) -> str:
    path = url.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    base = path.rsplit("/", 1)[-1]
    if ":" in base:  # scp-like: user@host:owner/repo
        base = base.rsplit(":", 1)[-1]
    return base.strip() or "repository"


def name_from_zip(filename: str) -> str:
    return Path(filename).stem.strip() or "repository"


# ---------------------------------------------------------------------------
# Zip validation (fast, no extraction)
# ---------------------------------------------------------------------------


def _fmt_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def validate_zip(path: Path) -> tuple[bool, str, dict]:
    """Cheaply check that a zip contains an extractable git repository.

    Returns (ok, error_message, meta). meta carries the chosen repository
    prefix inside the archive, whether .git was a directory or a gitdir
    pointer file, and the pointer target path (relative to the archive root).
    """
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()

            total = sum(i.file_size for i in infos)
            if total > config.MAX_ZIP_UNCOMPRESSED_BYTES:
                return False, (
                    f"Zip expands to {_fmt_bytes(total)}, over the "
                    f"{_fmt_bytes(config.MAX_ZIP_UNCOMPRESSED_BYTES)} limit."
                ), {}

            names: set[str] = set()
            explicit_dirs: set[str] = set()
            file_infos: dict[str, zipfile.ZipInfo] = {}
            for info in infos:
                name = info.filename.replace("\\", "/").rstrip("/")
                if not name:
                    continue
                names.add(name)
                if info.is_dir():
                    explicit_dirs.add(name)
                else:
                    file_infos[name] = info

            if not names:
                return False, "The zip archive is empty.", {}

            unsafe = [n for n in names if n.startswith("/") or ".." in n.split("/")]
            if unsafe:
                return False, f"Archive contains unsafe paths (e.g. '{unsafe[0]}').", {}

            dirs = set(explicit_dirs)
            for name in names:
                parent = posixpath.dirname(name)
                while parent:
                    dirs.add(parent)
                    parent = posixpath.dirname(parent)

            candidates = []  # (depth, prefix, mode, gitdir_target, problem)
            for name in sorted(names):
                if posixpath.basename(name) != ".git":
                    continue
                prefix = posixpath.dirname(name)
                depth = 0 if prefix == "" else prefix.count("/") + 1
                if name in dirs:
                    candidates.append((depth, prefix, "dir", None, None))
                    continue
                info = file_infos.get(name)
                if info is None:
                    continue
                with zf.open(info) as fh:
                    raw = fh.read(4096).decode("utf-8", "replace")
                match = re.search(r"^\s*gitdir:\s*(\S.*?)\s*$", raw, re.MULTILINE)
                if not match:
                    candidates.append(
                        (depth, prefix, "file", None, "a .git file without a 'gitdir:' pointer")
                    )
                    continue
                target = match.group(1)
                if target.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", target):
                    candidates.append(
                        (depth, prefix, "file", None,
                         "a .git file pointing to an absolute path outside the archive")
                    )
                    continue
                resolved = posixpath.normpath(posixpath.join(prefix, target))
                if resolved.startswith(".."):
                    candidates.append(
                        (depth, prefix, "file", None,
                         "a .git file pointing outside the archive")
                    )
                elif resolved not in dirs:
                    candidates.append(
                        (depth, prefix, "file", None,
                         f"a .git file whose gitdir '{resolved}' is missing from the archive")
                    )
                else:
                    candidates.append((depth, prefix, "file", resolved, None))

            usable = [c for c in candidates if c[4] is None]
            if not usable:
                if not candidates:
                    return False, (
                        "The zip does not contain a Git repository "
                        "(no .git directory or .git file found)."
                    ), {}
                problems = "; ".join(sorted({c[4] for c in candidates}))
                return False, f"Found .git marker(s) but could not use them: {problems}.", {}

            usable.sort(key=lambda c: (c[0], c[1]))
            chosen = usable[0]
            meta = {
                "prefix": chosen[1],
                "mode": chosen[2],
                "gitdir_target": chosen[3],
                "notes": [],
            }
            if len(usable) > 1:
                meta["notes"].append(
                    "Multiple git repositories found in the archive; using "
                    f"'{chosen[1] or '<archive root>'}'."
                )
            return True, "", meta
    except zipfile.BadZipFile:
        return False, "The uploaded file is not a valid zip archive.", {}


# ---------------------------------------------------------------------------
# Extraction and relocation
# ---------------------------------------------------------------------------


class ZipSafetyError(RuntimeError):
    pass


def _safe_target(dest: Path, name: str) -> Path:
    dest_resolved = dest.resolve()
    target = (dest / name).resolve()
    if target != dest_resolved and dest_resolved not in target.parents:
        raise ZipSafetyError(f"Archive member escapes the extraction directory: {name}")
    return target


def extract_zip(path: Path, dest: Path) -> list[str]:
    """Extract a validated zip with traversal and symlink protection."""
    warnings: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.endswith("/"):
                _safe_target(dest, name).mkdir(parents=True, exist_ok=True)
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                warnings.append(f"skipped symlink '{name}'")
                continue
            target = _safe_target(dest, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out, length=1 << 20)
    return warnings


def _relocate_extracted(repo_id: int, extract_root: Path, meta: dict) -> Path:
    """Move the repository root out of scratch space into data/repos/<id>/src."""
    prefix = meta.get("prefix", "")
    source_root = (extract_root / prefix) if prefix else extract_root
    if not source_root.is_dir():
        raise RuntimeError("Extraction finished but the repository root was not found.")

    src = config.repo_src_dir(repo_id)
    src.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.rmtree(src, ignore_errors=True)
    shutil.move(str(source_root), str(src))

    target_rel = meta.get("gitdir_target")
    if target_rel:
        rel_to_src = posixpath.relpath(target_rel, prefix or ".")
        if rel_to_src.startswith(".."):
            # A .git pointer file referencing a gitdir outside the repo folder:
            # keep the relative layout so git can still resolve the pointer.
            origin = extract_root / target_rel
            dest = (src / rel_to_src).resolve()
            workspace = (config.REPOS_DIR / str(repo_id)).resolve()
            if workspace not in dest.parents:
                raise RuntimeError("The .git file points outside the repository workspace.")
            if not origin.is_dir():
                raise RuntimeError("The .git file's gitdir target was not found after extraction.")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(origin), str(dest))
    return src


# ---------------------------------------------------------------------------
# Background processing
# ---------------------------------------------------------------------------


def process_zip_upload(repo_id: int, zip_path: Path, meta: dict) -> None:
    extract_root = config.EXTRACT_TMP_DIR / f"repo_{repo_id}"
    try:
        shutil.rmtree(extract_root, ignore_errors=True)
        warnings = extract_zip(zip_path, extract_root)
        src = _relocate_extracted(repo_id, extract_root, meta)
        head_sha, default_branch = _probe_repo(src)
        notes = list(meta.get("notes", [])) + warnings
        db.update_repo(
            repo_id,
            status="ready",
            status_detail="; ".join(notes) if notes else None,
            head_sha=head_sha,
            default_branch=default_branch,
            reference_commit=head_sha,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced through status_detail
        db.mark_failed(repo_id, f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(extract_root, ignore_errors=True)
        zip_path.unlink(missing_ok=True)


def process_clone(repo_id: int, url: str) -> None:
    src = config.repo_src_dir(repo_id)
    src.parent.mkdir(parents=True, exist_ok=True)
    try:
        if src.exists():
            shutil.rmtree(src, ignore_errors=True)
        result = run_git(["clone", "--", url, str(src)], timeout=config.CLONE_TIMEOUT_SECONDS)
        if result.returncode != 0:
            raise RuntimeError(
                "git clone failed: "
                + (result.stderr.strip() or result.stdout.strip() or "unknown error")
            )
        head_sha, default_branch = _probe_repo(src)
        db.update_repo(
            repo_id,
            status="ready",
            status_detail=None,
            head_sha=head_sha,
            default_branch=default_branch,
            reference_commit=head_sha,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced through status_detail
        db.mark_failed(repo_id, f"{type(exc).__name__}: {exc}")
        shutil.rmtree(src.parent, ignore_errors=True)


def start_background(fn, *args) -> threading.Thread:
    thread = threading.Thread(target=fn, args=args, daemon=True, name=fn.__name__)
    thread.start()
    return thread
