#!/usr/bin/env python3
"""
new_names.py -- READ-ONLY. Which light frames in a download folder are not yet in your archive?

Compares FILE NAMES only (light-frame names contain the capture time to the millisecond,
so a name match is strong evidence of the same frame).  Calibration files (dark_, bias_,
flat_, raw_) are skipped because their names repeat across sessions.

Usage:
    python new_names.py NEW_FOLDER ARCHIVE_FOLDER [ARCHIVE_FOLDER ...]
e.g.
    python new_names.py /Volumes/HD4/Astro_New_Captures/Astro_new_DWARF \
        /Volumes/HD4/Astronomy/Meridian /Volumes/HD4/Astronomy/Meridian_Review

Writes nothing, changes nothing, runs no other programs.
"""
import os
import re
import sys
from collections import Counter

FITS = ('.fit', '.fits')
SKIP_PREFIX = ('dark_', 'bias_', 'flat_', 'raw_')
STAMP = re.compile(r'^(?P<head>.*)_(?P<day>\d{8})-\d{6,9}')


def names_under(root, skip_calibration=False):
    found = []
    for folder, _dirs, files in os.walk(root):
        for f in files:
            if f.lower().endswith(FITS) and not (skip_calibration and f.lower().startswith(SKIP_PREFIX)):
                found.append(f)
    return found


def label(name):
    m = STAMP.match(name)
    return f"{m.group('head')}  {m.group('day')}" if m else f"(no time in name)  {name}"


def new_light_frames(new_folder, archive_folders):
    have = set()
    for a in archive_folders:
        have.update(names_under(a))
    new = set(names_under(new_folder, skip_calibration=True))
    return sorted(new - have), len(new)


def main(argv):
    if len(argv) < 3 or any(a in ('-h', '--help') for a in argv[1:]):
        print(__doc__)
        return 2
    for p in argv[1:]:
        if not os.path.isdir(p):
            print(f"  ✗ folder not found: {p}", file=sys.stderr)
            return 2
    missing, total = new_light_frames(argv[1], argv[2:])
    print(f"  Light frames in the new folder : {total}")
    print(f"  Not found in the archive       : {len(missing)}")
    for key, n in sorted(Counter(label(m) for m in missing).items()):
        print(f"    {n:6d}  {key}")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
