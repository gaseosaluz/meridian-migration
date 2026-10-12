#!/usr/bin/env python3
"""
fits_migrator.py

Astrophotography Migration Engine
Phase 2: Reorganize files into Meridian structure

Built for Ed's Astrophotography Migration Project
Sprint 2, Phase 2 - Rev 4: per-night sessions + safe re-runs + --target

Reads metadata extracted by fits_metadata_extractor.py and reorganizes
files into the structure expected by Meridian (MacObservatory.com):

    OutputRoot/
      Year/
        Target/
          Lights/                  ← LIGHT sub-frames
          Calibration/
            Dark/                  ← DARK frames (target-specific session darks)
            Bias/                  ← BIAS frames
            Flat/                  ← FLAT frames
          Finals/
            Device/                ← Device-produced stacks (stacked-N_…)
            PixInsight/            ← Your PI processed output (populated manually)
        _Calibration/              ← Factory/shared calibration (no target)
          Dark/
          Bias/
          Flat/
        _Review/                   ← UNKNOWN frame type — needs manual review

REV 3 - NIGHT SPLITTING (default):
    Lights, device stacks and review frames are grouped into capture SESSIONS
    (one source capture folder = one night; a Seestar 'X_sub' folder is treated
    as part of folder 'X').  Each session gets a folder named for the EVENING
    the session started, in local time, plus the exposure:

        Year/Target/Lights/2025-08-06_10s/
        Year/Target/Finals/Device/2025-08-06_10s/
        Year/Target/_Review/2025-08-06_10s/      (aborted / Unknown / failed)

    Evening convention: a session starting before 12:00 local belongs to the
    previous evening's date.  Seestar times come from the local time embedded
    in the filename; if absent, the UTC header time is converted with --tz.
    DWARF header times are already local.  Use --merge-nights for the old
    single-folder behavior.

REV 4 - SAFETY:
    * Re-running --execute into the same output folder no longer makes '_2'
      duplicates: a destination file with the same size and timestamp is
      reported as 'already present' and skipped.
    * Before copying, the free disk space is checked; if the copy cannot fit
      the run refuses to start.
    * If the disk fills during a run, the run stops at once and the partial
      file is removed.
    * --target "M 31" (repeatable) limits the run to the named target(s).

USAGE:
    # Dry-run (default — no files touched, shows what WOULD happen)
    python fits_migrator.py SOURCE_ROOT OUTPUT_ROOT --summary

    # Actually copy files
    python fits_migrator.py SOURCE_ROOT OUTPUT_ROOT --execute

    # Move instead of copy (use with caution)
    python fits_migrator.py SOURCE_ROOT OUTPUT_ROOT --execute --move

    # Limit to one year or device
    python fits_migrator.py SOURCE_ROOT OUTPUT_ROOT --year 2025 --summary
    python fits_migrator.py SOURCE_ROOT OUTPUT_ROOT --device SEESTAR_S50 --summary
"""

import re
import sys
import errno
import shutil
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# Phase 1 extractor — must be in the same directory (or on PYTHONPATH)
from fits_metadata_extractor import (
    FITSMetadata,
    scan_directory,
    setup_logging,
)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# Meridian subfolder names (change here only if Meridian renames them)
FOLDER_LIGHTS       = 'Lights'
FOLDER_CALIBRATION  = 'Calibration'
FOLDER_FINALS       = 'Finals'
FOLDER_DEVICE_STACK = 'Device'       # inside Finals/
FOLDER_PI_WORK      = 'PixInsight'   # inside Finals/ — populated manually by user
FOLDER_SIRIL        = 'Siril'        # inside Finals/ — populated manually by user
FOLDER_SHARED_CALIB = '_Calibration' # factory/shared frames with no target
FOLDER_REVIEW       = '_Review'      # UNKNOWN frame type — needs human attention
FOLDER_UNKNOWN_YEAR = '_UnknownYear' # files with no parseable date
FOLDER_UNKNOWN_DATE = '_UnknownDate' # session with no usable capture time

