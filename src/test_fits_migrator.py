#!/usr/bin/env python3
"""
test_fits_migrator.py

Unit tests for fits_migrator.py  (Phase 2 — Migration Engine)

Tests cover:
  - sanitize_name()      : target name cleaning
  - build_dest_path()    : routing logic for every frame type / edge case
  - plan_migration()     : filter logic and action building
  - execute_plan()       : dry-run validation and live copy (via tmp_path)
  - _make_unique_dest()  : conflict avoidance
  - _is_device_stack()   : stacked-N_ detection (Rev 2: adds stacked_N_ for Seestar)

All tests are self-contained — no real FITS files required.
FITSMetadata instances are built directly from the dataclass.

Run with:
    pytest test_fits_migrator.py -v
"""

import shutil
import pytest
from pathlib import Path
from dataclasses import asdict

from fits_metadata_extractor import FITSMetadata
from fits_migrator import (
    sanitize_name,
    build_dest_path,
    plan_migration,
    execute_plan,
    _make_unique_dest,
    _is_device_stack,
    FOLDER_LIGHTS,
    FOLDER_CALIBRATION,
    FOLDER_FINALS,
    FOLDER_DEVICE_STACK,
    FOLDER_PI_WORK,
    FOLDER_SIRIL,
    FOLDER_SHARED_CALIB,
    FOLDER_REVIEW,
    MigrationAction,
    FOLDER_UNKNOWN_DATE,
    parse_filename_time,
    local_capture_time,
    night_label,
    normalize_session_folder,
    build_sessions,
    session_key,
    print_summary,
    check_free_space,
    is_solar_system_target,
    _resolve_dest,
    _exposure_label,
    _is_failed_frame,
    _frame_problem,
    __version__ as MIGRATOR_VERSION,
    EXTRACTOR_VERSION,
    version_banner,
    version_mismatch,
    attach_run_log,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def make_meta(**kwargs) -> FITSMetadata:
    """Build a FITSMetadata with sensible defaults, overridden by kwargs."""
    fname = kwargs.get('file_name', 'file.fits')
    defaults = dict(
        file_path=f'/Users/edm/Astronomy/Astrocapture/2025/DWARF 3/{fname}',
        file_name=fname,
        file_extension='.fits',
        device_type='DWARF_3',
        frame_type='LIGHT',
        object_name='NGC 281',
        observation_year=2025,
        is_valid=True,
        warnings=[],
        unknown_headers={},
    )
    defaults.update(kwargs)
    return FITSMetadata(**defaults)


OUTPUT = Path('/output/Meridian')


# ──────────────────────────────────────────────────────────────────────────────
# sanitize_name
# ──────────────────────────────────────────────────────────────────────────────

class TestSanitizeName:

    def test_normal_name_unchanged(self):
        assert sanitize_name('NGC 281') == 'NGC 281'

    def test_spaces_preserved(self):
        assert sanitize_name('Barnard 33') == 'Barnard 33'

    def test_slash_replaced(self):
        assert sanitize_name('M42/M43') == 'M42-M43'

    def test_colon_replaced(self):
        assert sanitize_name('NGC:1234') == 'NGC-1234'

    def test_backslash_replaced(self):
        assert sanitize_name('M42\\M43') == 'M42-M43'

    def test_empty_string_returns_no_target(self):
        assert sanitize_name('') == '_NoTarget'

    def test_whitespace_only_returns_no_target(self):
        assert sanitize_name('   ') == '_NoTarget'

    def test_leading_trailing_dots_stripped(self):
        assert sanitize_name('.NGC 281.') == 'NGC 281'

    def test_all_unsafe_chars(self):
        result = sanitize_name('a/b\\c:d*e?f"g<h>i|j')
        assert '/' not in result
        assert '\\' not in result
        assert ':' not in result
        assert '*' not in result

    def test_real_target_names(self):
        names = ['C 11', 'Barnard 33', 'NGC 281', 'M 42', 'IC 1805']
        for name in names:
            assert sanitize_name(name)   # non-empty
            assert '/' not in sanitize_name(name)


# ──────────────────────────────────────────────────────────────────────────────
# _is_device_stack
# ──────────────────────────────────────────────────────────────────────────────

class TestIsDeviceStack:

    @pytest.mark.parametrize("filename,expected", [
        # ── DWARF format: stacked-N_ (hyphen, lowercase) ──────────────────
        ('stacked-16_NGC281_LP.fits',                        True),
        ('stacked-32_M42.fit',                               True),
        ('stacked-8_target.fits',                            True),
        ('STACKED-4_file.fits',                              True),   # case-insensitive
        ('stacked-1_x.fits',                                 True),
        # ── Seestar format: Stacked_N_ (underscore, any case) ─ Rev 2 ─────
        ('Stacked_492_M 57_10.0s_LP_20250825-010001.fit',   True),
        ('Stacked_16_NGC 281_10.0s_LP_20251001.fit',        True),
        ('STACKED_8_target.fits',                            True),   # case-insensitive
        ('stacked_1_x.fits',                                 True),   # lowercase
        # ── Not device stacks ─────────────────────────────────────────────
        ('light_NGC281.fits',                                False),
        ('Barnard 33_30s.fits',                              False),
        ('dark_exp_30.fits',                                 False),
        ('unknown_abc.fits',                                 False),
    ])
    def test_detection(self, filename, expected):
        assert _is_device_stack(filename) == expected

    def test_seestar_stacked_goes_to_finals_device(self):
        """Rev 2: Seestar Stacked_N_Target_… must route to Finals/Device/, not Lights/."""
        from pathlib import Path
        seestar_name = 'Stacked_492_M 57_10.0s_LP_20250825-010001.fit'
        meta = make_meta(
            frame_type='LIGHT',
            object_name='M 57',
            observation_year=2025,
            file_name=seestar_name,
        )
        dest = build_dest_path(meta, Path('/out'))
        parts = dest.parts
        assert FOLDER_FINALS      in parts, f'Expected Finals/ in {dest}'
        assert FOLDER_DEVICE_STACK in parts, f'Expected Device/ in {dest}'
        assert 'Lights' not in parts, (
            f'Seestar stack should NOT go to Lights/, but got: {dest}')


# ──────────────────────────────────────────────────────────────────────────────
# build_dest_path — routing logic
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildDestPath:

    # ── LIGHT frames ──────────────────────────────────────────────────────────

    def test_light_goes_to_lights_folder(self):
        meta = make_meta(frame_type='LIGHT', object_name='NGC 281',
                         observation_year=2025,
                         file_name='NGC281_30s_LP_001.fits')
        dest = build_dest_path(meta, OUTPUT)
        assert dest.parts[-4] == '2025'
        assert dest.parts[-3] == 'NGC 281'
        assert dest.parts[-2] == FOLDER_LIGHTS
        assert dest.name == 'NGC281_30s_LP_001.fits'

    def test_device_stack_goes_to_finals_device(self):
        meta = make_meta(frame_type='LIGHT', object_name='NGC 281',
                         observation_year=2025,
                         file_name='stacked-16_NGC281_LP_20251001.fits')
        dest = build_dest_path(meta, OUTPUT)
        assert FOLDER_FINALS in dest.parts
        assert FOLDER_DEVICE_STACK in dest.parts

    def test_device_stack_not_in_lights(self):
        meta = make_meta(frame_type='LIGHT', object_name='NGC 281',
                         observation_year=2025,
                         file_name='stacked-16_NGC281_LP_20251001.fits')
        dest = build_dest_path(meta, OUTPUT)
        assert FOLDER_LIGHTS not in dest.parts

    # ── Calibration frames with target ────────────────────────────────────────

    def test_dark_with_target_goes_to_target_calibration(self):
        meta = make_meta(frame_type='DARK', object_name='NGC 281',
                         observation_year=2025,
                         file_name='dark_exp_30_gain_60_bin_1_34C_stack_1.fits')
        dest = build_dest_path(meta, OUTPUT)
        assert FOLDER_CALIBRATION in dest.parts
        assert 'Dark' in dest.parts
        assert 'NGC 281' in dest.parts

    def test_bias_with_target_goes_to_target_calibration(self):
        meta = make_meta(frame_type='BIAS', object_name='Barnard 33',
                         observation_year=2026,
                         file_name='bias_gain_2_bin_1.fits')
        dest = build_dest_path(meta, OUTPUT)
        assert FOLDER_CALIBRATION in dest.parts
        assert 'Bias' in dest.parts

    def test_flat_with_target_goes_to_target_calibration(self):
        meta = make_meta(frame_type='FLAT', object_name='M42',
                         observation_year=2025,
                         file_name='flat_gain_2_bin_1.fits')
        dest = build_dest_path(meta, OUTPUT)
        assert FOLDER_CALIBRATION in dest.parts
        assert 'Flat' in dest.parts

    # ── Factory calibration (no target) ───────────────────────────────────────

    def test_dark_without_target_goes_to_shared_calibration(self):
        meta = make_meta(frame_type='DARK', object_name='',
                         observation_year=2025,
                         file_name='dark_exp_30_gain_60_bin_1_34C_stack_1.fits')
        dest = build_dest_path(meta, OUTPUT)
        assert FOLDER_SHARED_CALIB in dest.parts
        assert 'Dark' in dest.parts
        # Must NOT go under any target subdirectory
        assert FOLDER_CALIBRATION not in dest.parts

    def test_bias_without_target_goes_to_shared_calibration(self):
        meta = make_meta(frame_type='BIAS', object_name='',
                         observation_year=2026,
                         file_name='bias_gain_2_bin_1.fits')
        dest = build_dest_path(meta, OUTPUT)
        assert FOLDER_SHARED_CALIB in dest.parts
        assert 'Bias' in dest.parts

    def test_flat_without_target_goes_to_shared_calibration(self):
        meta = make_meta(frame_type='FLAT', object_name='',
                         observation_year=2025,
                         file_name='flat_gain_2_bin_1_ir_0.fits')
        dest = build_dest_path(meta, OUTPUT)
        assert FOLDER_SHARED_CALIB in dest.parts
        assert 'Flat' in dest.parts

    # ── UNKNOWN frame type ────────────────────────────────────────────────────

    def test_unknown_with_target_goes_to_target_review(self):
        meta = make_meta(frame_type='UNKNOWN', object_name='NGC 281',
                         observation_year=2025,
                         file_name='mystery_file.fits')
        dest = build_dest_path(meta, OUTPUT)
        assert FOLDER_REVIEW in dest.parts
        assert 'NGC 281' in dest.parts

    def test_unknown_without_target_goes_to_year_review(self):
        meta = make_meta(frame_type='UNKNOWN', object_name='',
                         observation_year=2025,
                         file_name='mystery_file.fits')
        dest = build_dest_path(meta, OUTPUT)
        assert FOLDER_REVIEW in dest.parts

    # ── Year routing ──────────────────────────────────────────────────────────

    def test_year_in_path(self):
        meta = make_meta(observation_year=2025)
        dest = build_dest_path(meta, OUTPUT)
        assert '2025' in dest.parts

    def test_year_2026_in_path(self):
        meta = make_meta(observation_year=2026,
                         file_path='/Astrocapture/2026/DWARF 3/file.fits')
        dest = build_dest_path(meta, OUTPUT)
        assert '2026' in dest.parts

    def test_unknown_year_falls_back_to_path(self):
        meta = make_meta(observation_year=None,
                         file_path='/Astrocapture/2025/DWARF 3/file.fits')
        dest = build_dest_path(meta, OUTPUT)
        assert '2025' in dest.parts

    # ── Filename preserved ────────────────────────────────────────────────────

    def test_filename_preserved(self):
        fname = 'C 11_30s60_Duo-Band_20250819-025612413_40C.fits'
        meta = make_meta(file_name=fname)
        dest = build_dest_path(meta, OUTPUT)
        assert dest.name == fname

    def test_output_root_is_parent(self):
        meta = make_meta()
        dest = build_dest_path(meta, OUTPUT)
        assert str(dest).startswith(str(OUTPUT))

    # ── Target name sanitization ──────────────────────────────────────────────

    def test_target_with_slash_sanitized_in_path(self):
        meta = make_meta(object_name='M42/M43')
        dest = build_dest_path(meta, OUTPUT)
        # Path should not contain a raw slash within the target component
        assert 'M42-M43' in dest.parts or 'M42' in dest.parts

    # ── Device-specific routing ───────────────────────────────────────────────

    def test_seestar_light_goes_to_lights(self):
        meta = make_meta(
            device_type='SEESTAR_S50',
            frame_type='LIGHT',
            object_name='NGC 281',
            file_name='Light_NGC 281_10.0s_LP_20250929-062845.fit',
        )
        dest = build_dest_path(meta, OUTPUT)
        assert FOLDER_LIGHTS in dest.parts

    def test_dwarf_mini_dark_no_target_to_shared_calib(self):
        meta = make_meta(
            device_type='DWARF_MINI',
            frame_type='DARK',
            object_name='',
            file_name='dark_exp_30.000000_gain_60_bin_1_34C_stack_1.fits',
        )
        dest = build_dest_path(meta, OUTPUT)
        assert FOLDER_SHARED_CALIB in dest.parts


# ──────────────────────────────────────────────────────────────────────────────
# plan_migration
# ──────────────────────────────────────────────────────────────────────────────

class TestPlanMigration:

    @pytest.fixture
    def mixed_results(self):
        """A small representative set covering the main cases."""
        return [
            make_meta(frame_type='LIGHT',   object_name='NGC 281',
                      observation_year=2025, device_type='DWARF_3',
                      file_name='NGC281_light_001.fits'),
            make_meta(frame_type='DARK',    object_name='',
                      observation_year=2025, device_type='DWARF_3',
                      file_name='dark_exp_30_gain_60_34C_stack_1.fits'),
            make_meta(frame_type='LIGHT',   object_name='Barnard 33',
                      observation_year=2026, device_type='DWARF_MINI',
                      file_name='Barnard33_light_001.fits'),
            make_meta(frame_type='LIGHT',   object_name='NGC 281',
                      observation_year=2025, device_type='SEESTAR_S50',
                      file_name='stacked-16_NGC281_LP.fits'),
            make_meta(frame_type='UNKNOWN', object_name='',
                      observation_year=2025, device_type='DWARF_3',
                      file_name='mystery_file.fits'),
            make_meta(frame_type='LIGHT',   object_name='M42',
                      observation_year=2025, device_type='DWARF_3',
                      is_valid=False,
                      file_name='corrupted.fits'),
        ]

    def test_plan_length_matches_results(self, mixed_results):
        plan = plan_migration(mixed_results, OUTPUT)
        assert len(plan) == len(mixed_results)

    def test_invalid_file_is_skipped(self, mixed_results):
        plan = plan_migration(mixed_results, OUTPUT)
        skips = [a for a in plan if a.is_skip]
        assert len(skips) == 1
        assert skips[0].source.name == 'corrupted.fits'

    def test_year_filter_excludes_other_years(self, mixed_results):
        plan = plan_migration(mixed_results, OUTPUT, year_filter='2025')
        years = {a.year for a in plan}
        assert '2026' not in years

    def test_year_filter_2026_only(self, mixed_results):
        plan = plan_migration(mixed_results, OUTPUT, year_filter='2026')
        assert len(plan) == 1
        assert plan[0].target == 'Barnard 33'

    def test_device_filter(self, mixed_results):
        plan = plan_migration(mixed_results, OUTPUT, device_filter='SEESTAR_S50')
        assert len(plan) == 1
        assert plan[0].source.name == 'stacked-16_NGC281_LP.fits'

    def test_device_filter_case_insensitive(self, mixed_results):
        plan_upper = plan_migration(mixed_results, OUTPUT, device_filter='SEESTAR_S50')
        plan_lower = plan_migration(mixed_results, OUTPUT, device_filter='seestar_s50')
        assert len(plan_upper) == len(plan_lower)

    def test_default_operation_is_copy(self, mixed_results):
        plan = plan_migration(mixed_results, OUTPUT)
        ops = {a.operation for a in plan if not a.is_skip}
        assert ops == {'copy'}

    def test_move_operation_propagated(self, mixed_results):
        plan = plan_migration(mixed_results, OUTPUT, operation='move')
        ops = {a.operation for a in plan if not a.is_skip}
        assert ops == {'move'}

    def test_all_actions_have_dest_under_output(self, mixed_results):
        plan = plan_migration(mixed_results, OUTPUT)
        for action in plan:
            if not action.is_skip:
                assert str(action.dest).startswith(str(OUTPUT))

    def test_stacked_file_goes_to_finals_device(self, mixed_results):
        plan = plan_migration(mixed_results, OUTPUT)
        stacked = next(a for a in plan if 'stacked-16' in a.source.name)
        assert FOLDER_FINALS     in stacked.dest.parts
        assert FOLDER_DEVICE_STACK in stacked.dest.parts

    def test_shared_dark_goes_to_shared_calib(self, mixed_results):
        plan = plan_migration(mixed_results, OUTPUT)
        dark = next(a for a in plan if 'dark_exp' in a.source.name)
        assert FOLDER_SHARED_CALIB in dark.dest.parts


# ──────────────────────────────────────────────────────────────────────────────
# _make_unique_dest
# ──────────────────────────────────────────────────────────────────────────────

class TestMakeUniqueDest:

    def test_no_conflict_returns_same_path(self, tmp_path):
        dest = tmp_path / 'file.fits'
        assert _make_unique_dest(dest) == dest

    def test_conflict_returns_numbered_path(self, tmp_path):
        dest = tmp_path / 'file.fits'
        dest.touch()                           # create conflict
        unique = _make_unique_dest(dest)
        assert unique == tmp_path / 'file_2.fits'

    def test_multiple_conflicts(self, tmp_path):
        dest = tmp_path / 'file.fits'
        dest.touch()
        (tmp_path / 'file_2.fits').touch()
        unique = _make_unique_dest(dest)
        assert unique == tmp_path / 'file_3.fits'

    def test_suffix_preserved(self, tmp_path):
        dest = tmp_path / 'image.fit'
        dest.touch()
        unique = _make_unique_dest(dest)
        assert unique.suffix == '.fit'


# ──────────────────────────────────────────────────────────────────────────────
# execute_plan — dry-run and live copy
# ──────────────────────────────────────────────────────────────────────────────

class TestExecutePlan:

    @pytest.fixture
    def source_files(self, tmp_path):
        """Create real source FITS-named files for copy tests."""
        src = tmp_path / 'source'
        src.mkdir()
        files = {
            'light.fits':   src / 'light.fits',
            'dark.fits':    src / 'dark.fits',
            'stacked.fits': src / 'stacked-16_NGC281.fits',
        }
        for f in files.values():
            f.write_bytes(b'SIMPLE  =                    T')
        return src, files

    @pytest.fixture
    def simple_plan(self, source_files, tmp_path):
        src, files = source_files
        out = tmp_path / 'output'
        return [
            MigrationAction(
                source=files['light.fits'],
                dest=out / '2025' / 'NGC 281' / 'Lights' / 'light.fits',
                operation='copy', skip_reason='',
                device='DWARF_3', frame_type='LIGHT',
                target='NGC 281', year='2025',
            ),
            MigrationAction(
                source=files['dark.fits'],
                dest=out / '2025' / '_Calibration' / 'Dark' / 'dark.fits',
                operation='copy', skip_reason='',
                device='DWARF_3', frame_type='DARK',
                target='', year='2025',
            ),
        ], out

    def test_dry_run_creates_no_files(self, simple_plan, tmp_path):
        plan, out = simple_plan
        execute_plan(plan, dry_run=True)
        assert not out.exists()

    def test_dry_run_counts_operations(self, simple_plan):
        plan, out = simple_plan
        stats = execute_plan(plan, dry_run=True)
        assert stats.get('copied', 0) == 2

    def test_execute_creates_destination_files(self, simple_plan):
        plan, out = simple_plan
        execute_plan(plan, dry_run=False, output_root=out)
        assert (out / '2025' / 'NGC 281' / 'Lights' / 'light.fits').exists()
        assert (out / '2025' / '_Calibration' / 'Dark' / 'dark.fits').exists()

    def test_execute_creates_directories(self, simple_plan):
        plan, out = simple_plan
        execute_plan(plan, dry_run=False, output_root=out)
        assert (out / '2025' / 'NGC 281' / 'Lights').is_dir()
        assert (out / '2025' / '_Calibration' / 'Dark').is_dir()

    def test_skip_action_not_copied(self, simple_plan):
        plan, out = simple_plan
        plan.append(MigrationAction(
            source=Path('/nonexistent/file.fits'),
            dest=Path('/dev/null'),
            operation='skip', skip_reason='Test skip',
            device='UNKNOWN', frame_type='UNKNOWN',
            target='', year='2025',
        ))
        stats = execute_plan(plan, dry_run=False, output_root=out)
        assert stats['skipped'] == 1

    def test_execute_stats_correct(self, simple_plan):
        plan, out = simple_plan
        stats = execute_plan(plan, dry_run=False, output_root=out)
        assert stats['copied']  == 2
        assert stats['skipped'] == 0
        assert stats['errors']  == 0

    def test_siril_placeholder_created_on_execute(self, source_files, tmp_path):
        """Finals/Siril/ must be created alongside Finals/PixInsight/ on execute."""
        src, files = source_files
        out = tmp_path / 'output'
        stacked_file = src / 'stacked-16_NGC281.fits'
        stacked_file.write_bytes(b'FITS')
        plan = [MigrationAction(
            source=stacked_file,
            dest=out / '2025' / 'NGC 281' / 'Finals' / 'Device' / stacked_file.name,
            operation='copy', skip_reason='',
            device='DWARF_3', frame_type='LIGHT',
            target='NGC 281', year='2025',
        )]
        execute_plan(plan, dry_run=False, output_root=out)
        assert (out / '2025' / 'NGC 281' / 'Finals' / 'Siril').is_dir()
        assert (out / '2025' / 'NGC 281' / 'Finals' / 'PixInsight').is_dir()

    def test_conflict_handled_without_overwrite(self, simple_plan):
        """
        Rev 4: running twice must NOT duplicate (the second run sees the files
        are already present).  A *different* file with the same destination name
        still gets a _2 suffix and is never overwritten.
        """
        plan, out = simple_plan
        execute_plan(plan, dry_run=False, output_root=out)
        s2 = execute_plan(plan, dry_run=False, output_root=out)      # second run
        lights_dir = out / '2025' / 'NGC 281' / 'Lights'
        assert [f.name for f in lights_dir.iterdir()] == ['light.fits']
        assert s2['copied'] == 0 and s2['already_present'] >= 1

        # a different file arriving at the same destination is kept, not overwritten
        (lights_dir / 'light.fits').write_bytes(b'different content, different size')
        execute_plan(plan, dry_run=False, output_root=out)
        assert {f.name for f in lights_dir.iterdir()} == {'light.fits', 'light_2.fits'}


# ──────────────────────────────────────────────────────────────────────────────
# Integration smoke test
# ──────────────────────────────────────────────────────────────────────────────

class TestIntegration:
    """End-to-end: metadata → plan → execute → verify filesystem."""

    def test_full_pipeline(self, tmp_path):
        # Build a small fake scan result
        src = tmp_path / 'source'
        src.mkdir()

        files = [
            ('light_NGC281_001.fits',                    'LIGHT',   'NGC 281', 2025),
            ('dark_exp_30_gain_60_bin_1_34C_stack_1.fits', 'DARK', '',        2025),
            ('stacked-16_NGC281_LP.fits',                'LIGHT',   'NGC 281', 2025),
            ('bias_gain_2_bin_1.fits',                   'BIAS',    '',        2025),
        ]

        results = []
        for fname, ftype, target, year in files:
            fpath = src / fname
            fpath.write_bytes(b'FITS')
            results.append(make_meta(
                file_path=str(fpath), file_name=fname,
                frame_type=ftype, object_name=target,
                observation_year=year,
            ))

        out = tmp_path / 'meridian'
        plan = plan_migration(results, out, split_nights=False)
        stats = execute_plan(plan, dry_run=False, output_root=out)

        assert stats['errors'] == 0
        assert (out / '2025' / 'NGC 281' / 'Lights' / 'light_NGC281_001.fits').exists()
        assert (out / '2025' / '_Calibration' / 'Dark' /
                'dark_exp_30_gain_60_bin_1_34C_stack_1.fits').exists()
        assert (out / '2025' / 'NGC 281' / 'Finals' / 'Device' /
                'stacked-16_NGC281_LP.fits').exists()
        assert (out / '2025' / '_Calibration' / 'Bias' /
                'bias_gain_2_bin_1.fits').exists()
        # Finals/ placeholders for post-processing tools
        assert (out / '2025' / 'NGC 281' / 'Finals' / 'PixInsight').is_dir()
        assert (out / '2025' / 'NGC 281' / 'Finals' / 'Siril').is_dir()


# ══════════════════════════════════════════════════════════════════════════════
# Rev 3 — night splitting
# ══════════════════════════════════════════════════════════════════════════════
from datetime import datetime


def lm(folder, ts, name=None, device='DWARF_3', exp=15.0, target='M 31',
       ftype='LIGHT', n=[0]):
    """Light-frame metadata inside a named capture folder."""
    n[0] += 1
    name = name or f'frame_{n[0]:05d}.fits'
    return make_meta(
        file_path=f'/src/2025/{folder}/{name}', file_name=name,
        device_type=device, frame_type=ftype, object_name=target,
        observation_date=ts, exposure_time=exp, observation_year=2025)


def night(plan_item):
    return plan_item.dest.parent.name


class TestFilenameTime:
    @pytest.mark.parametrize('name,expected', [
        ('Light_M 16_10.0s_LP_20250806-213920.fit',           datetime(2025, 8, 6, 21, 39, 20)),
        ('Stacked_1_M 16_10.0s_LP_20250806-213920.fit',       datetime(2025, 8, 6, 21, 39, 20)),
        ('Barnard 33_30s60_Duo-Band_20260324-203732172_36C.fits', datetime(2026, 3, 24, 20, 37, 32)),
        ('stacked-16_NGC281_LP.fits',                          None),
        ('light_NGC 281_10.0s_LP_20251399-250000.fit',         None),   # impossible date
    ])
    def test_parse(self, name, expected):
        assert parse_filename_time(name) == expected


class TestNightLabel:
    @pytest.mark.parametrize('ts,expected', [
        (datetime(2025, 8, 5, 23, 6),   '2025-08-05'),
        (datetime(2025, 8, 6, 0, 51),   '2025-08-05'),   # after midnight -> previous evening
        (datetime(2025, 8, 6, 11, 59),  '2025-08-05'),
        (datetime(2025, 8, 6, 12, 0),   '2025-08-06'),
        (datetime(2025, 1, 1, 0, 30),   '2024-12-31'),   # year boundary
    ])
    def test_evening_convention(self, ts, expected):
        assert night_label(ts) == expected


class TestLocalCaptureTime:
    def test_seestar_prefers_filename_time(self):
        m = lm('f', '2025-08-07T02:39:20', device='SEESTAR_S50',
               name='Light_M 16_10.0s_LP_20250806-213920.fit')
        t, conflict = local_capture_time(m)
        assert t == datetime(2025, 8, 6, 21, 39, 20) and not conflict

    def test_seestar_header_utc_converted_summer(self):
        m = lm('f', '2025-08-07T02:39:20', device='SEESTAR_S50', name='frame.fit')
        assert local_capture_time(m)[0] == datetime(2025, 8, 6, 21, 39, 20)   # CDT = UTC-5

    def test_seestar_header_utc_converted_winter(self):
        m = lm('f', '2025-12-07T03:39:20', device='SEESTAR_S50', name='frame.fit')
        assert local_capture_time(m)[0] == datetime(2025, 12, 6, 21, 39, 20)  # CST = UTC-6

    def test_seestar_conflict_when_header_is_already_local(self):
        # firmware starts writing local time in the header -> disagrees with UTC conversion
        m = lm('f', '2025-08-06T21:39:20', device='SEESTAR_S50',
               name='Light_M 16_10.0s_LP_20250806-213920.fit')
        t, conflict = local_capture_time(m)
        assert conflict and t == datetime(2025, 8, 6, 21, 39, 20)

    def test_seestar_stack_not_flagged_as_conflict(self):
        # stack filename time is when it was saved, hours after DATE-OBS
        m = lm('f', '2025-08-07T02:00:00', device='SEESTAR_S50',
               name='Stacked_492_M 57_10.0s_LP_20250806-233001.fit')
        assert local_capture_time(m)[1] is False

    def test_dwarf_header_is_local_no_conversion(self):
        m = lm('f', '2025-07-29T00:51:00')
        assert local_capture_time(m)[0] == datetime(2025, 7, 29, 0, 51)

    def test_dwarf_falls_back_to_filename(self):
        m = lm('f', None, name='Barnard 33_30s60_Duo-Band_20260324-203732172_36C.fits')
        assert local_capture_time(m)[0] == datetime(2026, 3, 24, 20, 37, 32)

    def test_no_time_anywhere(self):
        assert local_capture_time(lm('f', None))[0] is None


class TestSessionFolder:
    def test_sub_stripped(self):
        assert normalize_session_folder('M 16_sub Seestar 50') == 'M 16 Seestar 50'
        assert normalize_session_folder('M 16 Seestar 50') == 'M 16 Seestar 50'
        assert normalize_session_folder('M 16_SUB') == 'M 16'

    def test_sub_and_stack_folder_same_session(self):
        a = lm('M 16_sub Seestar 50', '2025-08-07T02:38:00', device='SEESTAR_S50')
        b = lm('M 16 Seestar 50',     '2025-08-07T02:39:00', device='SEESTAR_S50')
        assert session_key(a) == session_key(b)

    def test_different_folders_different_sessions(self):
        assert session_key(lm('n1', '2025-08-06T22:00:00')) != session_key(lm('n2', '2025-08-07T22:00:00'))


class TestNightSplitPlan:
    def run(self, metas, **kw):
        return plan_migration(metas, OUTPUT, **kw)

    def nights(self, plan):
        return {night(a) for a in plan}

    def test_two_nights_two_folders_default(self):
        ms = ([lm('n1', '2025-08-05T22:00:00') for _ in range(50)] +
              [lm('n2', '2025-08-12T22:00:00') for _ in range(50)])
        plan = self.run(ms)
        assert self.nights(plan) == {'2025-08-05_15s', '2025-08-12_15s'}
        assert all(FOLDER_LIGHTS in a.dest.parts for a in plan)

    def test_session_crossing_midnight_is_one_folder(self):
        ms = [lm('n1', f'2025-08-05T{h:02d}:00:00', exp=60.0) for h in (22, 23)] + \
             [lm('n1', f'2025-08-06T{h:02d}:00:00', exp=60.0) for h in (0, 1, 2)]
        plan = self.run(ms)
        assert self.nights(plan) == {'2025-08-05_60s'}

    def test_post_midnight_start_labeled_previous_evening(self):
        ms = [lm('n1', '2025-07-29T00:51:00') for _ in range(50)]
        assert self.nights(self.run(ms)) == {'2025-07-28_15s'}

    def test_exposure_in_label(self):
        ms = [lm('n1', '2025-08-05T22:00:00', exp=7.5) for _ in range(100)]
        assert self.nights(self.run(ms)) == {'2025-08-05_7.5s'}

    def test_devices_get_separate_folders(self):
        ms = ([lm('d3', '2025-08-05T22:00:00', device='DWARF_3') for _ in range(50)] +
              [lm('ss', '2025-08-05T22:00:00', device='SEESTAR_S50', exp=10.0,
                  name=f'Light_M 31_10.0s_LP_20250805-2200{i:02d}.fit') for i in range(50)])
        plan = self.run(ms)
        assert self.nights(plan) == {'2025-08-05_15s', '2025-08-05_10s'}

    def test_seestar_sub_and_stack_share_night_folder(self):
        subs = [lm('M 16_sub Seestar 50', '2025-08-07T02:38:00', device='SEESTAR_S50', exp=10.0,
                   name=f'Light_M 16_10.0s_LP_20250806-2138{i:02d}.fit', target='M 16') for i in range(70)]
        stack = lm('M 16 Seestar 50', '2025-08-07T02:00:00', device='SEESTAR_S50', exp=10.0,
                   name='Stacked_70_M 16_10.0s_LP_20250806-233001.fit', target='M 16')
        plan = self.run(subs + [stack])
        lights = [a for a in plan if FOLDER_LIGHTS in a.dest.parts]
        stk = [a for a in plan if FOLDER_DEVICE_STACK in a.dest.parts]
        assert {night(a) for a in lights} == {night(a) for a in stk} == {'2025-08-06_10s'}
        assert FOLDER_FINALS in stk[0].dest.parts

    def test_stack_goes_to_finals_device_night(self):
        ms = [lm('n1', '2025-08-05T22:00:00') for _ in range(50)]
        ms.append(lm('n1', '2025-08-05T23:00:00', name='stacked-50_M31_LP.fits'))
        plan = self.run(ms)
        stk = [a for a in plan if a.source.name.startswith('stacked-')][0]
        assert stk.dest.parent.parts[-3:] == (FOLDER_FINALS, FOLDER_DEVICE_STACK, '2025-08-05_15s')

    def test_merge_nights_restores_old_layout(self):
        ms = [lm('n1', '2025-08-05T22:00:00'), lm('n2', '2025-08-12T22:00:00')]
        plan = self.run(ms, split_nights=False)
        assert {a.dest.parent.name for a in plan} == {FOLDER_LIGHTS}
        assert all(a.session == '' for a in plan)

    def test_no_time_goes_to_unknown_date_folder(self):
        ms = [lm('n1', None) for _ in range(50)]
        assert self.nights(self.run(ms)) == {FOLDER_UNKNOWN_DATE}

    def test_calibration_and_unknown_frames_unchanged(self):
        d = lm('n1', '2025-08-05T22:00:00', ftype='DARK')
        u = lm('n1', '2025-08-05T22:00:00', ftype='UNKNOWN')
        plan = self.run([d, u])
        assert plan[0].dest.parent.name == 'Dark' and plan[1].dest.parent.name == FOLDER_REVIEW


class TestReviewSessions:
    def run(self, metas, **kw):
        return plan_migration(metas, OUTPUT, **kw)

    def test_short_session_flagged(self):
        ms = [lm('n7', '2025-10-12T20:46:00', exp=15.0) for _ in range(6)]     # 90 s
        plan = self.run(ms)
        assert all(FOLDER_REVIEW in a.dest.parts for a in plan)
        assert all('min of exposure' in a.review_reason for a in plan)

    def test_session_at_threshold_not_flagged(self):
        ms = [lm('n1', '2025-10-12T20:46:00', exp=60.0) for _ in range(10)]    # exactly 10 min
        assert all(FOLDER_LIGHTS in a.dest.parts for a in self.run(ms))

    def test_threshold_configurable(self):
        ms = [lm('n1', '2025-10-12T20:46:00', exp=60.0) for _ in range(10)]
        plan = self.run(ms, min_session_minutes=30)
        assert all(FOLDER_REVIEW in a.dest.parts for a in plan)

    def test_long_session_not_flagged(self):
        ms = [lm('n1', '2025-10-12T20:46:00', exp=30.0) for _ in range(200)]
        assert all(FOLDER_LIGHTS in a.dest.parts for a in self.run(ms))

    def test_unknown_target_flagged(self):
        ms = [lm('u1', '2026-02-27T19:50:00', exp=30.0, target='Unknown(1)') for _ in range(100)]
        plan = self.run(ms)
        assert all(FOLDER_REVIEW in a.dest.parts for a in plan)
        assert 'Unknown' in plan[0].review_reason

    def test_failed_frames_flagged(self):
        ms = [lm('f1', '2026-02-27T19:50:00', exp=30.0, target='Orion',
                 name=f'failed_Orion_{i}.fits') for i in range(100)]
        assert 'failed capture' in self.run(ms)[0].review_reason

    def test_stack_of_flagged_session_also_goes_to_review(self):
        ms = [lm('n7', '2025-10-12T20:46:00', exp=15.0) for _ in range(6)]
        ms.append(lm('n7', '2025-10-12T20:50:00', name='stacked-6_M31.fits'))
        plan = self.run(ms)
        assert all(FOLDER_REVIEW in a.dest.parts for a in plan)

    def test_stack_only_session_not_flagged_for_short_integration(self):
        plan = self.run([lm('s', '2025-10-12T20:50:00', name='stacked-6_M31.fits')])
        assert FOLDER_DEVICE_STACK in plan[0].dest.parts

    def test_unknown_exposure_not_flagged_by_threshold(self):
        ms = [lm('n1', '2025-10-12T20:46:00', exp=None) for _ in range(3)]
        assert all(FOLDER_LIGHTS in a.dest.parts for a in self.run(ms))

    def test_summary_lists_flagged_sessions(self, capsys):
        ms = [lm('n7', '2025-10-12T20:46:00', exp=15.0) for _ in range(6)]
        print_summary(self.run(ms))
        out = capsys.readouterr().out
        assert 'flagged for review' in out and '_Review/2025-10-12_15s' in out


class TestSessionWarnings:
    def test_folder_date_mismatch_warns(self, caplog):
        folder = 'DWARF_RAW_TELE_M 31_EXP_15_GAIN_60_2025-08-05-21-23-06-495'
        ms = [lm(folder, '2025-07-25T00:34:00') for _ in range(50)]
        import logging
        with caplog.at_level(logging.WARNING, logger='fits_migrator'):
            plan = plan_migration(ms, OUTPUT)
        assert any('date mismatch' in r.message for r in caplog.records)
        assert night(plan[0]) == '2025-07-24_15s'      # trusts capture times

    def test_matching_folder_date_silent(self, caplog):
        folder = 'DWARF_RAW_TELE_M 31_EXP_15_GAIN_60_2025-07-29-00-50-58-376'
        ms = [lm(folder, '2025-07-29T00:51:00') for _ in range(50)]
        import logging
        with caplog.at_level(logging.WARNING, logger='fits_migrator'):
            plan_migration(ms, OUTPUT)
        assert not [r for r in caplog.records if 'mismatch' in r.message]

    def test_seestar_time_conflict_warns_once(self, caplog):
        ms = [lm('s', '2025-08-06T21:39:20', device='SEESTAR_S50', exp=10.0,
                 name=f'Light_M 16_10.0s_LP_20250806-2139{i:02d}.fit') for i in range(20)]
        import logging
        with caplog.at_level(logging.WARNING, logger='fits_migrator'):
            plan_migration(ms, OUTPUT)
        msgs = [r for r in caplog.records if 'does not match' in r.message]
        assert len(msgs) == 1 and '20 Seestar' in msgs[0].message


class TestNightSplitExecute:
    def test_execute_creates_night_folders_and_placeholders(self, tmp_path):
        src = tmp_path / 'src' / 'night1'
        src.mkdir(parents=True)
        ms = []
        for i in range(30):
            f = src / f'frame_{i:03d}.fits'
            f.write_bytes(b'FITS')
            ms.append(make_meta(file_path=str(f), file_name=f.name, device_type='DWARF_3',
                                object_name='M 31', observation_date='2025-08-05T22:00:00',
                                exposure_time=30.0, observation_year=2025))
        st = src / 'stacked-30_M31.fits'
        st.write_bytes(b'FITS')
        ms.append(make_meta(file_path=str(st), file_name=st.name, device_type='DWARF_3',
                            object_name='M 31', observation_date='2025-08-05T23:00:00',
                            exposure_time=30.0, observation_year=2025))
        out = tmp_path / 'out'
        plan = plan_migration(ms, out)
        stats = execute_plan(plan, dry_run=False, output_root=out)
        assert stats['errors'] == 0
        base = out / '2025' / 'M 31'
        assert (base / 'Lights' / '2025-08-05_30s' / 'frame_000.fits').exists()
        assert (base / 'Finals' / 'Device' / '2025-08-05_30s' / 'stacked-30_M31.fits').exists()
        assert (base / 'Finals' / 'PixInsight').is_dir()      # placeholders at Finals/ level
        assert (base / 'Finals' / 'Siril').is_dir()
        assert not (base / 'Finals' / 'Device' / '2025-08-05_30s' / 'PixInsight').exists()


# ══════════════════════════════════════════════════════════════════════════════
# Rev 4 — --target filter, safe re-runs, disk-space safety
# ══════════════════════════════════════════════════════════════════════════════
import errno
import os
import shutil as _shutil
from collections import namedtuple


def real_meta(tmp_path, folder, name, target='M 31', ts='2025-08-05T22:00:00',
              size=10, ftype='LIGHT', exp=30.0):
    d = tmp_path / 'src' / folder
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(b'x' * size)
    return make_meta(file_path=str(f), file_name=name, object_name=target,
                     observation_date=ts, exposure_time=exp, observation_year=2025,
                     frame_type=ftype)


class TestTargetFilter:
    def metas(self):
        return [lm('a', '2025-08-05T22:00:00', target='M 31'),
                lm('b', '2025-08-05T22:00:00', target='M 16'),
                lm('c', '2025-08-05T22:00:00', target='NGC 281'),
                lm('d', '2025-08-05T22:00:00', target='', ftype='DARK')]

    def targets(self, plan):
        return sorted({a.target for a in plan})

    def test_single_target(self):
        assert self.targets(plan_migration(self.metas(), OUTPUT, target_filter=['M 31'])) == ['M 31']

    def test_string_accepted(self):
        assert self.targets(plan_migration(self.metas(), OUTPUT, target_filter='M 16')) == ['M 16']

    def test_multiple_targets(self):
        plan = plan_migration(self.metas(), OUTPUT, target_filter=['m 31', 'M 16'])
        assert self.targets(plan) == ['M 16', 'M 31']

    def test_shared_calibration_excluded(self):
        plan = plan_migration(self.metas(), OUTPUT, target_filter=['M 31'])
        assert all(a.frame_type != 'DARK' for a in plan)

    def test_no_filter_includes_everything(self):
        assert len(plan_migration(self.metas(), OUTPUT)) == 4

    def test_unknown_target_gives_empty_plan(self):
        assert plan_migration(self.metas(), OUTPUT, target_filter=['M 999']) == []


class TestSafeRerun:
    def make(self, tmp_path, n=5):
        return [real_meta(tmp_path, 'n1', f'f{i}.fits', size=100 + i) for i in range(n)]

    def test_second_run_copies_nothing_and_makes_no_duplicates(self, tmp_path):
        ms = self.make(tmp_path)
        out = tmp_path / 'out'
        plan = plan_migration(ms, out, min_session_minutes=0)
        s1 = execute_plan(plan, dry_run=False, output_root=out)
        s2 = execute_plan(plan, dry_run=False, output_root=out)
        assert s1['copied'] == 5 and s1['already_present'] == 0
        assert s2['copied'] == 0 and s2['already_present'] == 5
        names = [p.name for p in out.rglob('*.fits')]
        assert len(names) == 5 and not any('_2' in n for n in names)

    def test_partial_first_run_is_completed(self, tmp_path):
        ms = self.make(tmp_path)
        out = tmp_path / 'out'
        plan = plan_migration(ms, out, min_session_minutes=0)
        execute_plan(plan[:2], dry_run=False, output_root=out)
        s = execute_plan(plan, dry_run=False, output_root=out)
        assert s['copied'] == 3 and s['already_present'] == 2

    def test_different_file_same_name_still_gets_suffix(self, tmp_path):
        ms = self.make(tmp_path, n=1)
        out = tmp_path / 'out'
        plan = plan_migration(ms, out, min_session_minutes=0)
        dest = plan[0].dest
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b'something else entirely, different size')
        execute_plan(plan, dry_run=False, output_root=out)
        assert dest.exists() and (dest.parent / (dest.stem + '_2.fits')).exists()

    def test_dry_run_reports_already_present(self, tmp_path):
        ms = self.make(tmp_path)
        out = tmp_path / 'out'
        plan = plan_migration(ms, out, min_session_minutes=0)
        execute_plan(plan, dry_run=False, output_root=out)
        s = execute_plan(plan, dry_run=True)
        assert s['already_present'] == 5 and s['copied'] == 0


