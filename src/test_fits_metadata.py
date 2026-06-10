#!/usr/bin/env python3
"""
test_fits_metadata.py

Unit tests for fits_metadata_extractor.py

Fixtures use REAL header values from Ed's actual FITS files.
Rev 3 adds tests for:
  - New filename prefixes: unknown_ / failed_ / stacked-
  Rev 4:
  - stacked_ prefix (Seestar format), IMAGEW/IMAGEH headers,
    duplicate-handler guard for setup_logging()
  - detect_device() now returns 3-tuple (device, warnings, source)
  - _is_known_header() helper including WCS prefix families
  - Log file opened in write mode ('w')
  - print_summary() includes warning count

Run with:
    pytest test_fits_metadata.py -v
"""

import logging
import pytest
from pathlib import Path
from astropy.io import fits

from fits_metadata_extractor import (
    logger,
    detect_device,
    detect_device_from_path,
    classify_frame,
    classify_from_filename,
    parse_calibration_filename,
    get_temperature,
    get_exposure,
    parse_observation_date,
    setup_logging,
    _is_known_header,
    DEVICE_SIGNATURES,
    FITS_EXTENSIONS,
    FRAME_FILENAME_PREFIXES,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def make_header(kvs: dict) -> fits.Header:
    h = fits.Header()
    for k, v in kvs.items():
        h[k] = v
    return h


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures — real header values
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def dwarf3_light_2025():
    """C 11 (Veil Nebula) — DWARF 3, old firmware, August 2025."""
    return make_header({
        'TELESCOP': 'DWARFIII', 'INSTRUME': 'DWARFIII', 'ORIGIN': 'DWARFLAB',
        'OBJECT': 'C 11', 'EXPTIME': 30.0, 'GAIN': 60, 'FILTER': 'Duo-Band',
        'DET-TEMP': 40, 'RA': 350.2199, 'DEC': 61.28683,
        'NAXIS1': 3856, 'NAXIS2': 2180, 'BITPIX': 16,
        'BAYERPAT': 'RGGB', 'FOCALLEN': 150.0, 'XPIXSZ': 2.0, 'YPIXSZ': 2.0,
        'DATE-OBS': '2025-08-19T02:56:12.413',
    })


@pytest.fixture
def dwarf3_light_2026():
    """Barnard 33 (Horsehead) — DWARF 3, firmware 1.4.15.2, March 2026."""
    return make_header({
        'TELESCOP': 'DWARF 3', 'INSTRUME': 'DWARF 3', 'ORIGIN': 'DWARFLAB',
        'OBJECT': 'Barnard 33', 'EXPTIME': 30.0, 'GAIN': 60, 'FILTER': 'Duo-Band',
        'DET-TEMP': 36, 'RA': 85.24583, 'DEC': -2.45833,
        'NAXIS1': 3856, 'NAXIS2': 2180, 'BITPIX': 16,
        'BAYERPAT': 'RGGB', 'FOCALLEN': 150.0,
        'FIRMWARE': '1.4.15.2', 'MACADDR': '5478C93AC834', 'EQMODE': 1,
        'DATE-OBS': '2026-03-24T20:37:32.172',
    })


@pytest.fixture
def dwarf3_dark():
    """Factory dark frame — DWARF 3, January 2026, EXPTIME=10s."""
    return make_header({
        'TELESCOP': 'DWARF 3', 'INSTRUME': 'DWARF 3', 'ORIGIN': 'DWARFLAB',
        'OBJECT': '', 'EXPTIME': 10.0, 'GAIN': 60, 'FILTER': '',
        'DET-TEMP': 35, 'RA': 0.0, 'DEC': 0.0,
        'NAXIS1': 3856, 'NAXIS2': 2180, 'BITPIX': 16,
        'BAYERPAT': 'RGGB', 'FOCALLEN': 150.0,
        'DATE-OBS': '2026-01-02T18:49:51.912',
    })


@pytest.fixture
def dwarf3_calib_stripped():
    """
    Calibration frame with stripped headers — real behaviour from scan.
    No TELESCOP, no OBJECT, no RA/DEC, no IMAGETYP.
    Device/frame must be inferred from path/filename.
    """
    return make_header({
        'NAXIS1': 3856,
        'NAXIS2': 2180,
        'BITPIX': 16,
    })


@pytest.fixture
def dwarf_mini_calib_stripped():
    """DWARF Mini calibration frame — stripped headers, mini sensor size."""
    return make_header({
        'NAXIS1': 1920,
        'NAXIS2': 1080,
        'BITPIX': 16,
    })


@pytest.fixture
def dwarf_mini_light():
    """Barnard 33 — DWARF Mini, firmware 1.0.25.2, March 2026."""
    return make_header({
        'TELESCOP': 'DWARF mini', 'INSTRUME': 'DWARF mini', 'ORIGIN': 'DWARFLAB',
        'OBJECT': 'Barnard 33', 'EXPTIME': 15.0, 'GAIN': 80, 'FILTER': 'Duo-Band',
        'DET-TEMP': 31, 'RA': 85.24583, 'DEC': -2.45833,
        'NAXIS1': 1920, 'NAXIS2': 1080, 'BITPIX': 16,
        'BAYERPAT': 'RGGB', 'FOCALLEN': 150.0,
        'FIRMWARE': '1.0.25.2', 'MACADDR': '40D95ADC0CEE', 'EQMODE': 0,
        'DATE-OBS': '2026-03-20T20:45:11.261',
    })


@pytest.fixture
def seestar_s50_light():
    """NGC 281 (Pacman Nebula) — Seestar S50, September 2025."""
    return make_header({
        'TELESCOP': 'S50_40d24ed8', 'INSTRUME': 'Seestar S50',
        'CREATOR': 'ZWO Seestar S50', 'PRODUCER': 'ZWO',
        'OBJECT': 'NGC 281', 'IMAGETYP': 'Light',
        'EXPTIME': 10.0, 'EXPOSURE': 10.0, 'GAIN': 80, 'FILTER': 'LP',
        'CCD-TEMP': 27.9375, 'RA': 13.633335, 'DEC': 56.775833,
        'NAXIS1': 1080, 'NAXIS2': 1920, 'BITPIX': 16,
        'BAYERPAT': 'GRBG', 'FOCALLEN': 250.0, 'XPIXSZ': 2.9, 'YPIXSZ': 2.9,
        'SITELAT': 30.5038, 'SITELONG': -97.7429,
        'DATE-OBS': '2025-09-29T06:01:02.382641',
        'PROGRAM': '4.70', 'EQMODE': 0,
    })


# ──────────────────────────────────────────────────────────────────────────────
# Device Detection — header-based
# ── Rev 3: detect_device() returns 3-tuple (device, warnings, source) ─────────
# ──────────────────────────────────────────────────────────────────────────────

class TestDeviceDetectionHeader:

    def test_dwarf3_old_firmware(self, dwarf3_light_2025):
        device, warnings, source = detect_device(dwarf3_light_2025)
        assert device == 'DWARF_3' and warnings == [] and source == 'header'

    def test_dwarf3_new_firmware(self, dwarf3_light_2026):
        device, warnings, source = detect_device(dwarf3_light_2026)
        assert device == 'DWARF_3' and warnings == [] and source == 'header'

    def test_dwarf_mini_lowercase_m(self, dwarf_mini_light):
        device, warnings, source = detect_device(dwarf_mini_light)
        assert device == 'DWARF_MINI' and warnings == [] and source == 'header'

    def test_dwarf_mini_not_confused_with_dwarf3(self, dwarf_mini_light):
        device, _, _src = detect_device(dwarf_mini_light)
        assert device != 'DWARF_3'

    def test_seestar_s50_via_creator(self, seestar_s50_light):
        device, warnings, source = detect_device(seestar_s50_light)
        assert device == 'SEESTAR_S50' and warnings == [] and source == 'header'

    def test_seestar_serial_in_telescop_still_detected(self, seestar_s50_light):
        assert seestar_s50_light['TELESCOP'].startswith('S50_')
        device, _, _src = detect_device(seestar_s50_light)
        assert device == 'SEESTAR_S50'

    def test_unknown_device_warns(self):
        h = make_header({'TELESCOP': 'FutureScopeX 9000', 'NAXIS1': 9999, 'NAXIS2': 9999})
        device, warnings, source = detect_device(h)
        assert device == 'UNKNOWN' and len(warnings) == 1 and source == 'unknown'

    def test_dimension_fallback_warns(self):
        h = make_header({'TELESCOP': 'DWARF-III-V2', 'NAXIS1': 3856, 'NAXIS2': 2180})
        device, warnings, source = detect_device(h)
        assert device == 'DWARF_3' and len(warnings) == 1 and source == 'dimensions'
        assert 'DEVICE_SIGNATURES' in warnings[0]

    def test_source_field_is_header_for_known_devices(self, dwarf3_light_2025,
                                                       dwarf_mini_light,
                                                       seestar_s50_light):
        for hdr in (dwarf3_light_2025, dwarf_mini_light, seestar_s50_light):
            _, _, source = detect_device(hdr)
            assert source == 'header'


# ──────────────────────────────────────────────────────────────────────────────
# Device Detection — path-based
# ──────────────────────────────────────────────────────────────────────────────

class TestDeviceDetectionPath:

    def test_dwarf3_path_detected(self):
        p = Path('/Users/edm/Astronomy/Astrocapture/2026/DWARF 3/DWARF_DARK/'
                 'tele_exp_10_gain_60_bin_1_2026-01-02/dark_001.fits')
        assert detect_device_from_path(p) == 'DWARF_3'

    def test_dwarf_mini_path_detected(self):
        p = Path('/Users/edm/Astronomy/Astrocapture/2026/DWARF Mini/'
                 'March 2026/DWARF_RAW_TELE_Barnard 33/dark_001.fits')
        assert detect_device_from_path(p) == 'DWARF_MINI'

    def test_seestar_path_detected(self):
        p = Path('/Users/edm/Astronomy/Astrocapture/2025/'
                 'Seestar S50/Objects/NGC 281_sub/sub_0001.fit')
        assert detect_device_from_path(p) == 'SEESTAR_S50'

    def test_unknown_path_returns_none(self):
        p = Path('/some/other/directory/file.fits')
        assert detect_device_from_path(p) is None

    def test_path_detection_used_for_stripped_header(self, dwarf3_calib_stripped):
        """
        Stripped calibration header + known device path → correct device,
        no spurious 'possible firmware rename' warning.
        """
        p = Path('/Users/edm/Astronomy/Astrocapture/2026/DWARF 3/'
                 'DWARF_DARK/dark_exp_30_gain_60_bin_1_34C_stack_1.fits')
        device, warnings, source = detect_device(dwarf3_calib_stripped, p)
        assert device == 'DWARF_3'
        assert source == 'path'
        assert not any('firmware rename' in w for w in warnings)

    def test_mini_calib_no_false_firmware_warning(self, dwarf_mini_calib_stripped):
        p = Path('/Users/edm/Astronomy/Astrocapture/2026/DWARF Mini/'
                 'March 2026/bias_gain_2_bin_1.fits')
        device, warnings, source = detect_device(dwarf_mini_calib_stripped, p)
        assert device == 'DWARF_MINI'
        assert source == 'path'
        assert not any('firmware rename' in w for w in warnings)

    def test_path_beats_dimension_fallback(self, dwarf3_calib_stripped):
        """Path detection (silent) must take priority over dimension fallback (noisy)."""
        p = Path('/some/path/DWARF 3/calibration/dark.fits')
        _, warnings, source = detect_device(dwarf3_calib_stripped, p)
        assert source == 'path'
        assert not any('firmware rename' in w for w in warnings)


# ──────────────────────────────────────────────────────────────────────────────
# Frame Classification — header-based
# ──────────────────────────────────────────────────────────────────────────────

class TestFrameClassificationHeader:

    def test_seestar_imagetyp_light(self, seestar_s50_light):
        frame, warnings = classify_frame(seestar_s50_light)
        assert frame == 'LIGHT' and warnings == []

    def test_imagetyp_dark(self):
        frame, _ = classify_frame(make_header({'IMAGETYP': 'Dark'}))
        assert frame == 'DARK'

    def test_imagetyp_flat(self):
        frame, _ = classify_frame(make_header({'IMAGETYP': 'Flat'}))
        assert frame == 'FLAT'

    def test_imagetyp_flat_field_normalised(self):
        frame, _ = classify_frame(make_header({'IMAGETYP': 'Flat Field'}))
        assert frame == 'FLAT'

    def test_imagetyp_bias(self):
        frame, _ = classify_frame(make_header({'IMAGETYP': 'Bias'}))
        assert frame == 'BIAS'

    def test_imagetyp_offset_to_bias(self):
        frame, _ = classify_frame(make_header({'IMAGETYP': 'Offset'}))
        assert frame == 'BIAS'

    def test_imagetyp_case_insensitive(self):
        frame, _ = classify_frame(make_header({'IMAGETYP': 'LIGHT'}))
        assert frame == 'LIGHT'

    def test_dwarf3_light_by_object(self, dwarf3_light_2025):
        frame, warnings = classify_frame(dwarf3_light_2025)
        assert frame == 'LIGHT' and warnings == []

    def test_dwarf3_dark_by_ra_dec(self, dwarf3_dark):
        frame, warnings = classify_frame(dwarf3_dark)
        assert frame == 'DARK' and warnings == []

    def test_unrecognised_imagetyp_falls_through(self):
        h = make_header({'IMAGETYP': 'Experimental', 'OBJECT': 'M42', 'RA': 10.0, 'DEC': 5.0})
        frame, warnings = classify_frame(h)
        assert len(warnings) == 1
        assert 'Unrecognised IMAGETYP' in warnings[0]
        assert frame == 'LIGHT'


# ──────────────────────────────────────────────────────────────────────────────
# Frame Classification — filename-based
# ── Rev 3: added unknown_ / failed_ / stacked- cases ─────────────────────────
# ──────────────────────────────────────────────────────────────────────────────

class TestClassifyFromFilename:

    @pytest.mark.parametrize("filename,expected", [
        # ── original prefixes ─────────────────────────────────────────────────
        ('bias_gain_2_bin_1.fits',                                  'BIAS'),
        ('dark_exp_30.000000_gain_60_bin_1_34C_stack_1.fits',       'DARK'),
        ('dark_exp_15.000000_gain_60_bin_1_34C_stack_3.fits',       'DARK'),
        ('flat_gain_2_bin_1_ir_0.fits',                             'FLAT'),
        ('flat_gain_2_bin_1.fits',                                  'FLAT'),
        ('raw_10s_60_0000_20260102-184951912_35C.fits',             'DARK'),
        ('light_NGC 281_10.0s_LP_20250929-010116.fit',              'LIGHT'),
        ('BIAS_GAIN_2_BIN_1.fits',                                  'BIAS'),   # uppercase
        ('Barnard 33_30s60_Duo-Band_20260324-203732172_36C.fits',   None),     # no known prefix
        ('some_random_file.fits',                                   None),
        # ── Rev 3: new prefixes ───────────────────────────────────────────────
        ('unknown_abc123.fits',                                     'UNKNOWN'),
        ('Unknown_ABC123.fits',                                     'UNKNOWN'),  # case-insensitive
        ('failed_capture_20260401.fits',                            'UNKNOWN'),
        ('FAILED_capture.fits',                                     'UNKNOWN'),  # case-insensitive
        ('stacked-16_NGC281_LP_20251001.fits',                      'LIGHT'),
        ('stacked-32_M42_Duo-Band_20260115.fits',                   'LIGHT'),
        ('STACKED-8_target.fits',                                   'LIGHT'),   # case-insensitive
        # ── Seestar Light_ prefix (capital L — covered because lower() is used) ─
        ('Light_NGC 281_10.0s_LP_20250929-062845.fit',              'LIGHT'),
        # ── Rev 4: Seestar Stacked_N_ format (underscore separator) ─────────────
        ('Stacked_492_M 57_10.0s_LP_20250825-010001.fit',           'LIGHT'),
        ('Stacked_16_NGC 281_10.0s_LP_20251001-020000.fit',         'LIGHT'),
        ('STACKED_8_target.fits',                                   'LIGHT'),   # case-insensitive
    ])
    def test_classify_from_filename(self, filename, expected):
        assert classify_from_filename(filename) == expected

    def test_stripped_header_classified_by_dark_filename(self, dwarf3_calib_stripped):
        """Stripped header + dark filename → DARK, no warning."""
        frame, warnings = classify_frame(
            dwarf3_calib_stripped,
            filename='dark_exp_30.000000_gain_60_bin_1_34C_stack_1.fits')
        assert frame == 'DARK'
        assert warnings == []

    def test_stripped_header_classified_by_bias_filename(self, dwarf3_calib_stripped):
        frame, warnings = classify_frame(
            dwarf3_calib_stripped,
            filename='bias_gain_2_bin_1.fits')
        assert frame == 'BIAS'
        assert warnings == []

    def test_stripped_header_classified_by_flat_filename(self, dwarf3_calib_stripped):
        frame, warnings = classify_frame(
            dwarf3_calib_stripped,
            filename='flat_gain_2_bin_1_ir_0.fits')
        assert frame == 'FLAT'
        assert warnings == []

    def test_stripped_header_classified_by_raw_dark_filename(self, dwarf3_calib_stripped):
        frame, warnings = classify_frame(
            dwarf3_calib_stripped,
            filename='raw_10s_60_0000_20260102-184951912_35C.fits')
        assert frame == 'DARK'
        assert warnings == []

    def test_unknown_prefix_silent_no_warning(self, dwarf3_calib_stripped):
        """unknown_ files should be classified silently — no WARNING emitted."""
        frame, warnings = classify_frame(
            dwarf3_calib_stripped,
            filename='unknown_abc123.fits')
        assert frame == 'UNKNOWN'
        assert warnings == []           # silent — no device-layer warning

    def test_failed_prefix_silent_no_warning(self, dwarf3_calib_stripped):
        """failed_ capture files should be classified silently."""
        frame, warnings = classify_frame(
            dwarf3_calib_stripped,
            filename='failed_capture_20260401.fits')
        assert frame == 'UNKNOWN'
        assert warnings == []

    def test_stacked_prefix_classified_as_light(self, dwarf3_calib_stripped):
        """stacked-N_ files are device-produced stacks — treat as LIGHT."""
        frame, warnings = classify_frame(
            dwarf3_calib_stripped,
            filename='stacked-16_NGC281_LP_20251001.fits')
        assert frame == 'LIGHT'
        assert warnings == []

    def test_stacked_varying_count(self, dwarf3_calib_stripped):
        """Stacked count (8, 16, 32, …) should not affect classification."""
        for count in (4, 8, 16, 32, 64):
            frame, _ = classify_frame(
                dwarf3_calib_stripped,
                filename=f'stacked-{count}_target_LP.fits')
            assert frame == 'LIGHT', f"stacked-{count}_ should be LIGHT"

    def test_truly_unknown_still_warns(self, dwarf3_calib_stripped):
        """No header info AND no recognisable filename → UNKNOWN with warning."""
        frame, warnings = classify_frame(
            dwarf3_calib_stripped,
            filename='mystery_file.fits')
        assert frame == 'UNKNOWN'
        assert len(warnings) == 1


# ──────────────────────────────────────────────────────────────────────────────
# Calibration Filename Metadata Parsing
# ──────────────────────────────────────────────────────────────────────────────

class TestCalibrationFilenameParsing:

    def test_dark_stack_full_metadata(self):
        result = parse_calibration_filename(
            'dark_exp_30.000000_gain_60_bin_1_34C_stack_1.fits')
        assert result['exposure_time'] == 30.0
        assert result['gain']          == 60
        assert result['temperature']   == 34.0

    def test_dark_stack_15s(self):
        result = parse_calibration_filename(
            'dark_exp_15.000000_gain_60_bin_1_34C_stack_3.fits')
        assert result['exposure_time'] == 15.0
        assert result['gain']          == 60
        assert result['temperature']   == 34.0

    def test_dark_stack_60s(self):
        result = parse_calibration_filename(
            'dark_exp_60.000000_gain_60_bin_1_34C_stack_1.fits')
        assert result['exposure_time'] == 60.0

    def test_raw_dark_sub_metadata(self):
        result = parse_calibration_filename(
            'raw_10s_60_0000_20260102-184951912_35C.fits')
        assert result['exposure_time'] == 10.0
        assert result['gain']          == 60
        assert result['temperature']   == 35.0

    def test_bias_gain_only(self):
        result = parse_calibration_filename('bias_gain_2_bin_1.fits')
        assert result['gain'] == 2
        assert 'exposure_time' not in result
        assert 'temperature'   not in result

    def test_flat_gain_only(self):
        result = parse_calibration_filename('flat_gain_2_bin_1_ir_0.fits')
        assert result['gain'] == 2

    def test_flat_no_ir_suffix(self):
        result = parse_calibration_filename('flat_gain_2_bin_1.fits')
        assert result['gain'] == 2

    def test_light_frame_returns_empty(self):
        """Light frame filenames carry no calibration metadata."""
        result = parse_calibration_filename(
            'Barnard 33_30s60_Duo-Band_20260324-203732172_36C.fits')
        assert result == {}

    def test_negative_temperature_parsed(self):
        """Cold-weather darks may have negative temperatures."""
        result = parse_calibration_filename(
            'dark_exp_30.000000_gain_60_bin_1_-5C_stack_1.fits')
        assert result['temperature'] == -5.0

    def test_exposure_mismatch_detectable(self):
        """
        This is the dark-matching scenario we discovered:
        lights at 30s need matching darks — a 10s dark is NOT a match.
        Parsing both filenames gives us the data to detect the mismatch.
        """
        dark_10s = parse_calibration_filename(
            'dark_exp_10.000000_gain_60_bin_1_34C_stack_1.fits')
        dark_30s = parse_calibration_filename(
            'dark_exp_30.000000_gain_60_bin_1_34C_stack_1.fits')
        assert dark_10s['exposure_time'] != dark_30s['exposure_time']
        assert dark_10s['exposure_time'] == 10.0
        assert dark_30s['exposure_time'] == 30.0


# ──────────────────────────────────────────────────────────────────────────────
# Temperature Extraction
# ──────────────────────────────────────────────────────────────────────────────

class TestTemperatureExtraction:

    def test_dwarf3_det_temp(self, dwarf3_light_2025):
        assert get_temperature(dwarf3_light_2025) == 40.0

    def test_seestar_ccd_temp(self, seestar_s50_light):
        assert get_temperature(seestar_s50_light) == pytest.approx(27.9375, rel=1e-4)

    def test_dwarf_mini_temp(self, dwarf_mini_light):
        assert get_temperature(dwarf_mini_light) == 31.0

    def test_no_temp_returns_none(self):
        assert get_temperature(make_header({'OBJECT': 'M42'})) is None

    def test_texas_summer_heat(self, dwarf3_light_2025):
        assert get_temperature(dwarf3_light_2025) == 40.0


# ──────────────────────────────────────────────────────────────────────────────
# Exposure Extraction
# ──────────────────────────────────────────────────────────────────────────────

class TestExposureExtraction:

    def test_dwarf3_30s(self, dwarf3_light_2025):
        assert get_exposure(dwarf3_light_2025) == 30.0

    def test_dwarf3_dark_10s(self, dwarf3_dark):
        assert get_exposure(dwarf3_dark) == 10.0

    def test_seestar_exptime_priority(self, seestar_s50_light):
        assert get_exposure(seestar_s50_light) == 10.0

    def test_no_exposure_returns_none(self):
        assert get_exposure(make_header({'OBJECT': 'M42'})) is None

    def test_light_dark_exposure_mismatch_detectable(self, dwarf3_light_2025, dwarf3_dark):
        assert get_exposure(dwarf3_light_2025) != get_exposure(dwarf3_dark)


# ──────────────────────────────────────────────────────────────────────────────
# Date Parsing
# ──────────────────────────────────────────────────────────────────────────────

class TestDateParsing:

    def test_dwarf3_2025(self, dwarf3_light_2025):
        _, year, month = parse_observation_date(dwarf3_light_2025)
        assert year == 2025 and month == 8

    def test_dwarf3_2026(self, dwarf3_light_2026):
        _, year, month = parse_observation_date(dwarf3_light_2026)
        assert year == 2026 and month == 3

    def test_seestar_microseconds(self, seestar_s50_light):
        date_str, year, month = parse_observation_date(seestar_s50_light)
        assert year == 2025 and month == 9 and '.' in date_str

    def test_missing_date_all_none(self):
        date_str, year, month = parse_observation_date(make_header({}))
        assert (date_str, year, month) == (None, None, None)


# ──────────────────────────────────────────────────────────────────────────────
# Known-header filtering — Rev 3 new section
# ──────────────────────────────────────────────────────────────────────────────

class TestKnownHeaderFiltering:
    """Verify that the two-layer known-header check (_KNOWN_HEADERS +
    _KNOWN_HEADER_PREFIXES) correctly suppresses expected headers."""

    # ── Exact-match headers ───────────────────────────────────────────────────
    def test_standard_fits_keywords_known(self):
        for kw in ('SIMPLE', 'BITPIX', 'NAXIS', 'NAXIS1', 'NAXIS2', 'END'):
            assert _is_known_header(kw), f"'{kw}' should be known"

    def test_device_headers_known(self):
        for kw in ('TELESCOP', 'INSTRUME', 'CREATOR', 'FIRMWARE', 'MACADDR'):
            assert _is_known_header(kw)

    def test_capture_headers_known(self):
        for kw in ('EXPTIME', 'GAIN', 'FILTER', 'DATE-OBS', 'DET-TEMP', 'CCD-TEMP'):
            assert _is_known_header(kw)

    def test_wcs_fixed_headers_known(self):
        for kw in ('WCSAXES', 'RADESYS', 'EQUINOX', 'LONPOLE', 'LATPOLE',
                   'CTYPE1', 'CTYPE2', 'CRVAL1', 'CRVAL2', 'CRPIX1', 'CRPIX2',
                   'CD1_1', 'CD1_2', 'CD2_1', 'CD2_2',
                   'PC1_1', 'PC1_2', 'PC2_1', 'PC2_2'):
            assert _is_known_header(kw), f"WCS header '{kw}' should be known"

    # ── Prefix-family headers (variable suffix) ───────────────────────────────
    def test_seestar_corner_coords_known(self):
        """CRTLxx, CRTRxx, CRBLxx, CRBRxx are Seestar corner-coord headers."""
        for kw in ('CRTL01', 'CRTL02', 'CRTR01', 'CRTR02',
                   'CRBL01', 'CRBL02', 'CRBR01', 'CRBR02',
                   'CRTL1', 'CRTR1', 'CRBL1', 'CRBR1'):
            assert _is_known_header(kw), f"Corner-coord header '{kw}' should be known"

    def test_sip_coefficients_known(self):
        """SIP distortion polynomial coefficients: A_2_0, AP_1_1, B_0_2 …"""
        for kw in ('A_2_0', 'A_1_1', 'AP_1_0', 'B_0_2', 'BP_2_0', 'A_ORDER', 'B_ORDER'):
            assert _is_known_header(kw), f"SIP header '{kw}' should be known"

    def test_pv_ps_coefficients_known(self):
        for kw in ('PV1_1', 'PV2_3', 'PS1_0', 'PS2_1'):
            assert _is_known_header(kw), f"WCS projection header '{kw}' should be known"

    def test_truly_unknown_not_suppressed(self):
        """Headers that are genuinely new should NOT be filtered."""
        for kw in ('NEWKEY', 'FUTRSCOP', 'MYSTERY1', 'ZZZZZZ'):
            assert not _is_known_header(kw), f"'{kw}' should be unknown"

    def test_case_insensitive_matching(self):
        """Header names should match regardless of case."""
        assert _is_known_header('telescop')
        assert _is_known_header('TELESCOP')
        assert _is_known_header('crtl01')
        assert _is_known_header('CRTL01')

    def test_rev4_imagew_imageh_known(self):
        """Rev 4: Seestar non-standard dimension headers must not flood the log."""
        assert _is_known_header('IMAGEW'), (
            'IMAGEW should be in _KNOWN_HEADERS — Seestar writes it on every frame')
        assert _is_known_header('IMAGEH'), (
            'IMAGEH should be in _KNOWN_HEADERS — Seestar writes it on every frame')
        assert _is_known_header('imagew')   # case-insensitive
        assert _is_known_header('imageh')


# ──────────────────────────────────────────────────────────────────────────────
# Logging configuration — Rev 3
# ──────────────────────────────────────────────────────────────────────────────

class TestLoggingConfiguration:

    def test_log_file_opened_in_write_mode(self, tmp_path):
        """
        Rev 3 requirement: surprises.log must be opened in 'w' (write) mode
        so each scan produces a fresh file, not appending to previous runs.

        Rev 4 note: the dup-handler guard means a second call to setup_logging()
        does NOT add a new FileHandler when one already exists, so we verify the
        mode of the already-attached FileHandler rather than adding a fresh one.
        """
        # The module-level setup_logging() call attached the initial FileHandler.
        file_handlers = [
            h for h in logger.handlers
            if isinstance(h, logging.FileHandler)
        ]
        assert file_handlers, "Logger must have at least one FileHandler"

        # Every FileHandler attached by setup_logging() must use write mode.
        for fh in file_handlers:
            assert fh.mode == 'w', (
                f"FileHandler mode is '{fh.mode}', expected 'w'. "
                "Each scan should overwrite the previous surprises.log.")

    def test_console_handler_at_info(self):
        """Console should only show INFO and above."""
        handlers = [
            h for h in logger.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
        ]
        assert any(h.level == logging.INFO for h in handlers)

    def test_file_handler_at_debug(self):
        """File handler should capture DEBUG and above."""
        file_handlers = [
            h for h in logger.handlers
            if isinstance(h, logging.FileHandler)
        ]
        assert any(h.level == logging.DEBUG for h in file_handlers)

    def test_rev4_no_duplicate_handlers(self, tmp_path):
        """
        Rev 4: calling setup_logging() a second time (as happens when
        fits_migrator imports fits_metadata_extractor) must NOT add a
        second pair of handlers to the shared logger.
        """
        log_file = str(tmp_path / 'dup_test.log')
        count_before = len(logger.handlers)

        # Call setup_logging a second time with the same logger name
        setup_logging(log_file=log_file)
        count_after = len(logger.handlers)

        # Handler count must not have grown (guard must have suppressed duplicates)
        assert count_after == count_before, (
            f"setup_logging() added {count_after - count_before} duplicate "
            f"handler(s) on a second call. Expected 0 additions "
            f"(had {count_before}, now {count_after}).")


# ──────────────────────────────────────────────────────────────────────────────
# Configuration integrity
# ──────────────────────────────────────────────────────────────────────────────

class TestConfiguration:

    def test_all_known_devices_present(self):
        assert {'DWARF_3', 'DWARF_MINI', 'SEESTAR_S50'}.issubset(DEVICE_SIGNATURES)

    def test_each_device_has_detection_method(self):
        detection_keys = {'TELESCOPE', 'CREATOR', 'INSTRUMENT', 'image_size', 'path_names'}
        for name, sigs in DEVICE_SIGNATURES.items():
            assert detection_keys & set(sigs), f"{name} has no detection criteria"

    def test_each_device_has_path_names(self):
        """Every device must have path_names for calibration-frame detection."""
        for name, sigs in DEVICE_SIGNATURES.items():
            assert 'path_names' in sigs, f"{name} missing 'path_names'"
            assert len(sigs['path_names']) > 0

    def test_path_names_are_lowercase(self):
        """Path matching is case-insensitive via .lower() — names must be pre-lowered."""
        for name, sigs in DEVICE_SIGNATURES.items():
            for pn in sigs.get('path_names', []):
                assert pn == pn.lower(), \
                    f"{name} path_name '{pn}' must be lowercase"

    def test_fits_extensions_complete(self):
        for ext in ('.fits', '.fit', '.FITS', '.FIT'):
            assert ext in FITS_EXTENSIONS

    def test_dwarf3_both_firmware_names(self):
        sigs = DEVICE_SIGNATURES['DWARF_3']['TELESCOPE']
        assert 'DWARFIII' in sigs and 'DWARF 3' in sigs

    def test_dwarf_mini_lowercase_m_in_config(self):
        sigs = DEVICE_SIGNATURES['DWARF_MINI']['TELESCOPE']
        assert 'DWARF mini' in sigs
        assert 'DWARF Mini' not in sigs

    def test_frame_filename_prefixes_all_lowercase(self):
        """Prefix keys must be lowercase since we match against filename.lower()."""
        for prefix in FRAME_FILENAME_PREFIXES:
            assert prefix == prefix.lower(), \
                f"Prefix '{prefix}' must be lowercase"

    def test_rev3_prefixes_present(self):
        """Rev 3 additions must be in FRAME_FILENAME_PREFIXES."""
        assert 'unknown_' in FRAME_FILENAME_PREFIXES
        assert 'failed_'  in FRAME_FILENAME_PREFIXES
        assert 'stacked-' in FRAME_FILENAME_PREFIXES

    def test_rev4_stacked_underscore_prefix_present(self):
        """Rev 4: Seestar 'stacked_' prefix must be registered."""
        assert 'stacked_' in FRAME_FILENAME_PREFIXES, (
            "'stacked_' must be in FRAME_FILENAME_PREFIXES so that Seestar "
            "Stacked_N_… files are recognised as LIGHT frames")

    def test_unknown_and_failed_map_to_unknown_type(self):
        assert FRAME_FILENAME_PREFIXES['unknown_'] == 'UNKNOWN'
        assert FRAME_FILENAME_PREFIXES['failed_']  == 'UNKNOWN'

    def test_stacked_maps_to_light(self):
        assert FRAME_FILENAME_PREFIXES['stacked-'] == 'LIGHT'

    def test_stacked_underscore_maps_to_light(self):
        """Rev 4: stacked_ (Seestar) must also map to LIGHT."""
        assert FRAME_FILENAME_PREFIXES['stacked_'] == 'LIGHT'