# Rev 3 — night splitting
DEFAULT_TZ                  = 'America/Chicago'
DEFAULT_MIN_SESSION_MINUTES = 10.0
# Max allowed gap between a Seestar's filename (local) time and its header (UTC)
# time converted to the configured timezone before we warn about a mismatch.
_SEESTAR_TIME_TOLERANCE = timedelta(minutes=30)

# Filesystem characters that are unsafe across macOS / Linux / Windows
_UNSAFE_CHARS = re.compile(r'[/\\:*?"<>|]')

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logger = setup_logging(log_file='migration.log')


# ──────────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class MigrationAction:
    """Describes one planned file operation."""
    source:      Path
    dest:        Path
    operation:   str   # 'copy' | 'move' | 'skip'
    skip_reason: str   # non-empty only when operation == 'skip'
    # Metadata summary (for reporting — avoids re-reading the file)
    device:      str = ''
    frame_type:  str = ''
    target:      str = ''
    year:        str = ''
    session:       str = ''   # Rev 3: night folder label, e.g. '2025-08-06_10s'
    review_reason: str = ''   # Rev 3: non-empty when the whole session is flagged

    @property
    def is_skip(self) -> bool:
        return self.operation == 'skip'


# ──────────────────────────────────────────────────────────────────────────────
# Path helpers
# ──────────────────────────────────────────────────────────────────────────────

def sanitize_name(name: str) -> str:
    """
    Clean an astronomical target name for use as a directory component.

    Keeps spaces (e.g. 'NGC 281', 'Barnard 33') since macOS and Linux
    handle them fine and the names stay readable in Finder / Nautilus.
    Only removes characters that are outright illegal on at least one
    major OS.

    Examples:
        'NGC 281'   → 'NGC 281'
        'M42/M43'   → 'M42-M43'
        ''          → '_NoTarget'
    """
    cleaned = _UNSAFE_CHARS.sub('-', name).strip().strip('.')
    return cleaned if cleaned else '_NoTarget'


def _year_folder(meta: FITSMetadata) -> str:
    """Return the year component of the destination path."""
    if meta.observation_year:
        return str(meta.observation_year)
    # Fall back to year embedded in the source path (e.g. …/2025/…)
    for part in Path(meta.file_path).parts:
        if re.fullmatch(r'20\d{2}', part):
            return part
    return FOLDER_UNKNOWN_YEAR


def _is_device_stack(filename: str) -> bool:
    """True for device-produced stacked frames.

    Handles two naming conventions:
      DWARF   : stacked-16_…   (lowercase, hyphen separator)  ← Rev 1
      Seestar : Stacked_492_…  (any case, underscore separator) ← Rev 2
    """
    lower = filename.lower()
    return lower.startswith('stacked-') or lower.startswith('stacked_')


# ──────────────────────────────────────────────────────────────────────────────
# Night / session helpers  (Rev 3)
# ──────────────────────────────────────────────────────────────────────────────

_FILENAME_TS     = re.compile(r'(?<!\d)(\d{8})-(\d{6})(\d{3})?(?!\d)')
_FOLDER_DATE     = re.compile(r'(20\d{2})-(\d{2})-(\d{2})-\d{2}-\d{2}-\d{2}')
_SUB_TOKEN       = re.compile(r'_sub\b', re.IGNORECASE)


@dataclass
class SessionInfo:
    """One capture session (one night, one scope, one target)."""
    label:         str            # '2025-08-06_10s'
    review:        bool = False
    review_reason: str  = ''
    frames:        int  = 0       # regular light sub-frames
    integration_s: float = 0.0


def parse_filename_time(filename: str) -> Optional[datetime]:
    """Local capture time embedded in a filename like ..._20250806-213920[123]_..."""
    m = _FILENAME_TS.search(filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S')
    except ValueError:
        return None


def _header_time(meta: FITSMetadata) -> Optional[datetime]:
    s = meta.observation_date
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).strip().rstrip('Z').replace(' ', 'T'))
    except ValueError:
        return None