class TestDiskSpace:
    Usage = namedtuple('Usage', 'total used free')

    def test_fits_when_enough_space(self, tmp_path, monkeypatch):
        ms = [real_meta(tmp_path, 'n1', f'f{i}.fits', size=1000) for i in range(3)]
        plan = plan_migration(ms, tmp_path / 'out', min_session_minutes=0)
        monkeypatch.setattr(_shutil, 'disk_usage', lambda p: self.Usage(0, 0, 10_000))
        ok, needed, free = check_free_space(plan, tmp_path / 'out')
        assert ok and needed == 3000 and free == 10_000

    def test_refuses_when_not_enough_space(self, tmp_path, monkeypatch):
        ms = [real_meta(tmp_path, 'n1', f'f{i}.fits', size=1000) for i in range(3)]
        plan = plan_migration(ms, tmp_path / 'out', min_session_minutes=0)
        monkeypatch.setattr(_shutil, 'disk_usage', lambda p: self.Usage(0, 0, 2_000))
        assert check_free_space(plan, tmp_path / 'out')[0] is False

    def test_already_copied_files_not_counted(self, tmp_path, monkeypatch):
        ms = [real_meta(tmp_path, 'n1', f'f{i}.fits', size=1000) for i in range(3)]
        out = tmp_path / 'out'
        plan = plan_migration(ms, out, min_session_minutes=0)
        execute_plan(plan[:2], dry_run=False, output_root=out)
        monkeypatch.setattr(_shutil, 'disk_usage', lambda p: self.Usage(0, 0, 10_000))
        assert check_free_space(plan, out)[1] == 1000

    def test_move_needs_no_space(self, tmp_path, monkeypatch):
        ms = [real_meta(tmp_path, 'n1', 'f0.fits', size=1000)]
        plan = plan_migration(ms, tmp_path / 'out', operation='move', min_session_minutes=0)
        monkeypatch.setattr(_shutil, 'disk_usage', lambda p: self.Usage(0, 0, 1))
        assert check_free_space(plan, tmp_path / 'out')[1] == 0

    def test_disk_full_stops_run_and_removes_partial(self, tmp_path, monkeypatch):
        ms = [real_meta(tmp_path, 'n1', f'f{i}.fits', size=100) for i in range(5)]
        out = tmp_path / 'out'
        plan = plan_migration(ms, out, min_session_minutes=0)
        real_copy = _shutil.copy2
        calls = {'n': 0}

        def flaky(src, dst, *a, **k):
            calls['n'] += 1
            if calls['n'] == 3:
                open(dst, 'wb').write(b'partial')            # simulate half-written file
                raise OSError(errno.ENOSPC, 'No space left on device')
            return real_copy(src, dst, *a, **k)

        monkeypatch.setattr(_shutil, 'copy2', flaky)
        s = execute_plan(plan, dry_run=False, output_root=out)
        assert s['no_space'] is True and s['copied'] == 2 and calls['n'] == 3
        assert len(list(out.rglob('*.fits'))) == 2            # partial removed, run stopped

    def test_other_errors_do_not_stop_run(self, tmp_path, monkeypatch):
        ms = [real_meta(tmp_path, 'n1', f'f{i}.fits', size=100) for i in range(4)]
        out = tmp_path / 'out'
        plan = plan_migration(ms, out, min_session_minutes=0)
        real_copy = _shutil.copy2
        calls = {'n': 0}

        def flaky(src, dst, *a, **k):
            calls['n'] += 1
            if calls['n'] == 2:
                raise PermissionError(errno.EACCES, 'denied')
            return real_copy(src, dst, *a, **k)

        monkeypatch.setattr(_shutil, 'copy2', flaky)
        s = execute_plan(plan, dry_run=False, output_root=out)
        assert s['errors'] == 1 and s['copied'] == 3 and s['no_space'] is False



