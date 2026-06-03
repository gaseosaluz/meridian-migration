#!/usr/bin/env python3
"""
test_fits_metadata.py

Unit tests for fits_metadata_extractor.py

Test fixtures use REAL header values from Ed's actual FITS files:
  - DWARF 3 light frame  (Aug 2025, old firmware TELESCOP='DWARFIII')
  - DWARF 3 light frame  (Mar 2026, new firmware TELESCOP='DWARF 3')
  - DWARF 3 dark frame   (Jan 2026, factory calibration)
  - DWARF Mini light     (Mar 2026, TELESCOP='DWARF mini')
  - Seestar S50 light    (Sep 2025, CREATOR='ZWO Seestar S50')

Run with:
    pytest test_fits_metadata.py -v
    pytest test_fits_metadata.py -v --tb=short   # shorter tracebacks
"""

import pytest
from astropy.io import fits

from fits_metadata_extractor import (
    detect_device,
    classify_frame,
    get_temperature,
    get_exposure,
    parse_observation_date,
    DEVICE_SIGNATURES,
    FITS_EXTENSIONS,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def make_header(kvs: dict) -> fits.Header:
    """Build a minimal FITS header from a plain dict."""
    h = fits.Header()
    for k, v in kvs.items():
        h[k] = v
    return h


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures — real header values, verified from exiftool output
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def dwarf3_light_2025():
    """C 11 (Veil Nebula) — DWARF 3, old firmware, August 2025."""
    return make_header({
        'TELESCOP': 'DWARFIII',
        'INSTRUME': 'DWARFIII',
        'ORIGIN':   'DWARFLAB',
        'OBJECT':   'C 11',
        'EXPTIME':  30.0,
        'GAIN':     60,
        'FILTER':   'Duo-Band',
        'DET-TEMP': 40,
        'RA':       350.2199,
        'DEC':      61.28683,
        'NAXIS1':   3856,
        'NAXIS2':   2180,
        'BITPIX':   16,
        'BAYERPAT': 'RGGB',
        'FOCALLEN': 150.0,
        'XPIXSZ':   2.0,
        'YPIXSZ':   2.0,
        'DATE-OBS': '2025-08-19T02:56:12.413',
    })


@pytest.fixture
def dwarf3_light_2026():
    """Barnard 33 (Horsehead) — DWARF 3, firmware 1.4.15.2, March 2026."""
    return make_header({
        'TELESCOP': 'DWARF 3',
        'INSTRUME': 'DWARF 3',
        'ORIGIN':   'DWARFLAB',
        'OBJECT':   'Barnard 33',
        'EXPTIME':  30.0,
        'GAIN':     60,
        'FILTER':   'Duo-Band',
        'DET-TEMP': 36,
        'RA':       85.24583,
        'DEC':      -2.45833,
        'NAXIS1':   3856,
        'NAXIS2':   2180,
        'BITPIX':   16,
        'BAYERPAT': 'RGGB',
        'FOCALLEN': 150.0,
        'FIRMWARE': '1.4.15.2',
        'MACADDR':  '5478C93AC834',
        'EQMODE':   1,
        'DATE-OBS': '2026-03-24T20:37:32.172',
    })


@pytest.fixture
def dwarf3_dark():
    """Factory dark frame — DWARF 3, January 2026, EXPTIME=10s."""
    return make_header({
        'TELESCOP': 'DWARF 3',
        'INSTRUME': 'DWARF 3',
        'ORIGIN':   'DWARFLAB',
        'OBJECT':   '',
        'EXPTIME':  10.0,
        'GAIN':     60,
        'FILTER':   '',
        'DET-TEMP': 35,
        'RA':       0.0,
        'DEC':      0.0,
        'NAXIS1':   3856,
        'NAXIS2':   2180,
        'BITPIX':   16,
        'BAYERPAT': 'RGGB',
        'FOCALLEN': 150.0,
        'DATE-OBS': '2026-01-02T18:49:51.912',
    })


@pytest.fixture
def dwarf_mini_light():
    """Barnard 33 — DWARF Mini, firmware 1.0.25.2, March 2026."""
    return make_header({
        'TELESCOP': 'DWARF mini',
        'INSTRUME': 'DWARF mini',
        'ORIGIN':   'DWARFLAB',
        'OBJECT':   'Barnard 33',
        'EXPTIME':  15.0,
        'GAIN':     80,
        'FILTER':   'Duo-Band',
        'DET-TEMP': 31,
        'RA':       85.24583,
        'DEC':      -2.45833,
        'NAXIS1':   1920,
        'NAXIS2':   1080,
        'BITPIX':   16,
        'BAYERPAT': 'RGGB',
        'FOCALLEN': 150.0,
        'FIRMWARE': '1.0.25.2',
        'MACADDR':  '40D95ADC0CEE',
        'EQMODE':   0,
        'DATE-OBS': '2026-03-20T20:45:11.261',
    })


@pytest.fixture
def seestar_s50_light():
    """NGC 281 (Pacman Nebula) — Seestar S50, September 2025."""
    return make_header({
        'TELESCOP': 'S50_40d24ed8',      # serial embedded in TELESCOP
        'INSTRUME': 'Seestar S50',
        'CREATOR':  'ZWO Seestar S50',
        'PRODUCER': 'ZWO',
        'OBJECT':   'NGC 281',
        'IMAGETYP': 'Light',
        'EXPTIME':  10.0,
        'EXPOSURE': 10.0,
        'GAIN':     80,
        'FILTER':   'LP',
        'CCD-TEMP': 27.9375,
        'RA':       13.633335,
        'DEC':      56.775833,
        'NAXIS1':   1080,
        'NAXIS2':   1920,
        'BITPIX':   16,
        'BAYERPAT': 'GRBG',
        'FOCALLEN': 250.0,
        'XPIXSZ':   2.9,
        'YPIXSZ':   2.9,
        'SITELAT':  30.5038,
        'SITELONG': -97.7429,
        'DATE-OBS': '2025-09-29T06:01:02.382641',
        'PROGRAM':  '4.70',
        'EQMODE':   0,
    })


# ──────────────────────────────────────────────────────────────────────────────
# Device Detection
# ──────────────────────────────────────────────────────────────────────────────

class TestDeviceDetection:

    def test_dwarf3_old_firmware_dwarfiii(self, dwarf3_light_2025):
        """Pre-2026 DWARF 3 used TELESCOP='DWARFIII'."""
        device, warnings = detect_device(dwarf3_light_2025)
        assert device == 'DWARF_3'
        assert warnings == []

    def test_dwarf3_new_firmware_dwarf3(self, dwarf3_light_2026):
        """2026+ DWARF 3 uses TELESCOP='DWARF 3'."""
        device, warnings = detect_device(dwarf3_light_2026)
        assert device == 'DWARF_3'
        assert warnings == []

    def test_dwarf3_dark_frame(self, dwarf3_dark):
        device, _ = detect_device(dwarf3_dark)
        assert device == 'DWARF_3'

    def test_dwarf_mini_lowercase_m(self, dwarf_mini_light):
        """DWARF mini uses lowercase 'm' — case matters."""
        device, warnings = detect_device(dwarf_mini_light)
        assert device == 'DWARF_MINI'
        assert warnings == []

    def test_dwarf_mini_not_confused_with_dwarf3(self, dwarf_mini_light):
        """DWARF Mini must not be detected as DWARF_3 despite same focal length."""
        device, _ = detect_device(dwarf_mini_light)
        assert device != 'DWARF_3'

    def test_seestar_s50_via_creator(self, seestar_s50_light):
        """Seestar detected via CREATOR field."""
        device, warnings = detect_device(seestar_s50_light)
        assert device == 'SEESTAR_S50'
        assert warnings == []

    def test_seestar_telescop_has_serial(self, seestar_s50_light):
        """Confirm fixture: Seestar TELESCOP embeds serial, not plain model name."""
        assert seestar_s50_light['TELESCOP'].startswith('S50_')

    def test_seestar_detected_despite_serial_in_telescop(self, seestar_s50_light):
        """Detection must succeed even with serial embedded in TELESCOP."""
        device, _ = detect_device(seestar_s50_light)
        assert device == 'SEESTAR_S50'

    def test_unknown_device_returns_unknown_with_warning(self):
        """A device we've never seen must return UNKNOWN with a helpful warning."""
        header = make_header({
            'TELESCOP': 'FutureScopeX 9000',
            'NAXIS1': 9999,
            'NAXIS2': 9999,
        })
        device, warnings = detect_device(header)
        assert device == 'UNKNOWN'
        assert len(warnings) == 1
        assert 'UNKNOWN' in warnings[0]

    def test_fallback_detection_dwarf3_by_dimensions(self):
        """If firmware renames TELESCOP but dimensions match, detect with warning."""
        header = make_header({
            'TELESCOP': 'DWARF-III-V2',   # hypothetical future rename
            'NAXIS1': 3856,
            'NAXIS2': 2180,
        })
        device, warnings = detect_device(header)
        assert device == 'DWARF_3'
        assert len(warnings) == 1                        # warning issued
        assert 'DEVICE_SIGNATURES' in warnings[0]        # tells user what to update

    def test_fallback_detection_dwarf_mini_by_dimensions(self):
        header = make_header({'TELESCOP': '', 'NAXIS1': 1920, 'NAXIS2': 1080})
        device, warnings = detect_device(header)
        assert device == 'DWARF_MINI'
        assert len(warnings) == 1

    def test_fallback_detection_seestar_by_dimensions(self):
        header = make_header({'TELESCOP': '', 'NAXIS1': 1080, 'NAXIS2': 1920})
        device, warnings = detect_device(header)
        assert device == 'SEESTAR_S50'


# ──────────────────────────────────────────────────────────────────────────────
# Frame Classification
# ──────────────────────────────────────────────────────────────────────────────

class TestFrameClassification:

    # ── Seestar: IMAGETYP field ────────────────────────────────────────────────

    def test_seestar_light_via_imagetyp(self, seestar_s50_light):
        frame, warnings = classify_frame(seestar_s50_light)
        assert frame == 'LIGHT'
        assert warnings == []

    def test_imagetyp_dark(self):
        h = make_header({'IMAGETYP': 'Dark', 'OBJECT': '', 'RA': 0.0, 'DEC': 0.0})
        frame, _ = classify_frame(h)
        assert frame == 'DARK'

    def test_imagetyp_flat(self):
        h = make_header({'IMAGETYP': 'Flat', 'OBJECT': '', 'RA': 0.0, 'DEC': 0.0})
        frame, _ = classify_frame(h)
        assert frame == 'FLAT'

    def test_imagetyp_flat_field_normalised(self):
        """'Flat Field' (two words) should normalise to FLAT."""
        h = make_header({'IMAGETYP': 'Flat Field'})
        frame, _ = classify_frame(h)
        assert frame == 'FLAT'

    def test_imagetyp_bias(self):
        h = make_header({'IMAGETYP': 'Bias'})
        frame, _ = classify_frame(h)
        assert frame == 'BIAS'

    def test_imagetyp_offset_normalised_to_bias(self):
        h = make_header({'IMAGETYP': 'Offset'})
        frame, _ = classify_frame(h)
        assert frame == 'BIAS'

    def test_imagetyp_case_insensitive(self):
        h = make_header({'IMAGETYP': 'LIGHT'})
        frame, _ = classify_frame(h)
        assert frame == 'LIGHT'

    def test_unrecognised_imagetyp_logs_warning_and_falls_through(self):
        """Unknown IMAGETYP should warn but still try to classify by other fields."""
        h = make_header({'IMAGETYP': 'Experimental', 'OBJECT': 'M42', 'RA': 10.0, 'DEC': 5.0})
        frame, warnings = classify_frame(h)
        assert len(warnings) == 1
        assert 'Unrecognised IMAGETYP' in warnings[0]
        assert frame == 'LIGHT'   # fell through to OBJECT-based classification

    # ── DWARF: no IMAGETYP, infer from OBJECT + RA/DEC ───────────────────────

    def test_dwarf3_light_by_object(self, dwarf3_light_2025):
        frame, warnings = classify_frame(dwarf3_light_2025)
        assert frame == 'LIGHT'
        assert warnings == []

    def test_dwarf3_2026_light(self, dwarf3_light_2026):
        frame, _ = classify_frame(dwarf3_light_2026)
        assert frame == 'LIGHT'

    def test_dwarf_mini_light(self, dwarf_mini_light):
        frame, _ = classify_frame(dwarf_mini_light)
        assert frame == 'LIGHT'

    def test_dwarf3_dark_empty_object_and_zero_coords(self, dwarf3_dark):
        """DWARF dark: OBJECT empty, RA=0, DEC=0."""
        frame, warnings = classify_frame(dwarf3_dark)
        assert frame == 'DARK'
        assert warnings == []

    def test_cannot_classify_returns_unknown_with_warning(self):
        """Ambiguous headers should return UNKNOWN with a logged warning."""
        h = make_header({'RA': 45.0, 'DEC': 30.0})  # no OBJECT, non-zero coords
        frame, warnings = classify_frame(h)
        assert frame == 'UNKNOWN'
        assert len(warnings) == 1


# ──────────────────────────────────────────────────────────────────────────────
# Temperature Extraction
# ──────────────────────────────────────────────────────────────────────────────

class TestTemperatureExtraction:

    def test_dwarf3_det_temp(self, dwarf3_light_2025):
        assert get_temperature(dwarf3_light_2025) == 40.0

    def test_dwarf3_2026_det_temp(self, dwarf3_light_2026):
        assert get_temperature(dwarf3_light_2026) == 36.0

    def test_dwarf3_dark_temp(self, dwarf3_dark):
        assert get_temperature(dwarf3_dark) == 35.0

    def test_dwarf_mini_det_temp(self, dwarf_mini_light):
        assert get_temperature(dwarf_mini_light) == 31.0

    def test_seestar_ccd_temp(self, seestar_s50_light):
        assert get_temperature(seestar_s50_light) == pytest.approx(27.9375, rel=1e-4)

    def test_no_temp_field_returns_none(self):
        h = make_header({'OBJECT': 'M42'})
        assert get_temperature(h) is None

    def test_texas_summer_heat_captured(self, dwarf3_light_2025):
        """40°C sensor temp on a Texas August night - confirmed."""
        temp = get_temperature(dwarf3_light_2025)
        assert temp == 40.0
        assert temp > 35, "Should detect hot Texas summer conditions"


# ──────────────────────────────────────────────────────────────────────────────
# Exposure Extraction
# ──────────────────────────────────────────────────────────────────────────────

class TestExposureExtraction:

    def test_dwarf3_exptime_30s(self, dwarf3_light_2025):
        assert get_exposure(dwarf3_light_2025) == 30.0

    def test_dwarf3_dark_exptime_10s(self, dwarf3_dark):
        """Dark frames can have different exposure than lights — must read correctly."""
        assert get_exposure(dwarf3_dark) == 10.0

    def test_dwarf_mini_exptime_15s(self, dwarf_mini_light):
        assert get_exposure(dwarf_mini_light) == 15.0

    def test_seestar_has_both_exposure_and_exptime(self, seestar_s50_light):
        """Seestar provides both fields - EXPTIME takes priority."""
        assert seestar_s50_light.get('EXPTIME')  is not None
        assert seestar_s50_light.get('EXPOSURE') is not None
        assert get_exposure(seestar_s50_light) == 10.0

    def test_no_exposure_field_returns_none(self):
        h = make_header({'OBJECT': 'M42'})
        assert get_exposure(h) is None

    def test_dark_exposure_differs_from_light(self, dwarf3_light_2025, dwarf3_dark):
        """
        This is the exposure-mismatch case we discovered:
        lights are 30s but factory dark is 10s.
        Both values must be read correctly for dark-matching logic.
        """
        light_exp = get_exposure(dwarf3_light_2025)
        dark_exp  = get_exposure(dwarf3_dark)
        assert light_exp == 30.0
        assert dark_exp  == 10.0
        assert light_exp != dark_exp   # confirms mismatch detection


# ──────────────────────────────────────────────────────────────────────────────
# Date Parsing
# ──────────────────────────────────────────────────────────────────────────────

class TestDateParsing:

    def test_dwarf3_2025_date(self, dwarf3_light_2025):
        _, year, month = parse_observation_date(dwarf3_light_2025)
        assert year  == 2025
        assert month == 8

    def test_dwarf3_2026_date(self, dwarf3_light_2026):
        _, year, month = parse_observation_date(dwarf3_light_2026)
        assert year  == 2026
        assert month == 3

    def test_dwarf3_dark_date(self, dwarf3_dark):
        _, year, month = parse_observation_date(dwarf3_dark)
        assert year  == 2026
        assert month == 1

    def test_dwarf_mini_date(self, dwarf_mini_light):
        _, year, month = parse_observation_date(dwarf_mini_light)
        assert year  == 2026
        assert month == 3

    def test_seestar_date_with_microseconds(self, seestar_s50_light):
        """Seestar uses full ISO 8601 with microseconds."""
        date_str, year, month = parse_observation_date(seestar_s50_light)
        assert year  == 2025
        assert month == 9
        assert '.' in date_str   # microseconds present

    def test_missing_date_returns_triple_none(self):
        h = make_header({'OBJECT': 'M42'})
        date_str, year, month = parse_observation_date(h)
        assert date_str is None
        assert year     is None
        assert month    is None

    def test_date_string_preserved(self, dwarf3_light_2025):
        date_str, _, _ = parse_observation_date(dwarf3_light_2025)
        assert date_str == '2025-08-19T02:56:12.413'


# ──────────────────────────────────────────────────────────────────────────────
# Configuration integrity
# ──────────────────────────────────────────────────────────────────────────────

class TestConfiguration:

    def test_all_known_devices_present(self):
        expected = {'DWARF_3', 'DWARF_MINI', 'SEESTAR_S50'}
        assert expected.issubset(set(DEVICE_SIGNATURES.keys()))

    def test_each_device_has_at_least_one_detection_method(self):
        detection_keys = {'TELESCOPE', 'CREATOR', 'INSTRUMENT', 'image_size'}
        for device_name, sigs in DEVICE_SIGNATURES.items():
            assert detection_keys & set(sigs.keys()), \
                f"Device '{device_name}' has no detection criteria"

    def test_fits_extensions_complete(self):
        for ext in ('.fits', '.fit', '.FITS', '.FIT'):
            assert ext in FITS_EXTENSIONS, f"Missing extension: {ext}"

    def test_dwarf3_handles_both_firmware_names(self):
        """Both 'DWARFIII' and 'DWARF 3' must be in DWARF_3 signatures."""
        sigs = DEVICE_SIGNATURES['DWARF_3']['TELESCOPE']
        assert 'DWARFIII' in sigs, "Missing old firmware name 'DWARFIII'"
        assert 'DWARF 3'  in sigs, "Missing new firmware name 'DWARF 3'"

    def test_dwarf_mini_uses_lowercase_m(self):
        """Case matters - firmware uses lowercase 'm'."""
        sigs = DEVICE_SIGNATURES['DWARF_MINI']['TELESCOPE']
        assert 'DWARF mini' in sigs
        assert 'DWARF Mini' not in sigs, "Wrong capitalisation would miss real files"