def _is_seestar(meta: FITSMetadata) -> bool:
    return (meta.device_type or '').upper().startswith('SEESTAR')


def local_capture_time(meta: FITSMetadata, tz_name: str = DEFAULT_TZ):
    """
    Return (local_datetime_or_None, header_conflict: bool), naive local time.

    Seestar : filename time is local -> primary.  Fallback: header is UTC,
              converted to tz_name.  header_conflict is True when both exist
              and disagree by more than 30 minutes (firmware / region change).
    Others  : header time is already local (verified for DWARF 3 / Mini);
              fallback to a time embedded in the filename.
    """
    fn_time  = parse_filename_time(meta.file_name)
    hdr_time = _header_time(meta)

    if _is_seestar(meta):
        converted = None
        if hdr_time is not None:
            converted = (hdr_time.replace(tzinfo=timezone.utc)
                         .astimezone(ZoneInfo(tz_name)).replace(tzinfo=None))
        if fn_time is not None:
            conflict = (converted is not None and not _is_device_stack(meta.file_name)
                        and abs(converted - fn_time) > _SEESTAR_TIME_TOLERANCE)
            return fn_time, conflict
        return converted, False

    return (hdr_time if hdr_time is not None else fn_time), False


def night_label(local_dt: datetime) -> str:
    """Evening convention: a capture starting before 12:00 belongs to the previous day."""
    return (local_dt - timedelta(hours=12)).strftime('%Y-%m-%d')


def normalize_session_folder(name: str) -> str:
    """'M 16_sub Seestar 50' -> 'M 16 Seestar 50' (Seestar sub-frame folder)."""
    return re.sub(r'\s+', ' ', _SUB_TOKEN.sub('', name)).strip()


def session_key(meta: FITSMetadata) -> tuple:
    p = Path(meta.file_path)
    target = sanitize_name(meta.object_name) if meta.object_name.strip() else '_NoTarget'
    return (_year_folder(meta), target, meta.device_type,
            str(p.parent.parent / normalize_session_folder(p.parent.name)))


def _exposure_label(exposures: list) -> str:
    known = [e for e in exposures if e]
    if not known:
        return ''
    return f"_{Counter(known).most_common(1)[0][0]:g}s"


def build_sessions(metas: list,
                   tz_name: str = DEFAULT_TZ,
                   min_session_minutes: float = DEFAULT_MIN_SESSION_MINUTES) -> dict:
    """
    Group LIGHT frames (regular lights and device stacks) into sessions.
    Returns {session_key: SessionInfo}.
    """
    groups: dict = {}
    for m in metas:
        if m.frame_type == 'LIGHT':
            groups.setdefault(session_key(m), []).append(m)

    sessions: dict = {}
    conflicts: list = []
    for key, members in groups.items():
        target = key[1]
        subs   = [m for m in members if not _is_device_stack(m.file_name)]
        basis  = subs if subs else members

        times = []
        for m in basis:
            t, conflict = local_capture_time(m, tz_name)
            if t is not None:
                times.append(t)
            if conflict:
                conflicts.append(m.file_name)

        info = SessionInfo(label=FOLDER_UNKNOWN_DATE, frames=len(subs))
        if times:
            first = min(times)
            info.label = night_label(first) + _exposure_label([m.exposure_time for m in basis])
            fm = _FOLDER_DATE.search(Path(key[3]).name)
            if fm:
                folder_dt = datetime(int(fm.group(1)), int(fm.group(2)), int(fm.group(3)))
                if abs((first - folder_dt).days) > 1:
                    logger.warning(
                        f"Session date mismatch for {target}: capture times say "
                        f"{first:%Y-%m-%d} but folder name says {folder_dt:%Y-%m-%d} "
                        f"({Path(key[3]).name}) — using capture times")

        exps = [m.exposure_time for m in subs if m.exposure_time is not None]
        info.integration_s = float(sum(exps))

        reasons = []
        if re.match(r'unknown', target, re.IGNORECASE):
            reasons.append('target is Unknown')
        if subs and all(m.file_name.lower().startswith('failed_') for m in subs):
            reasons.append('failed capture')
        if exps and info.integration_s < min_session_minutes * 60:
            reasons.append(f'only {info.integration_s / 60:.1f} min of exposure '
                           f'(< {min_session_minutes:g} min)')
        if reasons:
            info.review, info.review_reason = True, '; '.join(reasons)
        sessions[key] = info

    if conflicts:
        logger.warning(
            f"{len(conflicts)} Seestar file(s) have a header time that does not match "
            f"the time in the filename after converting from UTC to {tz_name} "
            f"(e.g. {conflicts[0]}). Using filename time — firmware or region "
            f"settings may have changed; please check.")
    return sessions


