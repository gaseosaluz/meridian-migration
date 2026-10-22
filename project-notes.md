# Project notes — astrophotography migration tools

Design notes for the tools in `src/`. Written at version **0.9.1** (2026-10). It records
*what* the tools do, *why* they do it that way, and what is still unknown, so the project
can be picked up later by you, or by any assistant, without the original chat.
Statements marked **(unconfirmed)** are guesses, not facts.

## 1. Purpose

Reorganize personal smart-telescope FITS captures into the folder structure the Meridian app
(MacObservatory.com) scans, without ever modifying the originals.

- **Scopes:** DWARF 3, DWARF Mini, Seestar S50, Seestar S30 Pro (first used 2026-10).
- **Machines:** `kim1` = laptop (travel, single targets); `tumi` = Mac Studio (main storage and work).
- **Working style:** small increments, tests first, **dry run by default**, copy never move,
  verify before deleting anything.

## 2. Files and versions

One shared version number for all files (see `RELEASING.md`; tests fail if they differ).

| File | Role |
|---|---|
| `fits_metadata_extractor.py` | Reads FITS headers, identifies device and frame type, flags odd headers/dates/truncation. `surprises.log` is its log. |
| `fits_migrator.py` | Builds a plan (dry run) and copies files into the Meridian layout. Writes `migration.log` (dry run) or `migration_<time>.log` (execute, kept). |
| `session_report.py` | Read-only listing of capture sessions per target. |
| `copy_verify.sh` | Copy a tree keeping file dates, then verify every file by name, size and date. `--verify-only` just compares. |
| `test_*.py`, `test_copy_verify.sh` | Tests (`pytest -q` and `sh test_copy_verify.sh`). |
| `CHANGELOG.md`, `RELEASING.md` | What changed per version; how to cut a release (tag, archive source, freeze environment). |

## 3. Layout

**Source (never modified):** `Astrocapture/<Year>/…/<capture folder>/<frames>` — one capture
folder per session. Real data: `/Users/edm/Astronomy/Astrocapture/{2025,2026}` (2025 = 29,202
files, 38 targets; 2026 = 18,944 files, 22 targets, includes some late-2025 sessions).

**Output (what Meridian watches):**
```
Meridian/
  <Year>/
    <Target>/
      Lights/<YYYY-MM-DD>_<exposure>/      regular light frames, one folder per night
      Calibration/Dark|Bias|Flat/          calibration frames that have a target
      Finals/Device/<night>/               scope-made stacks (stacked-N_… / Stacked_N_…)
      Finals/PixInsight/  Finals/Siril/    empty folders for your own processed work
    _Calibration/Dark|Bias|Flat/           factory / shared calibration (no target)

Meridian_Review/                           OUTSIDE the watched folder (sibling of the output)
  <Year>/<Target>/<night>/                 failed, truncated, aborted, Unknown frames
```
Examples of the night label: `2025-07-28_15s` (evening date, exposure), `2026-02-28_3.125ms` (Moon).

## 4. Design decisions and why

| Decision | Reason |
|---|---|
| Copy, not move; dry run is the default | The originals are the only irreplaceable thing. |
| One folder per **night** (evening convention: a session starting before 12:00 local belongs to the previous evening's date) | All-night sessions run past midnight; the label follows when the user started. |
| A **night = one source capture folder** (Seestar `X_sub` is merged with `X`) | Capture folders never span two nights in this data. |
| Exposure is part of the night label | Matters for stacking and for matching darks. |
| Seestar night/time from the **filename** (local); header (UTC) as fallback; warn if they disagree | Seestar headers are UTC, filenames local; firmware may change. |
| DWARF header times are used as local time | Checked against Austin daylight data. |
| Header dates beat folder names | A DWARF 3 folder named 2025-08-05 held frames dated 2025-07-25; the other DWARF 3 sessions prove the header date is right. |
| Implausible header years (before 2015 / after next year) are ignored | The DWARF Mini writes `2038-01-01…` when its clock is not set. Year then comes from the folder path, time from the filename. |
| Year folder follows the session's evening date | A New Year's Eve session stays in one year. |
| **Review material goes outside the watched folder** | Meridian counts every frame under a watched folder, `_Review` included. A test showed 18.6 h / 3,231 frames in Meridian against 15.6 h / 2,635 usable after the change. |
| Review reasons: `failed_*` frames, truncated files, sessions under 10 min of exposure, target `Unknown…`, all-failed sessions | Aborted runs and failed frames should not count toward integration time. |
| Solar-system targets (Moon, Sun, planets, comets) are exempt from the 10-minute rule | Short exposures are normal for them. |
| Failed frames never count toward a night's exposure; summary suggests discarding a night at ≥50% failed | The user discards mostly-failed sessions by habit. Nothing is ever deleted by the tools. |
| Re-runs are safe: a destination file with the same size and date (±2 s) is "already present" | The tools can be interrupted and re-run; the first full-copy attempt without this created 24,648 `_2` duplicates. |
| Files with the same name from different sessions each keep their own copy (`_2`, `_3`) | DWARF calibration files repeat names across sessions. |
| Free-space check before copying; clean stop if the disk fills | A full disk partway through a long copy corrupts the plan. |
| Stable machine-readable things are config, not code | `DEVICE_SIGNATURES` in the extractor: add a scope there when firmware renames. |

## 5. What is known about Meridian

From its help ("Scanning and Object Identification") and from tests on real data:

- Scans **recursively**; subfolders (e.g. `Lights/<date>/`) belong to the same target. **Confirmed**
  by test: 2,635 frames in `Lights/<date>/` were all counted.
- Identifies targets by **RA/Dec first**, then folder name; it never uses the `OBJECT` header.
- **Read-only** — never moves or modifies files.
- Skips FITS written by Siril / PixInsight / Astro Pixel Processor (by the `PROGRAM`/`SOFTWARE` header).
- JPG/PNG/TIF files in a target's folders can be used as final images (hero image = a JPG named
  exactly like the target).