# ══════════════════════════════════════════════════════════════════════════════
# Rev 5 — collisions, solar-system targets, year-of-night, 2038, S30 Pro
# ══════════════════════════════════════════════════════════════════════════════
class TestCollisionSafety:
    def two_same_name(self, tmp_path):
        """Two different sessions, same calibration file name, same size AND mtime."""
        a = tmp_path / 'src' / 's1'; b = tmp_path / 'src' / 's2'
        a.mkdir(parents=True); b.mkdir(parents=True)
        fa, fb = a / 'bias_gain_2_bin_1.fits', b / 'bias_gain_2_bin_1.fits'
        fa.write_bytes(b'A' * 100); fb.write_bytes(b'B' * 100)
        os.utime(fa, (1_700_000_000, 1_700_000_000)); os.utime(fb, (1_700_000_000, 1_700_000_000))
        return [make_meta(file_path=str(f), file_name=f.name, frame_type='BIAS',
                          object_name='', observation_year=2026) for f in (fa, fb)]

    def test_same_name_same_size_same_time_both_kept(self, tmp_path):
        out = tmp_path / 'out'
        plan = plan_migration(self.two_same_name(tmp_path), out)
        s = execute_plan(plan, dry_run=False, output_root=out)
        assert s['copied'] == 2 and s['already_present'] == 0
        contents = sorted(p.read_bytes()[:1] for p in out.rglob('*.fits'))
        assert contents == [b'A', b'B']

    def test_rerun_with_collisions_is_idempotent(self, tmp_path):
        out = tmp_path / 'out'
        plan = plan_migration(self.two_same_name(tmp_path), out)
        execute_plan(plan, dry_run=False, output_root=out)
        s2 = execute_plan(plan, dry_run=False, output_root=out)
        assert s2['copied'] == 0 and s2['already_present'] == 2
        assert len(list(out.rglob('*.fits'))) == 2

    def test_dry_run_predicts_collision_copies(self, tmp_path):
        out = tmp_path / 'out'
        plan = plan_migration(self.two_same_name(tmp_path), out)
        s = execute_plan(plan, dry_run=True)
        assert s['copied'] == 2 and s['already_present'] == 0

    def test_resolve_dest_order(self, tmp_path):
        src = tmp_path / 'f.fits'; src.write_bytes(b'x')
        claimed = set()
        d = tmp_path / 'out' / 'f.fits'
        assert _resolve_dest(src, d, claimed) == (d, False)
        assert _resolve_dest(src, d, claimed)[0].name == 'f_2.fits'
        assert _resolve_dest(src, d, claimed)[0].name == 'f_3.fits'