def build_dest_path(meta: FITSMetadata, output_root: Path,
                    session: Optional[SessionInfo] = None) -> Path:
    """
    Determine the destination directory for a single file.

    Routing table:
      LIGHT (regular)       → Year/Target/Lights/
      LIGHT (stacked-/stacked_) → Year/Target/Finals/Device/
      DARK/BIAS/FLAT        → Year/Target/Calibration/Type/  (has target)
                            → Year/_Calibration/Type/         (no target)
      UNKNOWN               → Year/Target/_Review/
                            → Year/_Review/                   (no target)

    Rev 3: when `session` is given, lights / stacks go into a per-night folder
    (Lights/<label>/, Finals/Device/<label>/) or, for flagged sessions,
    _Review/<label>/.  With session=None the Rev 2 layout is used unchanged.

    Returns the full destination path including filename.
    """
    year    = _year_folder(meta)
    target  = sanitize_name(meta.object_name) if meta.object_name.strip() else None
    ft      = meta.frame_type       # LIGHT | DARK | BIAS | FLAT | UNKNOWN
    fname   = meta.file_name
    ft_cap  = ft.title()            # 'Dark' | 'Bias' | 'Flat'

    if ft == 'LIGHT' and session is not None and session.review:
        folder = (output_root / year / (target or '_NoTarget')
                  / FOLDER_REVIEW / session.label)
    elif ft == 'LIGHT':
        if _is_device_stack(fname):
            folder = (output_root / year / (target or '_NoTarget')
                      / FOLDER_FINALS / FOLDER_DEVICE_STACK)
            if session is not None:
                folder = folder / session.label
        else:
            folder = (output_root / year / (target or '_Review')
                      / FOLDER_LIGHTS)
            if session is not None:
                folder = folder / session.label
    elif ft in ('DARK', 'BIAS', 'FLAT'):
        if target:
            folder = (output_root / year / target
                      / FOLDER_CALIBRATION / ft_cap)
        else:
            # Factory / shared calibration — no target association
            folder = output_root / year / FOLDER_SHARED_CALIB / ft_cap
    else:
        # UNKNOWN frame type → quarantine for manual review
        folder = (output_root / year / (target or '')
                  / FOLDER_REVIEW).resolve()
        # Flatten empty target segment
        folder = (output_root / year / target / FOLDER_REVIEW
                  if target else output_root / year / FOLDER_REVIEW)

    return folder / fname


def _make_unique_dest(dest: Path) -> Path:
    """
    If dest already exists, append _2, _3, … to the stem until unique.
    Prevents silent overwrites during execute mode.
    """
    if not dest.exists():
        return dest
    stem   = dest.stem
    suffix = dest.suffix
    parent = dest.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _same_file(source: Path, dest: Path) -> bool:
    """True when dest already holds a copy of source (same size, same mtime +-2 s)."""
    try:
        s, d = source.stat(), dest.stat()
    except OSError:
        return False
    return s.st_size == d.st_size and abs(s.st_mtime - d.st_mtime) <= 2


def check_free_space(plan: list, output_root: Path, margin: float = 0.02):
    """
    Will a COPY of this plan fit on the output disk?
    Returns (ok, needed_bytes, free_bytes).  Files already present are not
    counted; 'move' operations need no extra space (assumes the same disk).
    """
    needed = 0
    for a in plan:
        if a.is_skip or a.operation != 'copy':
            continue
        if _same_file(a.source, a.dest):
            continue
        try:
            needed += a.source.stat().st_size
        except OSError:
            pass
    probe = Path(output_root)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    return needed <= free * (1 - margin), needed, free


