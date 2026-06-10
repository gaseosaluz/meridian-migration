#!/usr/bin/env python3
"""
fits_migrator.py

Astrophotography Migration Engine
Phase 2: Reorganize files into Meridian structure

Built for Ed's Astrophotography Migration Project
Sprint 2, Phase 2 - Rev 1

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
import shutil
import logging
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
    """True for device-produced stacked frames (e.g. stacked-16_…)."""
    return filename.lower().startswith('stacked-')


def build_dest_path(meta: FITSMetadata, output_root: Path) -> Path:
    """
    Determine the destination directory for a single file.

    Routing table:
      LIGHT (regular)       → Year/Target/Lights/
      LIGHT (stacked-*)     → Year/Target/Finals/Device/
      DARK/BIAS/FLAT        → Year/Target/Calibration/Type/  (has target)
                            → Year/_Calibration/Type/         (no target)
      UNKNOWN               → Year/Target/_Review/
                            → Year/_Review/                   (no target)

    Returns the full destination path including filename.
    """
    year    = _year_folder(meta)
    target  = sanitize_name(meta.object_name) if meta.object_name.strip() else None
    ft      = meta.frame_type       # LIGHT | DARK | BIAS | FLAT | UNKNOWN
    fname   = meta.file_name
    ft_cap  = ft.title()            # 'Dark' | 'Bias' | 'Flat'

    if ft == 'LIGHT':
        if _is_device_stack(fname):
            folder = (output_root / year / (target or '_NoTarget')
                      / FOLDER_FINALS / FOLDER_DEVICE_STACK)
        else:
            folder = (output_root / year / (target or '_Review')
                      / FOLDER_LIGHTS)
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


# ──────────────────────────────────────────────────────────────────────────────
# Migration planner
# ──────────────────────────────────────────────────────────────────────────────

def plan_migration(results:     list,
                   output_root: Path,
                   operation:   str  = 'copy',
                   year_filter: Optional[str]  = None,
                   device_filter: Optional[str] = None) -> list:
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
    """
    plan: list = []

    for meta in results:
        source = Path(meta.file_path)

        # ── Filters ───────────────────────────────────────────────────────────
        if year_filter and _year_folder(meta) != year_filter:
            continue
        if device_filter and meta.device_type != device_filter.upper():
            continue

        # ── Skip unreadable files ─────────────────────────────────────────────
        if not meta.is_valid:
            plan.append(MigrationAction(
                source=source, dest=Path('/dev/null'),
                operation='skip', skip_reason='File could not be read by FITS extractor',
                device=meta.device_type, frame_type=meta.frame_type,
                target=meta.object_name, year=_year_folder(meta),
            ))
            continue

        dest = build_dest_path(meta, output_root)

        plan.append(MigrationAction(
            source=source, dest=dest,
            operation=operation, skip_reason='',
            device=meta.device_type, frame_type=meta.frame_type,
            target=meta.object_name, year=_year_folder(meta),
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
    stats = {'copied': 0, 'moved': 0, 'skipped': 0, 'errors': 0}

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
            else:
                key = 'copied' if action.operation == 'copy' else 'moved'
                stats[key] = stats.get(key, 0) + 1
            continue

        # ── Live execution ────────────────────────────────────────────────────
        try:
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
        if not action.is_skip and FOLDER_DEVICE_STACK in str(action.dest):
            # action.dest is …/Finals/Device/filename — parent.parent = …/Finals/
            finals_dirs.add(action.dest.parent.parent)
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
    parser.add_argument('--summary',   action='store_true',
                        help='Show summary statistics only (no per-directory listing)')
    parser.add_argument('--verbose',   action='store_true',
                        help='Show every individual file operation')
    args = parser.parse_args()

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

    # ── Scan ──────────────────────────────────────────────────────────────────
    print(f"\n  Scanning {source_root} …")
    results = scan_directory(source_root)

    # ── Plan ──────────────────────────────────────────────────────────────────
    plan = plan_migration(
        results, output_root,
        operation=operation,
        year_filter=args.year,
        device_filter=args.device,
    )

    # ── Report ────────────────────────────────────────────────────────────────
    if not args.summary:
        print_plan(plan, verbose=args.verbose)
    print_summary(plan, dry_run=dry_run)

    # ── Execute ───────────────────────────────────────────────────────────────
    stats = execute_plan(plan, dry_run=dry_run, output_root=output_root)

    if not dry_run:
        print(f"\n  Done — copied: {stats.get('copied', 0)}, "
              f"moved: {stats.get('moved', 0)}, "
              f"skipped: {stats['skipped']}, "
              f"errors: {stats['errors']}")
        if stats['errors']:
            print("  ⚠  Errors occurred — check migration.log")


if __name__ == '__main__':
    main()