class TestSolarSystem:
    @pytest.mark.parametrize('name,expected', [
        ('Moon', True), ('moon', True), ('Jupiter', True), ('Sun', True),
        ('Comet NEOWISE', True), ('M 31', False), ('Unknown', False), ('Mars Rover', False)])
    def test_detection(self, name, expected):
        assert is_solar_system_target(name) is expected

    def test_moon_not_flagged_for_short_exposure(self):
        ms = [lm('mn', '2026-02-28T20:00:00', target='Moon', exp=0.003125) for _ in range(20)]
        plan = plan_migration(ms, OUTPUT)
        assert all(FOLDER_LIGHTS in a.dest.parts for a in plan)
        assert plan[0].dest.parent.name == '2026-02-28_3.125ms'

    def test_dso_with_short_session_still_flagged(self):
        ms = [lm('x', '2026-02-28T20:00:00', target='M 42', exp=1.0) for _ in range(20)]
        assert all(FOLDER_REVIEW in a.dest.parts for a in plan_migration(ms, OUTPUT))

    @pytest.mark.parametrize('e,label', [(0.000125, '_0.125ms'), (0.0001, '_0.1ms'),
                                         (0.003125, '_3.125ms'), (0.5, '_500ms'),
                                         (1.0, '_1s'), (30.0, '_30s'), (7.5, '_7.5s')])
    def test_exposure_label(self, e, label):
        assert _exposure_label([e]) == label