# ──────────────────────────────────────────────────────────────────────────────
# Migration planner
# ──────────────────────────────────────────────────────────────────────────────

def plan_migration(results:     list,
                   output_root: Path,
                   operation:   str  = 'copy',
                   year_filter: Optional[str]  = None,
                   device_filter: Optional[str] = None,
                   split_nights: bool  = True,
                   min_session_minutes: float = DEFAULT_MIN_SESSION_MINUTES,
                   tz_name: str = DEFAULT_TZ,
                   target_filter=None) -> list:
    """
    Build a migration plan from a list of FITSMetadata objects.

    Returns a list of MigrationAction — one per file.
    No filesystem operations are performed here.

    Args:
        results:       Output of scan_directory()
        output_root:   Root of the Meridian output tree
        operation:     'copy' or 'move'
        year_filter:   If set, only include files from this year (e.g. '2025')
        device_filter: If set, only include files from this device
        split_nights:  Rev 3 — one folder per capture night (default True).
                       False restores the Rev 2 merged layout.
        min_session_minutes: sessions with less total exposure go to _Review
        tz_name:       timezone used to convert Seestar UTC header times
        target_filter: Rev 4 — a target name or list of names (case-insensitive);
                       only those targets are planned (shared calibration
                       frames without a target are excluded)
    """
    if isinstance(target_filter, str):
        target_filter = [target_filter]
    wanted = ({sanitize_name(t).lower() for t in target_filter}
              if target_filter else None)

    entries: list = []      # ('skip', MigrationAction) | ('go', FITSMetadata)

    for meta in results:
        source = Path(meta.file_path)

        # ── Filters ───────────────────────────────────────────────────────────
        if year_filter and _year_folder(meta) != year_filter:
            continue
        if device_filter and meta.device_type != device_filter.upper():
            continue
        if wanted is not None and (
                not meta.object_name.strip()
                or sanitize_name(meta.object_name).lower() not in wanted):
            continue

        # ── Skip unreadable files ─────────────────────────────────────────────
        if not meta.is_valid:
            entries.append(('skip', MigrationAction(
                source=source, dest=Path('/dev/null'),
                operation='skip', skip_reason='File could not be read by FITS extractor',
                device=meta.device_type, frame_type=meta.frame_type,
                target=meta.object_name, year=_year_folder(meta),
            )))
            continue

        entries.append(('go', meta))

    sessions: dict = {}
    if split_nights:
        sessions = build_sessions([x for kind, x in entries if kind == 'go'],
                                  tz_name=tz_name,
                                  min_session_minutes=min_session_minutes)

    plan: list = []
    for kind, item in entries:
        if kind == 'skip':
            plan.append(item)
            continue
        meta = item
        session = sessions.get(session_key(meta)) if meta.frame_type == 'LIGHT' else None
        dest = build_dest_path(meta, output_root, session)

        plan.append(MigrationAction(
            source=Path(meta.file_path), dest=dest,
            operation=operation, skip_reason='',
            device=meta.device_type, frame_type=meta.frame_type,
            target=meta.object_name, year=_year_folder(meta),
            session=session.label if session else '',
            review_reason=session.review_reason if session else '',
        ))

    return plan


# ──────────────────────────────────────────────────────────────────────────────
# Dry-run report
# ──────────────────────────────────────────────────────────────────────────────

