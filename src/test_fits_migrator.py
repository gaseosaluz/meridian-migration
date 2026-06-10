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
        """Running twice should not overwrite — second file gets _2 suffix."""
        plan, out = simple_plan
        execute_plan(plan, dry_run=False, output_root=out)
        execute_plan(plan, dry_run=False, output_root=out)   # second run
        lights_dir = out / '2025' / 'NGC 281' / 'Lights'
        files = list(lights_dir.iterdir())
        assert len(files) == 2
        names = {f.name for f in files}
        assert 'light.fits'   in names
        assert 'light_2.fits' in names


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
        plan = plan_migration(results, out)
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