class TestYearOfNight:
    def test_new_years_eve_session_stays_in_old_year(self):
        ms = ([lm('nye', f'2025-12-31T23:{mm:02d}:00', exp=60.0, target='M 33') for mm in range(0, 60, 6)] +
              [lm('nye', f'2026-01-01T00:{mm:02d}:00', exp=60.0, target='M 33') for mm in range(0, 60, 6)])
        for m in ms:
            m.observation_year = int(m.observation_date[:4])
        plan = plan_migration(ms, OUTPUT)
        assert {a.dest.parts[-5:-1] for a in plan} == {('2025', 'M 33', 'Lights', '2025-12-31_60s')}
        assert {a.year for a in plan} == {'2025'}

    def test_normal_session_year_unchanged(self):
        ms = [lm('n', '2026-03-05T22:00:00', exp=60.0, target='M 42') for _ in range(12)]
        for m in ms: m.observation_year = 2026
        assert {a.year for a in plan_migration(ms, OUTPUT)} == {'2026'}

    def test_calibration_year_still_from_header(self):
        d = lm('c', '2026-03-05T22:00:00', ftype='DARK', target='')
        d.observation_year = 2026
        assert plan_migration([d], OUTPUT)[0].year == '2026'


class TestDwarf2038:
    def test_bad_header_year_falls_back_to_filename_time_and_path_year(self):
        name = 'Barnard 33_30s60_Duo-Band_20260324-203732172_36C.fits'
        ms = [make_meta(file_path=f'/src/2026/s/{name[:-5]}{i}.fits',
                        file_name=name.replace('2037', f'20{i:02d}'),
                        device_type='DWARF_MINI', object_name='Barnard 33',
                        observation_date='2038-01-01T00:07:00', observation_year=None,
                        exposure_time=30.0) for i in range(30, 50)]
        plan = plan_migration(ms, OUTPUT)
        assert {a.year for a in plan} == {'2026'}
        assert {a.dest.parent.name for a in plan} == {'2026-03-24_30s'}

    def test_calibration_with_2038_goes_to_path_year(self):
        d = make_meta(file_path='/src/2026/DWARF Mini/dark_exp_10_gain_60_bin_1_33C_stack_3.fits',
                      file_name='dark_exp_10_gain_60_bin_1_33C_stack_3.fits',
                      frame_type='DARK', object_name='', device_type='DWARF_MINI',
                      observation_year=None)
        assert plan_migration([d], OUTPUT)[0].dest.parts[-4:-1] == ('2026', '_Calibration', 'Dark')