def print_plan(plan: list, verbose: bool = False) -> None:
    """
    Print the migration plan.

    verbose=False (default): print one line per destination directory
                             with file count — useful for 44K-file runs.
    verbose=True:            print every individual file operation.
    """
    if verbose:
        for action in plan:
            tag = '  SKIP' if action.is_skip else action.operation.upper()
            reason = f'  [{action.skip_reason}]' if action.is_skip else ''
            print(f"  {tag:6s}  {action.source.name}  →  {action.dest}{reason}")
    else:
        # Group by destination directory
        dirs: dict = {}
        skips: list = []
        for action in plan:
            if action.is_skip:
                skips.append(action)
            else:
                key = str(action.dest.parent)
                dirs[key] = dirs.get(key, 0) + 1

        print(f"\n  {'DESTINATION DIRECTORY':<65}  {'FILES':>6}")
        print(f"  {'─' * 65}  {'─' * 6}")
        for dest_dir in sorted(dirs):
            count = dirs[dest_dir]
            # Trim output_root prefix for readability
            print(f"  {dest_dir:<65}  {count:>6}")

        if skips:
            print(f"\n  SKIPPED: {len(skips)} file(s)")
            for s in skips[:10]:
                print(f"    • {s.source.name}: {s.skip_reason}")
            if len(skips) > 10:
                print(f"    … and {len(skips) - 10} more (see migration.log)")


def print_summary(plan: list, dry_run: bool = True) -> None:
    """Print aggregate statistics for the migration plan."""
    mode_label = '  ⚠  DRY-RUN — no files have been touched' if dry_run else '  ✓  EXECUTE MODE'
    total   = len(plan)
    skipped = sum(1 for a in plan if a.is_skip)
    active  = total - skipped

    devices: dict = {}
    frames:  dict = {}
    years:   dict = {}
    targets: set  = set()

    for a in plan:
        if a.is_skip:
            continue
        devices[a.device]     = devices.get(a.device, 0) + 1
        frames[a.frame_type]  = frames.get(a.frame_type, 0) + 1
        years[a.year]         = years.get(a.year, 0) + 1
        if a.target:
            targets.add(a.target)

    W = 68
    print('\n' + '=' * W)
    print('  MIGRATION SUMMARY')
    print('=' * W)
    print(f"  {mode_label}")
    print(f"  {'─' * (W - 2)}")
    print(f"  Total planned : {active}  ({skipped} skipped)")
    print(f"  Devices       : {dict(sorted(devices.items()))}")
    print(f"  Frame types   : {dict(sorted(frames.items()))}")
    print(f"  Years         : {dict(sorted(years.items()))}")
    print(f"  Targets       : {len(targets)} unique")
    for t in sorted(targets):
        print(f"    • {t}")

    # Rev 3 — night sessions and review flags
    night_sessions: dict = {}
    for a in plan:
        if not a.is_skip and a.session:
            k = (a.year, a.target, a.device, a.session, a.review_reason)
            night_sessions[k] = night_sessions.get(k, 0) + 1
    if night_sessions:
        flagged = {k: n for k, n in night_sessions.items() if k[4]}
        print(f"  {'─' * (W - 2)}")
        print(f"  Night sessions: {len(night_sessions)}  "
              f"({len(flagged)} flagged for review)")
        for (year, target, device, label, reason), n in sorted(flagged.items()):
            print(f"    ⚑ {year}/{target}/_Review/{label}  [{device}]  "
                  f"{n} file(s) — {reason}")
    print('=' * W)


# ──────────────────────────────────────────────────────────────────────────────
# Executor
# ──────────────────────────────────────────────────────────────────────────────

