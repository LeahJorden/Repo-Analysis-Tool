"""Central configuration for the Repo Analysis Tool: paths and limits."""
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"

DATA_DIR = ROOT_DIR / "data"
REPOS_DIR = DATA_DIR / "repos"          # data/repos/<repo_id>/src holds each ingested repo
TMP_DIR = DATA_DIR / "tmp"
UPLOAD_TMP_DIR = TMP_DIR / "uploads"    # incoming zip files (deleted after processing)
EXTRACT_TMP_DIR = TMP_DIR / "extract"   # scratch space for archive extraction
# RAT_DB_PATH lets verification scripts point the engine at a throwaway database.
DB_PATH = (
    Path(os.environ["RAT_DB_PATH"]) if os.environ.get("RAT_DB_PATH") else DATA_DIR / "rat.db"
)

MAX_ZIP_UPLOAD_BYTES = 2 * 1024 ** 3         # hard cap on the uploaded (compressed) zip
MAX_ZIP_UNCOMPRESSED_BYTES = 4 * 1024 ** 3   # zip-bomb guard on total uncompressed size

GIT_TIMEOUT_SECONDS = 60          # quick metadata commands (rev-parse, symbolic-ref)
CLONE_TIMEOUT_SECONDS = 60 * 60   # full clone of very large repositories


def repo_src_dir(repo_id: int) -> Path:
    """Directory that holds the ingested working tree for a repository."""
    return REPOS_DIR / str(repo_id) / "src"