class TestS30ProSessions:
    def test_s30_pro_uses_filename_local_time(self):
        ms = [lm('s30', '2026-10-12T01:31:50', device='SEESTAR_S30_PRO', exp=30.0, target='M 31',
                 name=f'Light_M 31_30.0s_IRCUT_20261011-2031{i:02d}.fit') for i in range(40)]
        plan = plan_migration(ms, OUTPUT)
        assert {a.dest.parent.name for a in plan} == {'2026-10-11_30s'}

    def test_s30_pro_header_utc_fallback(self):
        ms = [lm('s30', '2026-10-12T01:31:50', device='SEESTAR_S30_PRO', exp=30.0,
                 target='M 31', name=f'frame_{i}.fit') for i in range(40)]
        assert {a.dest.parent.name for a in plan_migration(ms, OUTPUT)} == {'2026-10-11_30s'}



# ══════════════════════════════════════════════════════════════════════════════
# Rev 6 — failed_ frames go to _Review individually
# ══════════════════════════════════════════════════════════════════════════════
def session_with_failed(good=40, failed=5, exp=30.0, folder='m33'):
    ms = [lm(folder, f'2025-12-31T21:{i % 60:02d}:00', exp=exp, target='M 33',
             name=f'DWARF_good_{i}.fits') for i in range(good)]
    ms += [lm(folder, f'2025-12-31T22:{i % 60:02d}:00', exp=exp, target='M 33',
              name=f'failed_M 33_{i}.fits') for i in range(failed)]
    return ms


