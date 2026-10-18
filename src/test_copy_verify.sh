#!/bin/sh
# Tests for copy_verify.sh — run:  sh test_copy_verify.sh
HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT="$HERE/copy_verify.sh"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$3', got '$2')"; fi; }

W=$(mktemp -d); cd "$W" || exit 1
mkdir -p "src/DWARF Mini/March 2026/session one" "src/2026"
echo "alpha"    > "src/DWARF Mini/March 2026/session one/frame 1.fits"
echo "beta beta" > "src/2026/frame_2.fits"
touch -t 202602281857 "src/2026/frame_2.fits"
echo ds > "src/.DS_Store"

# 1 copy a tree with spaces in names
sh "$SCRIPT" src dest > out1.txt 2>&1; check "copy exits 0" $? 0
grep -q "IDENTICAL" out1.txt && ok "copy reports IDENTICAL" || bad "copy reports IDENTICAL"
[ -f "dest/DWARF Mini/March 2026/session one/frame 1.fits" ] && ok "nested file with spaces copied" || bad "nested file with spaces copied"

# 2 modification dates preserved
A=$(ls -l --time-style=+%Y%m%d%H%M "src/2026/frame_2.fits" 2>/dev/null | awk '{print $6}')
B=$(ls -l --time-style=+%Y%m%d%H%M "dest/2026/frame_2.fits" 2>/dev/null | awk '{print $6}')
[ -z "$A" ] && { A=$(stat -f %m src/2026/frame_2.fits); B=$(stat -f %m dest/2026/frame_2.fits); }
check "modification date preserved" "$B" "$A"

# 3 re-run is safe
sh "$SCRIPT" src dest > out2.txt 2>&1; check "re-run exits 0" $? 0

# 4 .DS_Store on one side only is ignored
echo x > "dest/2026/.DS_Store"
sh "$SCRIPT" --verify-only src dest > out3.txt 2>&1; check ".DS_Store ignored" $? 0

# 5 a changed file is detected
echo "much longer content now" > "dest/2026/frame_2.fits"
sh "$SCRIPT" --verify-only src dest > out4.txt 2>&1; check "difference detected exit 1" $? 1
grep -q "DIFFERENCES" out4.txt && ok "difference message shown" || bad "difference message shown"
ls copy_verify_*.log >/dev/null 2>&1 && ok "difference log written" || bad "difference log written"

# 6 a missing file is detected
cp -p "src/2026/frame_2.fits" "dest/2026/frame_2.fits"; rm "dest/DWARF Mini/March 2026/session one/frame 1.fits"
sh "$SCRIPT" --verify-only src dest > out5.txt 2>&1; check "missing file detected" $? 1

# 7 re-run repairs a missing file
sh "$SCRIPT" src dest > out6.txt 2>&1; check "re-run repairs missing file" $? 0

# 7b --use-cp forces cp and still verifies
rm -rf destcp; sh "$SCRIPT" --use-cp src destcp > outcp.txt 2>&1; check "--use-cp exits 0" $? 0
grep -q "Copying with cp -Rp" outcp.txt && ok "--use-cp uses cp" || bad "--use-cp uses cp"
grep -q "IDENTICAL" outcp.txt && ok "--use-cp verified" || bad "--use-cp verified"

# 8 refuses placeholders, missing source, nested folders
sh "$SCRIPT" src "/Volumes/<your SSD name>/x" > o.txt 2>&1; check "placeholder refused" $? 2
grep -q "placeholder" o.txt && ok "placeholder message" || bad "placeholder message"
[ -d "/Volumes/<your SSD name>" ] && bad "placeholder folder was NOT created" || ok "placeholder folder was NOT created"
sh "$SCRIPT" nosuch dest2 > o.txt 2>&1; check "missing source refused" $? 2
[ -d dest2 ] && bad "no destination created for a bad source" || ok "no destination created for a bad source"
sh "$SCRIPT" src src/inside > o.txt 2>&1; check "destination inside source refused" $? 2
sh "$SCRIPT" src src > o.txt 2>&1; check "same folder refused" $? 2
sh "$SCRIPT" src/2026 src > o.txt 2>&1; check "source inside destination refused" $? 2
sh "$SCRIPT" > o.txt 2>&1; check "no arguments shows usage (exit 2)" $? 2

cd /; rm -rf "$W"
echo "  $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