def execute_plan(plan:     list,
                 dry_run:  bool = True,
                 output_root: Optional[Path] = None) -> dict:
    """
    Execute or simulate the migration plan.

    In dry-run mode: validates paths and counts, no disk I/O.
    In execute mode: creates directories and copies/moves files.

    Returns a stats dict: {'copied': N, 'moved': N, 'skipped': N, 'errors': N}
    """
    stats = {'copied': 0, 'moved': 0, 'skipped': 0, 'errors': 0,
             'already_present': 0, 'no_space': False}

    # Create the PixInsight placeholder so users see it immediately
    if not dry_run and output_root is not None:
        _create_pi_placeholders(plan, output_root)

    for action in plan:

        if action.is_skip:
            stats['skipped'] += 1
            logger.debug(f"SKIP {action.source.name}: {action.skip_reason}")
            continue

        if dry_run:
            # Validate source exists; destination parent would be created
            if not action.source.exists():
                logger.warning(f"DRY-RUN: source missing: {action.source}")
                stats['errors'] += 1
            elif action.operation == 'copy' and _same_file(action.source, action.dest):
                stats['already_present'] += 1
            else:
                key = 'copied' if action.operation == 'copy' else 'moved'
                stats[key] = stats.get(key, 0) + 1
            continue

        # ── Live execution ────────────────────────────────────────────────────
        dest = None
        try:
            if action.operation == 'copy' and _same_file(action.source, action.dest):
                stats['already_present'] += 1
                logger.debug(f"PRESENT {action.dest}  (already copied, skipped)")
                continue

            dest = _make_unique_dest(action.dest)
            dest.parent.mkdir(parents=True, exist_ok=True)

            if action.operation == 'copy':
                shutil.copy2(action.source, dest)
                stats['copied'] += 1
                logger.debug(f"COPY  {action.source}  →  {dest}")
            elif action.operation == 'move':
                shutil.move(str(action.source), dest)
                stats['moved'] += 1
                logger.debug(f"MOVE  {action.source}  →  {dest}")

        except OSError as exc:
            stats['errors'] += 1
            if exc.errno == errno.ENOSPC:
                if dest is not None:
                    dest.unlink(missing_ok=True)      # remove the partial file
                stats['no_space'] = True
                logger.error("DISK FULL — stopping. Partial file removed. "
                             "Free some space and re-run the same command: "
                             "files already copied will be skipped.")
                break
            logger.error(f"ERROR {action.operation} {action.source}: {exc}")
        except Exception as exc:
            stats['errors'] += 1
            logger.error(f"ERROR {action.operation} {action.source}: {exc}")

    return stats