class TestFailedFrames:
    def test_detection(self):
        assert _is_failed_frame(lm('f', None, name='failed_x.fits'))
        assert _is_failed_frame(lm('f', None, name='FAILED_x.fits'))
        assert not _is_failed_frame(lm('f', None, name='light_x.fits'))
        assert not _is_failed_frame(lm('f', None, name='stacked-5_x.fits'))
        assert not _is_failed_frame(lm('f', None, name='failed_x.fits', ftype='DARK'))

    def test_failed_frames_go_to_review_good_stay(self):
        plan = plan_migration(session_with_failed(), OUTPUT)
        lights = [a for a in plan if FOLDER_LIGHTS in a.dest.parts]
        review = [a for a in plan if FOLDER_REVIEW in a.dest.parts]
        assert len(lights) == 40 and len(review) == 5
        assert {a.dest.parent.name for a in review} == {a.dest.parent.name for a in lights}
        assert all(a.review_reason == 'failed frame' for a in review)
        assert all(a.review_reason == '' for a in lights)

    def test_failed_frames_do_not_count_toward_integration(self):
        # 19 good x 30 s = 9.5 min  (+ 30 failed x 30 s that must NOT count)
        plan = plan_migration(session_with_failed(good=19, failed=30), OUTPUT)
        assert all(FOLDER_REVIEW in a.dest.parts for a in plan)
        assert any('min of exposure' in a.review_reason for a in plan if 'failed' not in a.source.name)

    def test_all_failed_session_flagged_whole(self):
        plan = plan_migration(session_with_failed(good=0, failed=40), OUTPUT)
        assert all(FOLDER_REVIEW in a.dest.parts for a in plan)
        assert all(a.review_reason == 'failed capture' for a in plan)

    def test_merged_layout_failed_goes_to_review(self):
        plan = plan_migration(session_with_failed(), OUTPUT, split_nights=False)
        assert sum(FOLDER_REVIEW in a.dest.parts for a in plan) == 5

    def test_summary_counts_session_once_and_reports_percentage(self, capsys):
        print_summary(plan_migration(session_with_failed(good=40, failed=5), OUTPUT))
        out = capsys.readouterr().out
        assert 'Nights        : 1  (0 session(s) flagged for review)' in out
        assert 'Failed frames : 5 moved to _Review in 1 session(s)' in out
        assert '5 failed (11% of that night)' in out and 'consider discarding' not in out

    def test_summary_suggests_discarding_mostly_failed_session(self, capsys):
        print_summary(plan_migration(session_with_failed(good=25, failed=25), OUTPUT))
        out = capsys.readouterr().out
        assert '50% of that night' in out and 'consider discarding that night' in out



