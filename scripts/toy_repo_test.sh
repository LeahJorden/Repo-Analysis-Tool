#!/usr/bin/env bash
# Build a synthetic repository exercising every metric-spec rule
# (root add, pure rename, rename+edit, deletion, binary add), compute its
# metrics into a throwaway database, and assert the spec through toy_check.py.
#
# Usage: scripts/toy_repo_test.sh   (exit 0 = all assertions passed)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRATCH="$ROOT/data/scratch/toy_repo_test"
REPO="$SCRATCH/toy_repo"
DB_PATH="$SCRATCH/toy_test.db"

echo "== toy_repo_test: building fixture at $REPO"
rm -rf "$SCRATCH"
mkdir -p "$REPO/src" "$REPO/assets"

git -C "$REPO" init -q -b main
git -C "$REPO" config user.name "Toy Tester"
git -C "$REPO" config user.email "toy@example.com"
git -C "$REPO" config commit.gpgsign false

commit() {  # $1 = ISO date, $2 = message
    GIT_AUTHOR_DATE="$1" GIT_COMMITTER_DATE="$1" \
        git -C "$REPO" commit -q --no-gpg-sign -m "$2"
}

# c1 - root commit: three additions, one of them binary
printf 'line1\nline2\nline3\n' > "$REPO/README.md"
printf 'alpha\nbeta\n' > "$REPO/src/a.txt"
printf 'PNG\x00BINARY' > "$REPO/assets/logo.png"
git -C "$REPO" add -A
commit "2020-01-01T00:00:01Z" "c1 root add"

# c2 - pure rename: all increments must be zero
git -C "$REPO" mv src/a.txt src/b.txt
commit "2020-01-02T00:00:01Z" "c2 pure rename"

# c3 - rename plus edit: delta attributed to the new path only
git -C "$REPO" mv src/b.txt src/c.txt
printf 'gamma\n' >> "$REPO/src/c.txt"
git -C "$REPO" add -A
commit "2020-01-03T00:00:01Z" "c3 rename and edit"

# c4 - deletion: removals at the path, no additions
git -C "$REPO" rm -q README.md
commit "2020-01-04T00:00:01Z" "c4 deletion"

# c5 - binary add: flagged but never measured in lines
printf 'JPEG\x00DATA' > "$REPO/assets/logo2.png"
git -C "$REPO" add assets/logo2.png
commit "2020-01-05T00:00:01Z" "c5 binary add"

PYTHON="$ROOT/venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3)"
fi

echo "== toy_repo_test: computing metrics into $DB_PATH"
RAT_DB_PATH="$DB_PATH" "$PYTHON" "$ROOT/scripts/toy_check.py" "$REPO"

echo
echo "== toy_repo_test: ALL ASSERTIONS PASSED"
