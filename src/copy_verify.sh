#!/bin/sh
# copy_verify.sh — copy a folder tree, keep the file dates, then verify the copy.
# Companion to the fits tools 0.9.1.  Works in sh, bash and zsh (macOS and Linux).
#
# Usage:
#   ./copy_verify.sh SOURCE_FOLDER DEST_FOLDER          copy, then verify
#   ./copy_verify.sh --verify-only SOURCE DEST          just compare, copy nothing
#   ./copy_verify.sh --use-cp SOURCE DEST               copy with cp -Rp instead of rsync -a
#
# Copies the CONTENTS of SOURCE into DEST (created if needed).  Safe to re-run: it
# finishes what is missing (rsync) and never deletes anything.  The verification compares
# every file's modification date, size and relative path (.DS_Store files are ignored).
# Exit codes: 0 identical, 1 differences found, 2 bad arguments / not enough space.

set -u

usage() {
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

die() { echo "  ✗ $*" >&2; exit 2; }

VERIFY_ONLY=0
USE_CP=0
SRC=""
DEST=""
for arg in "$@"; do
    case "$arg" in
        --verify-only) VERIFY_ONLY=1 ;;
        --use-cp)      USE_CP=1 ;;
        -h|--help)     usage ;;
        *)  if   [ -z "$SRC"  ]; then SRC="$arg"
            elif [ -z "$DEST" ]; then DEST="$arg"
            else die "too many arguments: '$arg'"; fi ;;
    esac
done
[ -n "$SRC" ] && [ -n "$DEST" ] || usage

# ── sanity checks ─────────────────────────────────────────────────────────────
case "$SRC$DEST" in
    *"<"*|*">"*) die "a path still contains < > — replace the placeholder with the real name" ;;
esac
[ -d "$SRC" ] || die "source folder not found: $SRC"
SRC_ABS=$(cd "$SRC" && pwd -P)

if [ "$VERIFY_ONLY" -eq 0 ]; then
    mkdir -p "$DEST" || die "cannot create destination: $DEST"
fi
[ -d "$DEST" ] || die "destination folder not found: $DEST"
DEST_ABS=$(cd "$DEST" && pwd -P)

[ "$SRC_ABS" != "$DEST_ABS" ] || die "source and destination are the same folder"
case "$DEST_ABS/" in "$SRC_ABS"/*) die "destination is inside the source — that would copy a folder into itself" ;; esac
case "$SRC_ABS/" in "$DEST_ABS"/*) die "source is inside the destination" ;; esac

# ── helpers ───────────────────────────────────────────────────────────────────
if stat -f '%m' / >/dev/null 2>&1; then
    file_list() { (cd "$1" && find . -type f ! -name .DS_Store -exec stat -f '%m %z %N' {} + 2>/dev/null | sort -k3); }
else
    file_list() { (cd "$1" && find . -type f ! -name .DS_Store -exec stat -c '%Y %s %n' {} + 2>/dev/null | sort -k3); }
fi

STAMP=$(date +%Y%m%d_%H%M%S)
LOG="copy_verify_$STAMP.log"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/copy_verify.XXXXXX") || die "cannot create a temporary folder"
trap 'rm -rf "$TMP"' EXIT

echo "  copy_verify  (fits tools 0.9.1)"
echo "  Source      : $SRC_ABS"
echo "  Destination : $DEST_ABS"

# ── copy ──────────────────────────────────────────────────────────────────────
if [ "$VERIFY_ONLY" -eq 0 ]; then
    need_kb=$(du -sk "$SRC_ABS" | cut -f1)
    free_kb=$(df -k "$DEST_ABS" | tail -1 | awk '{print $4}')
    have_files=$(find "$DEST_ABS" -type f ! -name .DS_Store 2>/dev/null | head -1)
    echo "  Size        : $((need_kb / 1048576)) GB to copy, $((free_kb / 1048576)) GB free"
    if [ "$need_kb" -gt "$free_kb" ]; then
        if [ -z "$have_files" ]; then
            die "not enough free space on the destination"
        fi
        echo "  ⚠ more than the free space, but the destination already has files — continuing (a re-run only copies what is missing)"
    fi

    PREFIX=""
    command -v caffeinate >/dev/null 2>&1 && PREFIX="caffeinate -i"
    if [ "$USE_CP" -eq 0 ] && command -v rsync >/dev/null 2>&1; then
        echo "  Copying with rsync -a ..."
        $PREFIX rsync -a "$SRC_ABS"/ "$DEST_ABS"/ || echo "  ⚠ rsync reported problems (exit $?) — the verification below shows what is missing"
    else
        echo "  Copying with cp -Rp ..."
        $PREFIX cp -Rp "$SRC_ABS"/. "$DEST_ABS"/ || echo "  ⚠ cp reported problems — the verification below shows what is missing"
    fi
fi

# ── verify ────────────────────────────────────────────────────────────────────
echo "  Verifying (dates, sizes, names) ..."
file_list "$SRC_ABS"  > "$TMP/src.txt"
file_list "$DEST_ABS" > "$TMP/dest.txt"
N_SRC=$(wc -l < "$TMP/src.txt" | tr -d ' ')
N_DEST=$(wc -l < "$TMP/dest.txt" | tr -d ' ')
echo "  Files       : $N_SRC in source, $N_DEST in destination"

if diff "$TMP/src.txt" "$TMP/dest.txt" > "$TMP/diff.txt"; then
    echo "  ✓ IDENTICAL — every file matches by name, size and date"
    exit 0
fi

N_DIFF=$(grep -c '^[<>]' "$TMP/diff.txt")
cp "$TMP/diff.txt" "$LOG"
echo "  ✗ DIFFERENCES: $N_DIFF line(s)   ( '<' = source,  '>' = destination )"
head -10 "$TMP/diff.txt" | sed 's/^/    /'
echo "  Full list saved to: $LOG"
exit 1
