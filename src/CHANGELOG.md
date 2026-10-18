# Changelog

One shared version for all files (`fits_metadata_extractor.py`, `fits_migrator.py`,
`session_report.py`, `copy_verify.sh`). Versions bump together; see `RELEASING.md`.
Dates were not recorded for 0.1.0 – 0.7.0.

## [0.9.1] - 2026-10-13
### Added
- A dry run now prints a `Plan check` line (would copy / already present / skipped / missing
  sources). Before, "already present" was only visible after `--execute`, so a dry run
  could not be used to verify an existing copy.
- The reason for a flagged night now says how many sub-frames failed or were truncated
  (e.g. "329 of 335 sub-frames failed or truncated").
- Each truncated file is logged (debug) with its full path, actual size and expected size.
- `copy_verify.sh` (+ `test_copy_verify.sh`, 23 checks): copies a folder tree keeping file
  dates (rsync -a, or cp -Rp with `--use-cp`), then verifies every file by name, size and
  modification date and prints IDENTICAL or the differences. `--verify-only` just compares.
  Refuses `< >` placeholders, a missing source, and a destination inside the source.
  Safe to re-run. Written after Finder failed on DWARF files with odd permissions.
### Changed
- The truncated-file list in the summary shows three path levels (`session/cam_0/name`).

## [0.9.0] - 2026-10-12
### Changed (default layout!)
- Review material (failed / truncated frames, aborted and Unknown sessions, damaged
  calibration frames) now goes OUTSIDE the Meridian folder, to `<output>_Review/Year/Target/<night>/`.
  Reason: Meridian counts every frame under a watched folder (an M 31 test showed 18.6 h
  and 3,231 frames including all of `_Review`; the usable total was about 15.6 h / 2,635).
  Do NOT watch the review folder in Meridian.
### Added
- `--review-root PATH` to choose that folder; `--review-in-tree` for the old in-tree `_Review`.
- The summary shows the real review folder; the header prints `Review :`.
- Refuses a `--review-root` inside the output folder.

## [0.8.1] - 2026-10-12
### Fixed
- The dry-run summary printed a night's TOTAL file count next to a flagged session
  (e.g. 1190 files for a 2-frame aborted start). It now counts only the flagged files.
  (Routing was always correct; only the summary was wrong.)
### Changed
- The summary says "Nights" and "of that night" (several capture folders can share one night).
- Truncated-file list shows `folder/filename` (DWARF calibration names repeat).

## [0.8.0] - 2026-10-12
### Added
- Failed (`failed_…`) frames go to `_Review` individually; the summary shows what
  share of each session failed and suggests discarding sessions at 50% or more.
- Truncated files (shorter than their own header says) are detected and go to `_Review`.
- Visible version: `--version`, a banner at the start of every run, and the version,
  Python and astropy versions written to the top of the logs.
- A mismatch warning when the migrator and extractor are different versions.
- A log file per run: `migration.log` for dry runs, `migration_<time>.log` for `--execute`
  (a record of what was copied). Scan surprises stay in `surprises.log`.
### Changed
- One warning (with a count) for implausible observation dates and for truncated files,
  instead of one per file.
### Fixed
- `migration.log` was never written (the scan log and migrator log shared one file).

## [0.7.0]
### Added
- Seestar S30 Pro support (`BIAS`, `DATE-EXP` headers known).
- Implausible header dates (e.g. the DWARF Mini's 2038) are ignored; the year comes
  from the folder path and times from the filename.
- Moon, Sun, planets and comets are exempt from the minimum-exposure rule;
  sub-second exposures are labelled in milliseconds.
- The year folder follows the session's evening date (New Year's Eve sessions stay together).
### Fixed
- Files with the same name, size and time from different sessions (DWARF calibration
  files) could be treated as already copied; each now gets its own destination.

## [0.6.0]
### Added
- `--target` filter (repeatable).
- Re-running `--execute` is safe: identical files are reported as "already present".
- Free-space check before copying; a clean stop (partial file removed) if the disk fills.

## [0.5.0]
### Added
- Night splitting: `Lights/<evening date>_<exposure>/`, with the evening convention
  (a session starting before 12:00 belongs to the previous evening), Seestar local
  time from the filename (UTC header fallback), `_sub` folders merged with their
  session, aborted / Unknown sessions to `_Review`, and `--merge-nights` for the old layout.
- `session_report.py`: read-only preview of sessions.
- `WIDECAM` is a known header.

## [0.4.0]
### Fixed
- Seestar `Stacked_N_…` files are routed to `Finals/Device/`; `IMAGEW`/`IMAGEH` are
  known headers; duplicate log lines removed.

## [0.3.0]
### Added
- `Finals/Siril/` placeholder next to `Finals/Device/` and `Finals/PixInsight/`.

## [0.2.0]
### Added
- Phase 2 migrator: dry-run by default, copy or move, Year/Target/Lights layout,
  shared `_Calibration`, `_Review`.

## [0.1.0]
### Added
- Phase 1 metadata extractor: device detection, frame classification, calibration
  filename parsing, `surprises.log`.
