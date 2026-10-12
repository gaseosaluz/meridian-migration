#!/usr/bin/env python3
"""
session_report.py  --  READ-ONLY preview of per-night sessions.

Groups LIGHT frames by (year, target, device, source capture folder) and
prints one line per session.  Nothing is copied, moved, or written.
Flags sessions of the same target+device whose time ranges overlap or sit
within 12 h of each other (possible one night split across two folders,
e.g. a Seestar 'M 31' folder plus its 'M 31_sub' folder).

Usage:
    python session_report.py /Users/edm/Astronomy/Astrocapture/2025
    python session_report.py /Users/edm/Astronomy/Astrocapture/2025 --target "M 31"
"""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from fits_migrator import sanitize_name, _year_folder, version_banner

NEAR = timedelta(hours=12)


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace('Z', ''))
    except ValueError:
        return None


def group_sessions(results, target_filter=None):
    """Return {(year, target, device): [session dict, ...]} for LIGHT frames."""
    groups = defaultdict(lambda: defaultdict(list))
    for m in results:
        if not m.is_valid or m.frame_type != 'LIGHT':
            continue
        target = sanitize_name(m.object_name) if m.object_name else '_NoTarget'
        if target_filter and target.lower() != target_filter.lower():
            continue
        key = (_year_folder(m), target, m.device_type)
        groups[key][str(Path(m.file_path).parent)].append(m)

    out = {}
    for key, folders in groups.items():
        sessions = []
        for folder, metas in folders.items():
            times = [t for t in (_parse(x.observation_date) for x in metas) if t]
            sessions.append({
                'folder': folder,
                'frames': len(metas),
                'first': min(times) if times else None,
                'last': max(times) if times else None,
            })
        sessions.sort(key=lambda s: (s['first'] is None, s['first'] or datetime.min))
        out[key] = sessions
    return out


def find_close_pairs(sessions):
    """Indices (i, j) of sessions whose ranges overlap or are within NEAR."""
    pairs = []
    for i in range(len(sessions)):
        for j in range(i + 1, len(sessions)):
            a, b = sessions[i], sessions[j]
            if not (a['first'] and b['first']):
                continue
            gap = max(a['first'], b['first']) - min(a['last'], b['last'])
            if gap < NEAR:
                pairs.append((i, j))
    return pairs


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('source')
    ap.add_argument('--target', help='Only this target, e.g. "M 31"')
    ap.add_argument('--version', action='version', version=version_banner())
    args = ap.parse_args()

    from fits_metadata_extractor import scan_directory
    results = scan_directory(Path(args.source))
    grouped = group_sessions(results, args.target)

    print('\n' + '=' * 78)
    print(f'  SESSION PREVIEW (read-only)   {version_banner()}')
    print('=' * 78)
    for (year, target, device), sessions in sorted(grouped.items()):
        multi = '  <-- multi-session' if len(sessions) > 1 else ''
        print(f'\n{year} / {target} / {device}: {len(sessions)} session(s){multi}')
        close = find_close_pairs(sessions)
        for s in sessions:
            f = s['first'].strftime('%Y-%m-%d %H:%M') if s['first'] else '?'
            l = s['last'].strftime('%Y-%m-%d %H:%M') if s['last'] else '?'
            print(f"   {s['frames']:5d} frames  {f} -> {l}  {Path(s['folder']).name}")
        for i, j in close:
            print(f'   !! sessions {i+1} and {j+1} overlap or are <12h apart '
                  f'(same night split across folders?)')
    print()


if __name__ == '__main__':
    main()