def _create_pi_placeholders(plan: list, output_root: Path) -> None:
    """
    Create empty Finals/PixInsight/ and Finals/Siril/ directories for each
    target that has a Finals/Device/ entry — so the post-processing workspaces
    are ready to use immediately after migration.
    """
    finals_dirs: set = set()
    for action in plan:
        if action.is_skip:
            continue
        parts = action.dest.parts
        if FOLDER_FINALS in parts and FOLDER_DEVICE_STACK in parts:
            # …/Finals/Device/[night/]filename  ->  …/Finals/
            idx = parts.index(FOLDER_FINALS)
            finals_dirs.add(Path(*parts[:idx + 1]))
    for finals_dir in finals_dirs:
        for subfolder in (FOLDER_PI_WORK, FOLDER_SIRIL):
            placeholder = finals_dir / subfolder
            placeholder.mkdir(parents=True, exist_ok=True)
            logger.debug(f"MKDIR {placeholder}  (post-processing workspace)")


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description='Migrate astrophotography files into Meridian structure.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview what would happen (safe — no files touched)
  python fits_migrator.py ~/Astronomy/Astrocapture ~/Astronomy/Meridian --summary

  # Preview with per-file detail
  python fits_migrator.py ~/Astronomy/Astrocapture ~/Astronomy/Meridian --verbose

  # Only look at 2025 data
  python fits_migrator.py ~/Astronomy/Astrocapture ~/Astronomy/Meridian --year 2025 --summary

  # Actually run the copy
  python fits_migrator.py ~/Astronomy/Astrocapture ~/Astronomy/Meridian --execute

  # Move instead of copy (use with care — modifies source tree)
  python fits_migrator.py ~/Astronomy/Astrocapture ~/Astronomy/Meridian --execute --move
        """
    )
    parser.add_argument('source',      help='Root of your current Astrocapture directory')
    parser.add_argument('output',      help='Root of the new Meridian directory')
    parser.add_argument('--execute',   action='store_true',
                        help='Actually perform the migration (default: dry-run)')
    parser.add_argument('--move',      action='store_true',
                        help='Move files instead of copying (requires --execute)')
    parser.add_argument('--year',      help='Only migrate files from this year, e.g. 2025')
    parser.add_argument('--device',    help='Only migrate files from this device, e.g. SEESTAR_S50')
    parser.add_argument('--target', action='append', metavar='NAME',
                        help='Only this target, e.g. --target "M 31" '
                             '(repeat for several; shared calibration is excluded)')
    parser.add_argument('--merge-nights', action='store_true',
                        help='Put all nights of a target in one folder (old behavior)')
    parser.add_argument('--min-session-minutes', type=float,
                        default=DEFAULT_MIN_SESSION_MINUTES,
                        help='Sessions with less total exposure go to _Review '
                             f'(default {DEFAULT_MIN_SESSION_MINUTES:g})')
    parser.add_argument('--tz', default=DEFAULT_TZ,
                        help='Timezone for converting Seestar UTC header times '
                             f'(default {DEFAULT_TZ})')
    parser.add_argument('--summary',   action='store_true',
                        help='Show summary statistics only (no per-directory listing)')
    parser.add_argument('--verbose',   action='store_true',
                        help='Show every individual file operation')
    args = parser.parse_args()

    try:
        ZoneInfo(args.tz)
    except Exception:
        parser.error(f"unknown timezone '{args.tz}' (example: America/Chicago)")

    source_root = Path(args.source).expanduser()
    output_root = Path(args.output).expanduser()
    dry_run     = not args.execute
    operation   = 'move' if (args.move and args.execute) else 'copy'

    if args.move and not args.execute:
        print("  ⚠  --move has no effect without --execute. Running dry-run.")

    print(f"\n  Mode      : {'DRY-RUN (use --execute to actually run)' if dry_run else 'EXECUTE'}")
    print(f"  Operation : {operation.upper()}")
    print(f"  Source    : {source_root}")
    print(f"  Output    : {output_root}")
    if args.year:   print(f"  Year      : {args.year}")
    if args.device: print(f"  Device    : {args.device}")
    if args.target: print(f"  Target(s) : {', '.join(args.target)}")
    print(f"  Nights    : {'MERGED (old layout)' if args.merge_nights else 'SPLIT per night, tz ' + args.tz}")

    # ── Scan ──────────────────────────────────────────────────────────────────
    print(f"\n  Scanning {source_root} …")
    results = scan_directory(source_root)

    # ── Plan ──────────────────────────────────────────────────────────────────
    plan = plan_migration(
        results, output_root,
        operation=operation,
        year_filter=args.year,
        device_filter=args.device,
        split_nights=not args.merge_nights,
        min_session_minutes=args.min_session_minutes,
        tz_name=args.tz,
        target_filter=args.target,
    )

    # ── Report ────────────────────────────────────────────────────────────────
    if not args.summary:
        print_plan(plan, verbose=args.verbose)
    print_summary(plan, dry_run=dry_run)

    # ── Free-space check (copy mode only) ─────────────────────────────────────
    if not dry_run and operation == 'copy':
        ok, needed, free = check_free_space(plan, output_root)
        gb = 1024 ** 3
        print(f"\n  Space needed : {needed / gb:,.1f} GB   free: {free / gb:,.1f} GB")
        if not ok:
            print("  ✗  Not enough free space for this copy — nothing was copied.")
            print("     Free some space, or limit the run with --target / --year.")
            sys.exit(2)

    # ── Execute ───────────────────────────────────────────────────────────────
    stats = execute_plan(plan, dry_run=dry_run, output_root=output_root)

    if not dry_run:
        print(f"\n  Done — copied: {stats.get('copied', 0)}, "
              f"already present: {stats.get('already_present', 0)}, "
              f"moved: {stats.get('moved', 0)}, "
              f"skipped: {stats['skipped']}, "
              f"errors: {stats['errors']}")
        if stats.get('no_space'):
            print("  ✗  DISK FULL — run stopped. Free space and re-run the same command; "
                  "files already copied are skipped.")
        elif stats['errors']:
            print("  ⚠  Errors occurred — check migration.log")


if __name__ == '__main__':
    main()
