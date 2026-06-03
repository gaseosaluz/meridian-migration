#!/usr/bin/env python3
"""
test_fits_metadata.py

Unit tests for fits_metadata_extractor.py

Fixtures use REAL header values from Ed's actual FITS files.
Rev 2 adds tests for:
  - Directory-path-based device detection  (calibration frames)
  - Filename-based frame classification    (calibration frames)
  - Calibration filename metadata parsing  (dark/bias/flat)

Run with:
    pytest test_fits_metadata.py -v
"""

import pytest
from pathlib import Path
from astropy.io import fits

from fits_metadata_extractor import (
    detect_device,
    detect_device_from_path,
    classify_frame,
    classify_from_filename,
    parse_calibration_filename,
    get_temperature,
    get_exposure,
    parse_observation_date,
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


def make_path(*parts: str) -> Path:
    """Build a Path from string parts — avoids hard-coding OS separators."""
    return Path(*parts)


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
# Device Detection — header-based (unchanged from Rev 1)
# ──────────────────────────────────────────────────────────────────────────────

class TestDeviceDetectionHeader:

    def test_dwarf3_old_firmware(self, dwarf3_light_2025):
        device, warnings = detect_device(dwarf3_light_2025)
        assert device == 'DWARF_3' and warnings == []

    def test_dwarf3_new_firmware(self, dwarf3_light_2026):
        device, warnings = detect_device(dwarf3_light_2026)
        assert device == 'DWARF_3' and warnings == []

    def test_dwarf_mini_lowercase_m(self, dwarf_mini_light):
        device, warnings = detect_device(dwarf_mini_light)
        assert device == 'DWARF_MINI' and warnings == []

    def test_dwarf_mini_not_confused_with_dwarf3(self, dwarf_mini_light):
        device, _ = detect_device(dwarf_mini_light)
        assert device != 'DWARF_3'

    def test_seestar_s50_via_creator(self, seestar_s50_light):
        device, warnings = detect_device(seestar_s50_light)
        assert device == 'SEESTAR_S50' and warnings == []

    def test_seestar_serial_in_telescop_still_detected(self, seestar_s50_light):
        assert seestar_s50_light['TELESCOP'].startswith('S50_')
        device, _ = detect_device(seestar_s50_light)
        assert device == 'SEESTAR_S50'

    def test_unknown_device_warns(self):
        h = make_header({'TELESCOP': 'FutureScopeX 9000', 'NAXIS1': 9999, 'NAXIS2': 9999})
        device, warnings = detect_device(h)
        assert device == 'UNKNOWN' and len(warnings) == 1

    def test_dimension_fallback_warns(self):
        h = make_header({'TELESCOP': 'DWARF-III-V2', 'NAXIS1': 3856, 'NAXIS2': 2180})
        device, warnings = detect_device(h)
        assert device == 'DWARF_3' and len(warnings) == 1
        assert 'DEVICE_SIGNATURES' in warnings[0]


# ──────────────────────────────────────────────────────────────────────────────
# Device Detection — path-based (NEW in Rev 2)
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
        p = Path('/Users/edm/Astronomy/Astrocapture/2025/September/'
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
        device, warnings = detect_device(dwarf3_calib_stripped, p)
        assert device == 'DWARF_3'
        # Path-based detection must NOT produce a firmware-rename warning
        assert not any('firmware rename' in w for w in warnings)

    def test_mini_calib_no_false_firmware_warning(self, dwarf_mini_calib_stripped):
        p = Path('/Users/edm/Astronomy/Astrocapture/2026/DWARF Mini/'
                 'March 2026/bias_gain_2_bin_1.fits')
        device, warnings = detect_device(dwarf_mini_calib_stripped, p)
        assert device == 'DWARF_MINI'
        assert not any('firmware rename' in w for w in warnings)

    def test_path_beats_dimension_fallback(self, dwarf3_calib_stripped):
        """Path detection (silent) must take priority over dimension fallback (noisy)."""
        p = Path('/some/path/DWARF 3/calibration/dark.fits')
        _, warnings = detect_device(dwarf3_calib_stripped, p)
        # dimension fallback would warn; path should prevent that
        assert not any('firmware rename' in w for w in warnings)


# ──────────────────────────────────────────────────────────────────────────────
# Frame Classification — header-based (unchanged from Rev 1)
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
# Frame Classification — filename-based (NEW in Rev 2)
# ──────────────────────────────────────────────────────────────────────────────

class TestClassifyFromFilename:

    @pytest.mark.parametrize("filename,expected", [
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

    def test_truly_unknown_still_warns(self, dwarf3_calib_stripped):
        """No header info AND no recognisable filename → UNKNOWN with warning."""
        frame, warnings = classify_frame(
            dwarf3_calib_stripped,
            filename='mystery_file.fits')
        assert frame == 'UNKNOWN'
        assert len(warnings) == 1


# ──────────────────────────────────────────────────────────────────────────────
# Calibration Filename Metadata Parsing (NEW in Rev 2)
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
# Temperature Extraction (unchanged from Rev 1)
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
# Exposure Extraction (unchanged from Rev 1)
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
# Date Parsing (unchanged from Rev 1)
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