# ══════════════════════════════════════════════════════════════════════════════
# Rev 7 — truncated frames go to _Review
# ══════════════════════════════════════════════════════════════════════════════
class TestTruncatedFrames:
    def sess(self, good=40, cut=3, exp=30.0):
        ms = [lm('s', f'2026-03-01T21:{i % 60:02d}:00', exp=exp, target='M 42',
                 name=f'good_{i}.fits') for i in range(good)]
        for i in range(cut):
            m_ = lm('s', f'2026-03-01T22:{i:02d}:00', exp=exp, target='M 42', name=f'cut_{i}.fits')
            m_.truncated = True
            ms.append(m_)
        return ms

    def test_problem_reasons(self):
        t_ = lm('f', None, name='x.fits'); t_.truncated = True
        assert _frame_problem(t_) == 'truncated file'
        assert _frame_problem(lm('f', None, name='failed_x.fits')) == 'failed frame'
        assert _frame_problem(lm('f', None, name='ok.fits')) == ''

    def test_truncated_lights_go_to_review_good_stay(self):
        plan = plan_migration(self.sess(), OUTPUT)
        cut = [a for a in plan if a.source.name.startswith('cut_')]
        good = [a for a in plan if a.source.name.startswith('good_')]
        assert all(FOLDER_REVIEW in a.dest.parts and a.review_reason == 'truncated file' for a in cut)
        assert all(FOLDER_LIGHTS in a.dest.parts for a in good)

    def test_truncated_do_not_count_toward_integration(self):
        plan = plan_migration(self.sess(good=19, cut=30), OUTPUT)         # 9.5 min of good frames
        assert all(FOLDER_REVIEW in a.dest.parts for a in plan)

    def test_truncated_stack_goes_to_review(self):
        ms = self.sess(cut=0)
        st = lm('s', '2026-03-01T22:30:00', name='stacked-40_M42.fits', target='M 42'); st.truncated = True
        plan = plan_migration(ms + [st], OUTPUT)
        assert FOLDER_REVIEW in [a for a in plan if a.source.name == 'stacked-40_M42.fits'][0].dest.parts

    def test_truncated_calibration_goes_to_review(self):
        d = lm('c', None, ftype='DARK', target='', name='dark_exp_10_gain_60_bin_1_33C_stack_3.fits')
        d.truncated = True
        d.observation_year = 2026
        assert plan_migration([d], OUTPUT)[0].dest.parts[-3:-1] == ('2026', FOLDER_REVIEW)

    def test_truncated_calibration_with_target(self):
        d = lm('c', None, ftype='FLAT', target='M 42', name='flat_gain_2_bin_1.fits')
        d.truncated = True
        assert plan_migration([d], OUTPUT)[0].dest.parts[-3:-1] == ('M 42', FOLDER_REVIEW)

    def test_merged_layout(self):
        plan = plan_migration(self.sess(), OUTPUT, split_nights=False)
        assert sum(FOLDER_REVIEW in a.dest.parts for a in plan) == 3

    def test_summary_lists_truncated_files(self, capsys):
        print_summary(plan_migration(self.sess(), OUTPUT))
        out = capsys.readouterr().out
        assert 'Truncated     : 3 file(s) moved to _Review' in out and 'cut_0.fits' in out
        assert 'Nights        : 1  (0 session(s) flagged for review)' in out



# ══════════════════════════════════════════════════════════════════════════════
# Versioning and run logs
# ══════════════════════════════════════════════════════════════════════════════
import re as _re
import subprocess
import sys as _sys
import logging as _lg
from datetime import datetime as _dt
import fits_migrator as _fm

HERE = Path(__file__).parent


class TestVersioning:
    def test_format_is_semver(self):
        assert _re.fullmatch(r'\d+\.\d+\.\d+', MIGRATOR_VERSION)

    def test_extractor_and_migrator_versions_match(self):
        from fits_metadata_extractor import __version__ as ev
        assert ev == MIGRATOR_VERSION == EXTRACTOR_VERSION

    def test_changelog_top_entry_is_current_version(self):
        text = (HERE / 'CHANGELOG.md').read_text()
        first = _re.search(r'^## \[(\d+\.\d+\.\d+)\]', text, _re.MULTILINE)
        assert first and first.group(1) == MIGRATOR_VERSION, (
            "CHANGELOG.md's newest entry must be the current version — "
            "update it when you bump __version__")

    def test_banner(self):
        assert version_banner() == f"fits_migrator {MIGRATOR_VERSION} (extractor {EXTRACTOR_VERSION})"

    def test_no_mismatch_warning_when_equal(self):
        assert version_mismatch() is None

    def test_mismatch_warning(self, monkeypatch):
        monkeypatch.setattr(_fm, 'EXTRACTOR_VERSION', '0.1.0')
        msg = version_mismatch()
        assert msg and '0.1.0' in msg and MIGRATOR_VERSION in msg

    def test_cli_version_flag(self):
        for script, expect in (('fits_migrator.py', f'fits_migrator {MIGRATOR_VERSION}'),
                               ('fits_metadata_extractor.py', f'fits_metadata_extractor {MIGRATOR_VERSION}'),
                               ('session_report.py', f'fits_migrator {MIGRATOR_VERSION}')):
            if not (HERE / script).exists():
                continue
            r = subprocess.run([_sys.executable, str(HERE / script), '--version'],
                               capture_output=True, text=True, cwd=HERE)
            assert r.returncode == 0 and expect in r.stdout, (script, r.stdout, r.stderr)

    def test_run_prints_banner_and_log_names(self, tmp_path):
        (tmp_path / 'src').mkdir()
        r = subprocess.run([_sys.executable, str(HERE / 'fits_migrator.py'),
                            str(tmp_path / 'src'), str(tmp_path / 'out'), '--summary'],
                           capture_output=True, text=True, cwd=tmp_path,
                           env={**__import__('os').environ, 'PYTHONPATH': str(HERE)})
        assert version_banner() in r.stdout
        assert 'Logs      : surprises.log (scan), migration.log (plan / copy)' in r.stdout
        assert (tmp_path / 'migration.log').exists()
        assert MIGRATOR_VERSION in (tmp_path / 'migration.log').read_text()
        assert MIGRATOR_VERSION in (tmp_path / 'surprises.log').read_text()


class TestRunLog:
    def cleanup(self):
        for h in [h for h in _fm.logger.handlers if h.get_name() == 'run-log']:
            _fm.logger.removeHandler(h); h.close()

    def test_dry_run_log_name(self, tmp_path):
        try:
            assert attach_run_log(True, tmp_path).name == 'migration.log'
        finally:
            self.cleanup()

    def test_execute_log_is_timestamped_and_kept(self, tmp_path):
        try:
            p = attach_run_log(False, tmp_path, now=_dt(2026, 10, 12, 20, 45, 7))
            assert p.name == 'migration_20261012_204507.log'
        finally:
            self.cleanup()

    def test_header_has_version_environment_and_mode(self, tmp_path):
        try:
            p = attach_run_log(False, tmp_path)
            _fm.logger.debug('COPY a -> b')
            for h in _fm.logger.handlers: h.flush()
            text = p.read_text()
            assert version_banner() in text and 'Python' in text and 'astropy' in text
            assert 'mode=execute' in text and 'COPY a -> b' in text
        finally:
            self.cleanup()

    def test_second_run_replaces_handler(self, tmp_path):
        try:
            attach_run_log(True, tmp_path)
            attach_run_log(True, tmp_path)
            assert sum(h.get_name() == 'run-log' for h in _fm.logger.handlers) == 1
        finally:
            self.cleanup()



# ══════════════════════════════════════════════════════════════════════════════
# 0.8.1 — summary counts only the files that carry the reason
# ══════════════════════════════════════════════════════════════════════════════
def night_with_aborted_start(good_each=594):
    """Like M 45 on 2026-01-02: a 2-frame aborted start plus two real sessions, same night + exposure."""
    ms = [lm('aborted', '2026-01-02T18:58:00', name=f'a{i}.fits', target='M 45', exp=10.0) for i in range(2)]
    ms += [lm('s1', f'2026-01-02T19:{i % 60:02d}:00', name=f's1_{i}.fits', target='M 45', exp=10.0) for i in range(good_each)]
    ms += [lm('s2', f'2026-01-02T21:{i % 60:02d}:00', name=f's2_{i}.fits', target='M 45', exp=10.0) for i in range(good_each)]
    return ms


class TestSummaryCountsFlaggedFilesOnly:
    def test_routing_is_per_session(self):
        plan = plan_migration(night_with_aborted_start(), OUTPUT)
        assert sum(FOLDER_REVIEW in a.dest.parts for a in plan) == 2
        assert sum(FOLDER_LIGHTS in a.dest.parts for a in plan) == 1188

    def test_summary_shows_2_files_not_the_nights_total(self, capsys):
        print_summary(plan_migration(night_with_aborted_start(), OUTPUT))
        out = capsys.readouterr().out
        assert '2 file(s) — only' in out and '1190 file(s)' not in out
        assert 'Total planned : 1190' in out

    def test_two_sessions_with_different_reasons_listed_separately(self, capsys):
        ms = night_with_aborted_start(good_each=60)
        ms += [lm('u', '2026-01-02T20:00:00', name=f'u{i}.fits', target='Unknown', exp=10.0) for i in range(50)]
        print_summary(plan_migration(ms, OUTPUT))
        out = capsys.readouterr().out
        assert '2 file(s) — only' in out and '50 file(s) — target is Unknown' in out

    def test_truncated_listing_shows_folder_names(self, capsys):
        a = lm('session_a', None, ftype='FLAT', target='', name='flat_gain_2_bin_1_ir_0.fits')
        b = lm('session_b', None, ftype='FLAT', target='', name='flat_gain_2_bin_1_ir_0.fits')
        a.truncated = b.truncated = True
        print_summary(plan_migration([a, b], OUTPUT))
        out = capsys.readouterr().out
        assert 'session_a/flat_gain_2_bin_1_ir_0.fits' in out and 'session_b/flat_gain_2_bin_1_ir_0.fits' in out
