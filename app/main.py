"""FastAPI entry point: JSON API + static dashboard for the Repo Analysis Tool."""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, ingest, metrics, query


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    # Scratch space from a previous process is always safe to drop.
    for tmp_dir in (config.UPLOAD_TMP_DIR, config.EXTRACT_TMP_DIR):
        shutil.rmtree(tmp_dir, ignore_errors=True)
    stale = db.reset_stale_repos()
    if stale:
        print(f"[rat] marked {stale} interrupted ingestion(s) as failed")
    interrupted = db.reset_running_computes()
    if interrupted:
        print(f"[rat] marked {interrupted} interrupted computation(s) as failed")
    yield


app = FastAPI(title="Repo Analysis Tool", version="0.1.0", lifespan=lifespan)


class CloneRequest(BaseModel):
    url: str


class ComputeRequest(BaseModel):
    # Optional: move the reference commit before computing (validated against
    # the cloned repository); the H-bar cache rebuilds automatically once the
    # new computation lands. Added for the dashboard's reference-commit input.
    reference_commit: str | None = None


def _duplicate_warning(source_type: str, source_ref: str) -> tuple[str | None, list[int]]:
    duplicates = db.find_duplicate_repos(source_type, source_ref)
    return ("duplicate_source" if duplicates else None), duplicates


@app.post("/api/repos/clone")
def clone_repo(request: CloneRequest):
    url = request.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="A repository URL is required.")
    if not ingest.is_supported_url(url):
        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported URL. Use https://, http://, ssh://, git://, file:// "
                "or the scp-like git@host:path form."
            ),
        )
    warning, duplicates = _duplicate_warning("url", url)
    repo_id = db.create_repo(ingest.name_from_url(url), "url", url, "cloning")
    ingest.start_background(ingest.process_clone, repo_id, url)
    return {
        "repo_id": repo_id,
        "status": "cloning",
        "warning": warning,
        "duplicate_repo_ids": duplicates,
    }


@app.post("/api/repos/upload")
def upload_repo(file: UploadFile = File(...)):
    raw_name = (file.filename or "").replace("\\", "/")
    filename = raw_name.rsplit("/", 1)[-1]
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip uploads are supported.")

    tmp_path = config.UPLOAD_TMP_DIR / f"{uuid.uuid4().hex}.zip"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)

    size = 0
    too_big = False
    with open(tmp_path, "wb") as out:
        while chunk := file.file.read(1 << 20):
            size += len(chunk)
            if size > config.MAX_ZIP_UPLOAD_BYTES:
                too_big = True
                break
            out.write(chunk)
    if too_big:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=413,
            detail=f"Zip exceeds the {config.MAX_ZIP_UPLOAD_BYTES // (1024 ** 3)} GiB upload cap.",
        )

    ok, error, meta = ingest.validate_zip(tmp_path)
    if not ok:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=error)

    warning, duplicates = _duplicate_warning("zip", filename)
    repo_id = db.create_repo(ingest.name_from_zip(filename), "zip", filename, "uploaded")
    ingest.start_background(ingest.process_zip_upload, repo_id, tmp_path, meta)
    return {
        "repo_id": repo_id,
        "status": "uploaded",
        "warning": warning,
        "duplicate_repo_ids": duplicates,
    }


@app.get("/api/repos")
def list_repos():
    return {"repos": db.list_repos()}


@app.get("/api/repos/{repo_id}")
def get_repo(repo_id: int):
    repo = db.get_repo(repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repository not found.")
    return repo


@app.post("/api/repos/{repo_id}/compute")
def start_compute(repo_id: int, request: ComputeRequest | None = None):
    repo = db.get_repo(repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repository not found.")
    if repo["status"] != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"Repository is not ready for computation (status: {repo['status']}).",
        )
    job = db.get_compute_job(repo_id)
    if job and job["state"] == "running":
        raise HTTPException(
            status_code=409, detail="A computation is already running for this repository."
        )
    if request and request.reference_commit and request.reference_commit.strip():
        revision = request.reference_commit.strip()
        src = config.repo_src_dir(repo_id)
        try:
            resolved = subprocess.run(
                ["git", "-C", str(src), "rev-parse", "--verify", "--quiet",
                 "--end-of-options", f"{revision}^{{commit}}"],
                capture_output=True, text=True, timeout=config.GIT_TIMEOUT_SECONDS,
                env={**os.environ, **ingest.GIT_ENV},
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="git rev-parse timed out.")
        sha = resolved.stdout.strip()
        if resolved.returncode != 0 or not sha:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown revision '{revision}' in this repository.",
            )
        db.update_repo(repo_id, reference_commit=sha)
    db.start_compute_job(repo_id, 0)
    ingest.start_background(metrics.compute_repo_bg, repo_id)
    return {"repo_id": repo_id, "state": "running"}


@app.get("/api/repos/{repo_id}/compute/status")
def compute_status(repo_id: int):
    repo = db.get_repo(repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repository not found.")
    job = db.get_compute_job(repo_id)
    if job is None:
        job = {"state": "idle", "processed": 0, "total": 0, "error": None,
               "started_at": None, "finished_at": None}
    total = job["total"] or 0
    if job["state"] == "done":
        percent = 100.0
    elif total:
        percent = round(100.0 * job["processed"] / total, 1)
    else:
        percent = 0.0
    return {
        "repo_id": repo_id,
        "state": job["state"],
        "processed": job["processed"],
        "total": total,
        "percent": percent,
        "error": job["error"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "computed_at": repo["computed_at"],
    }


def _require_repo(repo_id: int) -> dict:
    repo = db.get_repo(repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repository not found.")
    return repo


@app.get("/api/repos/{repo_id}/authors")
def repo_authors(repo_id: int):
    _require_repo(repo_id)
    return query.list_authors(repo_id)


@app.get("/api/repos/{repo_id}/authors/merges")
def repo_author_merges(repo_id: int):
    _require_repo(repo_id)
    return query.list_merges(repo_id)


@app.post("/api/repos/{repo_id}/authors/merge")
def merge_authors(repo_id: int, request: query.MergeRequest):
    _require_repo(repo_id)
    return query.create_merge(repo_id, request.aliases, request.label)


@app.delete("/api/repos/{repo_id}/authors/merges/{group_id}")
def unmerge_authors(repo_id: int, group_id: int):
    _require_repo(repo_id)
    return query.delete_merge(repo_id, group_id)


@app.get("/api/repos/{repo_id}/commits")
def repo_commits(
    repo_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    q: str | None = None,
    author: int | None = None,
    date_from: int | None = Query(None, alias="from"),
    date_to: int | None = Query(None, alias="to"),
):
    _require_repo(repo_id)
    return query.list_commits(
        repo_id,
        page=page,
        per_page=per_page,
        q=q,
        author_id=author,
        date_from=date_from,
        date_to=date_to,
    )


@app.get("/api/repos/{repo_id}/tree")
def repo_tree(repo_id: int, path: str = ""):
    _require_repo(repo_id)
    return query.tree_children(repo_id, path)


@app.post("/api/repos/{repo_id}/metrics/query")
def metrics_query(repo_id: int, request: query.MetricsQuery):
    _require_repo(repo_id)
    return query.query_metrics(repo_id, request)


@app.post("/api/repos/{repo_id}/metrics/children")
def metrics_children(repo_id: int, request: query.ChildrenQuery):
    _require_repo(repo_id)
    return query.query_children(repo_id, request)


# Static dashboard (registered last so /api/* keeps priority).
app.mount("/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="dashboard")