- The developer said `Finals/` is ignored. **Confirmed** (5 stacks in `Finals/` were not counted).
- DWARF stacks (`stacked-N_…`) were not counted even inside `_Review`. Seestar stacks may be counted
  **(unconfirmed)**: an M 16 test counted 2 frames for a sub plus a stack.
- A moved or renamed file is treated as a **new** file; deleted data stays as "missing".
  Use **Stop Watching & Delete Data** (or Settings → Cache → Rebuild Catalog) after reorganizing.
  Add the final folder to Meridian only after it is in its final location.
- Mosaic warning ("multiple targets"): frames in one folder pointing more than 10° apart. A failed
  or interrupted mosaic slew can trigger it. The developer was working on mosaic handling.
- Known Meridian beta issues reported to the developer: DWARF Mini was not told apart from DWARF 3.

## 6. Scope and data quirks

- **DWARF 3** — `TELESCOP` is `DWARFIII` (older firmware) or `DWARF 3` (1.4+); 3856×2180; header
  times are local; scope stacks are `stacked-N_…`; failed frames are `failed_…`; calibration files
  have stripped headers (`bias_gain_2_bin_1.fits`, `dark_exp_…`, `flat_…`), identified by path and name.
  Session folder names look like `DWARF_RAW_TELE_<target>_EXP_15_GAIN_60_<timestamp>`.
- **DWARF Mini** — `TELESCOP` is `DWARF mini` (lowercase m); 1920×1080; unsynced clock gives `2038-01-01`.
  Check the clock in the app before a session. `Burst` folders hold JPGs.
- **Seestar S50** — `CREATOR` = `ZWO Seestar S50`, `TELESCOP` = `S50_<serial>`; 1080×1920; header
  `DATE-OBS` is **UTC** while filenames are local; frames `Light_…`, stacks `Stacked_N_…`; each
  target has two folders `X` and `X_sub`; extra headers `IMAGEW`, `IMAGEH`, `WIDECAM`.
- **Seestar S30 Pro** — `TELESCOP` = `S30 Pro_<serial>`, `CREATOR` = `ZWO Seestar S30 Pro`;
  2160×3840; extra headers `BIAS`, `DATE-EXP`; UTC header like the S50. A new "Region of the World"
  setting appeared in the Seestar app; its effect on timestamps is **unknown**. The migrator warns
  if filename and header times stop agreeing.
- **Data problems seen:** a few truncated files (re-download if possible); finder failed to copy
  103 `Burst` JPGs whose permissions were `-rwx---r--` (FITS files were not affected).
  Fix on copies only: `chmod 644` files, `755` folders — never on the originals.
- **Duplicate finders** flagged "duplicates" between different nights. They match by *similar
  image content*; a name check found 0 shared light frames between 2025 and 2026.

## 7. Everyday commands

```bash
# Dry run (writes nothing; prints a "Plan check" line)
python fits_migrator.py <source>/2026 <output>/Meridian --summary > dry.txt

# Real copy (laptop: keep it awake)
caffeinate -i python fits_migrator.py <source>/2026 <output>/Meridian --execute --summary > run.txt

# Only some targets
python fits_migrator.py <source> <output> --target "M 31" --target "M 16" --execute

# Move or back up a folder tree and verify it
./copy_verify.sh <from> <to>            # copy, then verify
./copy_verify.sh --verify-only <a> <b>  # compare only
```
Options: `--year`, `--device`, `--merge-nights` (old layout), `--min-session-minutes N`,
`--tz`, `--review-root PATH`, `--review-in-tree`, `--move`, `--version`.

## 8. Rules the tools follow (keep them)

1. The source is read only; nothing is deleted or renamed.
2. `--execute` is always explicit; a dry run writes nothing.
3. The review folder may not be inside the output folder.
4. A second run never duplicates files that were copied correctly.
5. Anything unexpected (new headers, new device, odd dates, truncated files) produces a **warning
   with a count and one example**, not a silent guess. Check `surprises.log` after a firmware update.

## 9. Open items and backlog

- **Calibration per night.** Target-specific darks still go to `Calibration/<type>/` without a
  night split; shared factory frames stay in `_Calibration`. Waiting for real DWARF "please shoot
  darks" data and for what triggers them.
- **Siril / PixInsight stacks:** detect by `PROGRAM`/`SOFTWARE` header (as Meridian does) and route
  to `Finals/Siril` / `Finals/PixInsight`. Older Siril stacks have no recognizable folder names.
- **Non-FITS files** (JPG finals, Seestar stack JPGs, Burst JPGs) are not copied. Decide whether to.
- **Catalog script:** table per target (device, frames, total exposure, filters, temperature
  range, dates) to choose what to process.
- **Mosaics:** handled like any target (folder name from the `OBJECT` header, e.g. `mosaic_NGC 6960`);
  not tested with a complete mosaic.
- **Seestar stacks** may be counted by Meridian — verify with a Seestar-only target **(unconfirmed)**.
- **Review-folder names** for truncated files show only the last three path levels; the debug log
  has the full path.
- **Other scopes:** ASIAIR / traditional gear are not supported yet (add to `DEVICE_SIGNATURES`).
- A possible `1.0.0` = both years migrated, verified on tumi, and running in Meridian.

## 10. If you resume with an assistant

Give it this file, `CHANGELOG.md`, and the output of `python fits_migrator.py --version` and
`pytest -q`. Ask it to run the tests and a dry run before changing anything. It needs an
environment where it can run Python code, not just a chat.
