#!/usr/bin/env python3
"""
fits_metadata_extractor.py

Astrophotography FITS Metadata Extractor
Phase 1: Header Reading, Device Detection, Frame Classification

Built for Ed's Astrophotography Migration Project
Sprint 2, Phase 1 - Rev 3: log hygiene + extended classification

Validated against actual files from:
  - DWARF 3 (firmware pre-1.4  / TELESCOP='DWARFIII')
  - DWARF 3 (firmware 1.4.15.2 / TELESCOP='DWARF 3')
  - DWARF Mini (firmware 1.0.25.2 / TELESCOP='DWARF mini')
  - Seestar S50 (CREATOR='ZWO Seestar S50')

Rev 3 changes:
  - Log file now opened in WRITE mode ('w') so each run produces a fresh
    surprises.log.  Append mode caused old entries to accumulate across runs,
    making it hard to tell which scan produced which output.
  - Three new filename prefixes recognised (no WARNING, just silent DEBUG):
      unknown_  → UNKNOWN  (device-internal unclassified frames)
      failed_   → UNKNOWN  (capture-failure frames recorded by the device)
      stacked-  → LIGHT    (device-produced stacks, e.g. stacked-16_…)
  - WCS / astrometric headers no longer flagged as unknown:
      Standard : WCSAXES, CTYPE*, CRVAL*, CRPIX*, CDELT*, CUNIT*, PC*_*,
                 CD*_*, PV*_*, PS*_*, LONPOLE, LATPOLE, RADESYS, EQUINOX,
                 A_*, B_*, AP_*, BP_*, SIP coefficients, etc.
      Custom   : CRTL*, CRTR*, CRBL*, CRBR* (corner-coordinate headers
                 written by Seestar firmware, variable suffix)
  - detect_device() now returns a 3-tuple (device, warnings, source) so the
    extraction layer can record exactly how the device was identified.
  - print_summary() reports total WARNING count.
"""

import re
import sys
import json
import logging
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional
from astropy.io import fits

# ──────────────────────────────────────────────────────────────────────────────
# DEVICE CONFIGURATION
#
# When a firmware update changes a header value, update this section ONLY.
# No other code changes needed — detection logic reads from here at runtime.
# ──────────────────────────────────────────────────────────────────────────────

DEVICE_SIGNATURES: dict = {
    "DWARF_3": {
        # 'DWARFIII' = pre-firmware 1.4 (2025)
        # 'DWARF 3'  = firmware 1.4+ (2026 onwards)
        "TELESCOPE":  ["DWARFIII", "DWARF 3"],
        "INSTRUMENT": ["DWARFIII", "DWARF 3"],
        "ORIGIN":     ["DWARFLAB"],
        "image_size": (3856, 2180),
        # Directory names used in Ed's file system (lowercase for matching)
        "path_names": ["dwarf 3", "dwarf_3", "dwarfiii", "dwarf3"],
    },
    "DWARF_MINI": {
        "TELESCOPE":  ["DWARF mini"],           # lowercase 'm' — exact match required
        "INSTRUMENT": ["DWARF mini"],
        "ORIGIN":     ["DWARFLAB"],
        "image_size": (1920, 1080),
        "path_names": ["dwarf mini", "dwarf_mini", "dwarfmini"],
    },
    "SEESTAR_S50": {
        # TELESCOP embeds serial: 'S50_40d24ed8' — handled separately below
        "CREATOR":    ["ZWO Seestar S50"],
        "INSTRUMENT": ["Seestar S50"],
        "image_size": (1080, 1920),             # portrait orientation
        "path_names": ["seestar s50", "seestar_s50", "seestar50", "s50"],
    },
    # ── Add future devices here without touching any other code ──────────────
    # "SEESTAR_S30_PRO": {
    #     "CREATOR":    ["ZWO Seestar S30 Pro"],
    #     "INSTRUMENT": ["Seestar S30 Pro"],
    #     "path_names": ["seestar s30 pro", "seestar_s30_pro", "s30pro"],
    # },
    # "ASIAIR": {
    #     "CREATOR": ["ASIAIR"],
    #     "path_names": ["asiair"],
    # },
}

# All FITS file extensions we will process
FITS_EXTENSIONS: frozenset = frozenset({'.fits', '.fit', '.FITS', '.FIT'})

# Filename prefixes that unambiguously identify frame types.
# Key = lowercase prefix, Value = frame type string.
# Longer prefixes must come before shorter ones if they share a stem.
# ── Rev 3: added unknown_ / failed_ / stacked- ───────────────────────────────
FRAME_FILENAME_PREFIXES: dict = {
    'bias_':    'BIAS',
    'dark_':    'DARK',
    'flat_':    'FLAT',
    'raw_':     'DARK',     # DWARF individual dark sub-frames
    'light_':   'LIGHT',    # Seestar light sub-frames
    # Rev 3 additions — no WARNING produced; classified silently
    'unknown_': 'UNKNOWN',  # Device-internal unclassified frames
    'failed_':  'UNKNOWN',  # Capture-failure frames recorded by device
    'stacked-': 'LIGHT',    # Device-stacked output, e.g. stacked-16_…
}

# ──────────────────────────────────────────────────────────────────────────────
# Calibration filename patterns  (DWARF produces metadata-rich filenames
# even when FITS headers are stripped down)
# ──────────────────────────────────────────────────────────────────────────────

# dark_exp_30.000000_gain_60_bin_1_34C_stack_1.fits
_RE_DARK_STACK = re.compile(
    r'dark_exp_([\d.]+)_gain_(\d+)_bin_\d+_(-?\d+)C_stack_\d+', re.IGNORECASE)

# raw_10s_60_0000_20260102-184951912_35C.fits  (individual dark subs)
_RE_DARK_RAW = re.compile(
    r'raw_([\d.]+)s_(\d+)_\d+_[\d-]+_(-?\d+)C', re.IGNORECASE)

# bias_gain_2_bin_1.fits
_RE_BIAS = re.compile(r'bias_gain_(\d+)_bin_\d+', re.IGNORECASE)

# flat_gain_2_bin_1_ir_0.fits  or  flat_gain_2_bin_1.fits
_RE_FLAT = re.compile(r'flat_gain_(\d+)_bin_\d+', re.IGNORECASE)


# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

def setup_logging(log_file: Optional[str] = 'surprises.log') -> logging.Logger:
    """
    Two-channel logging:
      Console : INFO and above  (normal progress)
      File    : DEBUG and above (full detail including path-based detections)

    surprises.log is your early-warning system for firmware changes.
    Rev 3: opened in WRITE mode ('w') so each scan produces a fresh log.
    Delete surprises.log manually only if you want to compare runs back-to-back.
    """
    logger = logging.getLogger('fits_extractor')
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        '%(asctime)s [%(levelname)-8s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S')

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    if log_file:
        fh = logging.FileHandler(log_file, mode='w')   # Rev 3: 'a' → 'w'
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


logger = setup_logging()


# ──────────────────────────────────────────────────────────────────────────────
# Data Model
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class FITSMetadata:
    """All metadata extracted from a single FITS file."""

    # ── File ──────────────────────────────────────────────────────────────────
    file_path:      str = ""
    file_name:      str = ""
    file_extension: str = ""

    # ── Device ────────────────────────────────────────────────────────────────
    device_type:       str = "UNKNOWN"
    device_id:         str = ""
    device_source:     str = ""     # 'header' | 'path' | 'dimensions' | 'unknown'
    telescope:         str = ""
    instrument:        str = ""
    origin:            str = ""
    firmware:          str = ""
    mac_address:       str = ""

    # ── Frame ─────────────────────────────────────────────────────────────────
    frame_type:        str = "UNKNOWN"  # LIGHT | DARK | FLAT | BIAS | UNKNOWN
    frame_type_source: str = ""         # 'imagetyp' | 'object' | 'ra_dec' | 'filename' | 'unknown'

    # ── Target ────────────────────────────────────────────────────────────────
    object_name: str             = ""
    ra:          Optional[float] = None
    dec:         Optional[float] = None

    # ── Capture parameters ────────────────────────────────────────────────────
    exposure_time: Optional[float] = None
    gain:          Optional[int]   = None
    filter_name:   str             = ""
    temperature:   Optional[float] = None

    # ── Image properties ──────────────────────────────────────────────────────
    image_width:   Optional[int]   = None
    image_height:  Optional[int]   = None
    bit_depth:     Optional[int]   = None
    bayer_pattern: str             = ""
    focal_length:  Optional[float] = None
    pixel_size_x:  Optional[float] = None
    pixel_size_y:  Optional[float] = None

    # ── Date / Time ───────────────────────────────────────────────────────────
    observation_date:  Optional[str] = None
    observation_year:  Optional[int] = None
    observation_month: Optional[int] = None

    # ── Location (Seestar provides this) ──────────────────────────────────────
    site_latitude:  Optional[float] = None
    site_longitude: Optional[float] = None

    # ── Quality / diagnostics ─────────────────────────────────────────────────
    is_valid:        bool = True
    warnings:        list = field(default_factory=list)
    unknown_headers: dict = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────────
# Calibration filename parsing
# ──────────────────────────────────────────────────────────────────────────────

def parse_calibration_filename(filename: str) -> dict:
    """
    Extract exposure, gain, and temperature from DWARF calibration filenames.
    Calibration frames often have minimal FITS headers but information-rich names.

    Handles:
      dark_exp_30.000000_gain_60_bin_1_34C_stack_1.fits  → exp=30, gain=60, temp=34
      raw_10s_60_0000_20260102-184951912_35C.fits         → exp=10, gain=60, temp=35
      bias_gain_2_bin_1.fits                              → gain=2
      flat_gain_2_bin_1_ir_0.fits                         → gain=2

    Returns a (possibly partial) dict with keys: exposure_time, gain, temperature.
    Empty dict if no pattern matched.
    """
    stem = Path(filename).stem
    result: dict = {}

    m = _RE_DARK_STACK.search(stem)
    if m:
        result['exposure_time'] = float(m.group(1))
        result['gain']          = int(m.group(2))
        result['temperature']   = float(m.group(3))
        return result

    m = _RE_DARK_RAW.search(stem)
    if m:
        result['exposure_time'] = float(m.group(1))
        result['gain']          = int(m.group(2))
        result['temperature']   = float(m.group(3))
        return result

    m = _RE_BIAS.search(stem)
    if m:
        result['gain'] = int(m.group(1))
        return result

    m = _RE_FLAT.search(stem)
    if m:
        result['gain'] = int(m.group(1))
        return result

    return result


def classify_from_filename(filename: str) -> Optional[str]:
    """
    Classify frame type from filename prefix alone.
    Used as a fallback when FITS headers carry no IMAGETYP and OBJECT is empty.

    Returns frame type string or None if no prefix matched.
    """
    lower = Path(filename).name.lower()
    for prefix, frame_type in FRAME_FILENAME_PREFIXES.items():
        if lower.startswith(prefix):
            return frame_type
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Device detection
# ──────────────────────────────────────────────────────────────────────────────

def detect_device_from_path(file_path: Path) -> Optional[str]:
    """
    Identify device from the directory path when FITS headers are absent.

    DWARF calibration frames are stored under device-named directories:
      .../2026/DWARF 3/DWARF_DARK/...
      .../2026/DWARF Mini/March 2026/...

    Checks every component of the path (case-insensitive).
    Returns device type string or None if no match.
    """
    parts_lower = [p.lower() for p in file_path.parts]
    for device_name, sigs in DEVICE_SIGNATURES.items():
        for path_token in sigs.get('path_names', []):
            if path_token in parts_lower:
                return device_name
    return None


def detect_device(header: fits.Header,
                  file_path: Optional[Path] = None) -> tuple:
    """
    Identify the capture device. Detection cascade (stops at first success):

      1. TELESCOPE header exact match
      2. Seestar serial pattern in TELESCOPE ('S50_...')
      3. CREATOR header
      4. INSTRUMENT header
      5. Directory path  ← handles calibration frames with stripped headers
      6. Image dimensions fallback (warns — possible firmware rename)
      7. UNKNOWN (warns — add to DEVICE_SIGNATURES)

    Returns: (device_type: str, warnings: list[str], source: str)
      source is one of: 'header' | 'path' | 'dimensions' | 'unknown'

    Note: Rev 3 adds the 'source' third element.  Existing callers that unpack
    two values will need to update to:  device, warnings, source = detect_device(…)
    """
    warnings: list = []

    telescop   = str(header.get('TELESCOP',  header.get('TELESCOPE',  ''))).strip()
    instrument = str(header.get('INSTRUME',  header.get('INSTRUMENT', ''))).strip()
    creator    = str(header.get('CREATOR',   '')).strip()
    naxis1     = int(header.get('NAXIS1', 0))
    naxis2     = int(header.get('NAXIS2', 0))

    # ── Methods 1-4: FITS header ──────────────────────────────────────────────
    for device_name, sigs in DEVICE_SIGNATURES.items():
        if telescop and telescop in sigs.get('TELESCOPE', []):
            return device_name, warnings, 'header'
        if device_name == 'SEESTAR_S50' and telescop.upper().startswith('S50_'):
            return device_name, warnings, 'header'
        if creator and creator in sigs.get('CREATOR', []):
            return device_name, warnings, 'header'
        if instrument and instrument in sigs.get('INSTRUMENT', []):
            return device_name, warnings, 'header'

    # ── Method 5: directory path (calibration frames) ─────────────────────────
    if file_path is not None:
        path_device = detect_device_from_path(file_path)
        if path_device:
            logger.debug(
                f"[path-detect] {file_path.name} → {path_device} "
                f"(calibration file with minimal headers — expected behaviour)")
            return path_device, warnings, 'path'

    # ── Method 6: image dimensions (warns — possible firmware rename) ─────────
    dim_map = {
        (3856, 2180): 'DWARF_3',
        (1920, 1080): 'DWARF_MINI',
        (1080, 1920): 'SEESTAR_S50',
    }
    if (naxis1, naxis2) in dim_map:
        device = dim_map[(naxis1, naxis2)]
        msg = (f"Device identified by image size ({naxis1}×{naxis2}) as {device}. "
               f"TELESCOP='{telescop}' not in known signatures — "
               f"possible firmware rename. Update DEVICE_SIGNATURES['TELESCOPE'].")
        warnings.append(msg)
        logger.warning(msg)
        return device, warnings, 'dimensions'

    # ── Method 7: truly unknown ───────────────────────────────────────────────
    msg = (f"UNKNOWN device — TELESCOP='{telescop}', INSTRUMENT='{instrument}', "
           f"CREATOR='{creator}', dimensions={naxis1}×{naxis2}. "
           f"Check surprises.log and add to DEVICE_SIGNATURES if new device.")
    warnings.append(msg)
    logger.warning(msg)
    return 'UNKNOWN', warnings, 'unknown'


# ──────────────────────────────────────────────────────────────────────────────
# Frame classification
# ──────────────────────────────────────────────────────────────────────────────

def classify_frame(header: fits.Header,
                   filename: Optional[str] = None) -> tuple:
    """
    Classify frame type: LIGHT | DARK | FLAT | BIAS | UNKNOWN.

    Detection cascade:
      1. IMAGETYP header (Seestar provides this; DWARF does not)
      2. OBJECT header populated → LIGHT  (DWARF light frames)
      3. OBJECT empty + RA≈0 + DEC≈0 → DARK  (DWARF dark frames)
      4. Filename prefix  ← bias_/dark_/flat_/raw_/light_/unknown_/failed_/stacked-
      5. UNKNOWN (logged to surprises.log)

    Returns: (frame_type: str, warnings: list[str])
    """
    warnings: list = []

    # ── Method 1: IMAGETYP (Seestar) ─────────────────────────────────────────
    imagetyp = str(header.get('IMAGETYP', '')).strip()
    if imagetyp:
        type_map = {
            'LIGHT':      'LIGHT',
            'DARK':       'DARK',
            'FLAT':       'FLAT',
            'FLAT FIELD': 'FLAT',
            'BIAS':       'BIAS',
            'OFFSET':     'BIAS',
        }
        normalised = imagetyp.upper()
        if normalised in type_map:
            return type_map[normalised], warnings
        msg = f"Unrecognised IMAGETYP value: '{imagetyp}' — falling back to inference."
        warnings.append(msg)
        logger.warning(msg)

    # ── Method 2: OBJECT field populated (DWARF lights) ──────────────────────
    object_name = str(header.get('OBJECT', '')).strip()
    if object_name:
        return 'LIGHT', warnings

    # ── Method 3: RA/DEC at zero (DWARF darks) ───────────────────────────────
    try:
        ra  = float(header.get('RA',  -999))
        dec = float(header.get('DEC', -999))
        if abs(ra) < 0.001 and abs(dec) < 0.001:
            return 'DARK', warnings
    except (ValueError, TypeError):
        pass

    # ── Method 4: filename prefix (calibration + device-special frames) ───────
    if filename is not None:
        ft = classify_from_filename(filename)
        if ft is not None:
            logger.debug(
                f"[filename-classify] {filename} → {ft} "
                f"(header had no IMAGETYP/OBJECT — classified from filename prefix)")
            return ft, warnings

    # ── Method 5: cannot determine ───────────────────────────────────────────
    msg = (f"Cannot classify frame — IMAGETYP='{imagetyp}', "
           f"OBJECT='{object_name}', RA={header.get('RA')}, "
           f"DEC={header.get('DEC')}, filename='{filename}'.")
    warnings.append(msg)
    logger.warning(msg)
    return 'UNKNOWN', warnings


# ──────────────────────────────────────────────────────────────────────────────
# Helper extractors
# ──────────────────────────────────────────────────────────────────────────────

def get_temperature(header: fits.Header) -> Optional[float]:
    """
    Extract sensor temperature (°C).
    DWARF → DET-TEMP;  Seestar → CCD-TEMP.
    """
    for field_name in ('DET-TEMP', 'CCD-TEMP', 'CCDTEMP', 'TEMPERAT', 'SET-TEMP'):
        val = header.get(field_name)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                logger.warning(
                    f"Could not convert temperature {field_name}='{val}' to float.")
    return None


def get_exposure(header: fits.Header) -> Optional[float]:
    """
    Extract exposure time (seconds).
    DWARF → EXPTIME;  Seestar → both EXPOSURE and EXPTIME (EXPTIME wins).
    """
    for field_name in ('EXPTIME', 'EXPOSURE', 'EXP_TIME'):
        val = header.get(field_name)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                logger.warning(
                    f"Could not convert exposure {field_name}='{val}' to float.")
    return None


def parse_observation_date(header: fits.Header) -> tuple:
    """
    Parse observation date/time from header.
    Returns: (date_string, year, month) — all None if absent or unparseable.
    """
    for field_name in ('DATE-OBS', 'DATE_OBS', 'DATEOBS'):
        date_str = str(header.get(field_name, '')).strip()
        if date_str:
            break
    else:
        return None, None, None

    try:
        dt = datetime.fromisoformat(date_str.rstrip('Z').replace(' ', 'T'))
        return date_str, dt.year, dt.month
    except ValueError:
        logger.warning(f"Could not parse observation date: '{date_str}'")
        return date_str, None, None


# ──────────────────────────────────────────────────────────────────────────────
# Known headers (anything else → unknown_headers + surprises.log)
#
# Rev 3: two layers —
#   _KNOWN_HEADERS         : exact header names (frozenset lookup, O(1))
#   _KNOWN_HEADER_PREFIXES : variable-suffix headers (prefix check, O(n) short)
# ──────────────────────────────────────────────────────────────────────────────

_KNOWN_HEADERS: frozenset = frozenset({
    # FITS structural
    'SIMPLE', 'BITPIX', 'NAXIS', 'NAXIS1', 'NAXIS2', 'NAXIS3', 'EXTEND',
    'BZERO', 'BSCALE',
    # Device identification
    'TELESCOP', 'TELESCOPE', 'INSTRUME', 'INSTRUMENT',
    'CREATOR', 'PRODUCER', 'ORIGIN',
    # Target / pointing
    'OBJECT', 'RA', 'DEC',
    # Capture parameters
    'EXPTIME', 'EXPOSURE', 'EXP_TIME', 'GAIN',
    'FILTER', 'DATE-OBS', 'DATE_OBS', 'DATEOBS',
    # Temperature
    'DET-TEMP', 'CCD-TEMP', 'CCDTEMP', 'TEMPERAT', 'SET-TEMP',
    # Optics / sensor geometry
    'BAYERPAT', 'FOCALLEN', 'XPIXSZ', 'YPIXSZ',
    'XBINNING', 'YBINNING', 'CCDXBIN', 'CCDYBIN',
    'XORGSUBF', 'YORGSUBF', 'APERTURE',
    # Frame classification
    'IMAGETYP',
    # Site / location
    'SITELAT', 'SITELONG', 'SITEELEV',
    # Device metadata
    'FIRMWARE', 'MACADDR', 'EQMODE', 'PROGRAM', 'CAMERA',
    # Stacking / processing
    'RESTACK', 'STACKCNT', 'TOTALEXP', 'FOCUSPOS',
    # Standard WCS (astrometry solve headers)
    'WCSAXES', 'RADESYS', 'EQUINOX', 'LONPOLE', 'LATPOLE',
    'CTYPE1', 'CTYPE2', 'CRVAL1', 'CRVAL2',
    'CRPIX1', 'CRPIX2', 'CDELT1', 'CDELT2',
    'CUNIT1', 'CUNIT2',
    'CD1_1', 'CD1_2', 'CD2_1', 'CD2_2',
    'PC1_1', 'PC1_2', 'PC2_1', 'PC2_2',
    # FITS bookkeeping
    'COMMENT', 'HISTORY', 'END',
})

# Variable-suffix header families (matched by prefix, case-insensitive).
# Used for WCS headers that embed axis/coefficient indices in their names,
# and for Seestar corner-coordinate headers.
_KNOWN_HEADER_PREFIXES: frozenset = frozenset({
    # Standard WCS axis-indexed headers: CRPIXn, CTYPEn, CDELTn …
    'CRPIX', 'CRVAL', 'CTYPE', 'CDELT', 'CUNIT',
    # WCS matrix terms: PC1_1, CD1_1, PV2_3 …
    'PC', 'PV', 'PS',
    # SIP distortion polynomial coefficients: A_2_0, AP_1_1 …
    'A_', 'B_', 'AP', 'BP',
    # Seestar corner-coordinate headers: CRTLxx, CRTRxx, CRBLxx, CRBRxx
    'CRTL', 'CRTR', 'CRBL', 'CRBR',
})


def _is_known_header(key: str) -> bool:
    """Return True if the header key is expected and should not be reported."""
    key_upper = key.upper()
    if key_upper in _KNOWN_HEADERS:
        return True
    return any(key_upper.startswith(prefix.upper())
               for prefix in _KNOWN_HEADER_PREFIXES)


# ──────────────────────────────────────────────────────────────────────────────
# Main extraction function
# ──────────────────────────────────────────────────────────────────────────────

def extract_metadata(fits_path: Path) -> FITSMetadata:
    """
    Extract all available metadata from a single FITS file.

    Safe for batch use — errors are captured in the returned FITSMetadata
    rather than raised, so a directory scan continues past bad files.
    """
    meta = FITSMetadata(
        file_path=str(fits_path),
        file_name=fits_path.name,
        file_extension=fits_path.suffix.lower(),
    )

    if not fits_path.exists():
        meta.is_valid = False
        meta.warnings.append(f"File not found: {fits_path}")
        logger.error(f"File not found: {fits_path}")
        return meta

    if fits_path.suffix not in FITS_EXTENSIONS:
        msg = f"Unexpected extension '{fits_path.suffix}' — attempting to read anyway."
        meta.warnings.append(msg)
        logger.warning(msg)

    try:
        with fits.open(fits_path, ignore_missing_simple=True) as hdul:
            header = hdul[0].header

            # ── Device ────────────────────────────────────────────────────────
            meta.device_type, dev_w, meta.device_source = detect_device(
                header, fits_path)
            meta.warnings.extend(dev_w)

            meta.telescope   = str(header.get('TELESCOP',  header.get('TELESCOPE',  ''))).strip()
            meta.instrument  = str(header.get('INSTRUME',  header.get('INSTRUMENT', ''))).strip()
            meta.origin      = str(header.get('ORIGIN',    '')).strip()
            meta.firmware    = str(header.get('FIRMWARE',  '')).strip()
            meta.mac_address = str(header.get('MACADDR',   '')).strip()

            if meta.mac_address:
                meta.device_id = f"{meta.device_type}_{meta.mac_address[-6:]}"
            elif meta.telescope.upper().startswith('S50_'):
                meta.device_id = meta.telescope
            else:
                meta.device_id = meta.device_type

            # ── Frame type ────────────────────────────────────────────────────
            meta.frame_type, cls_w = classify_frame(header, fits_path.name)
            meta.warnings.extend(cls_w)

            # ── Target ────────────────────────────────────────────────────────
            meta.object_name = str(header.get('OBJECT', '')).strip()
            try:
                raw_ra  = float(header.get('RA',  0))
                raw_dec = float(header.get('DEC', 0))
                meta.ra  = raw_ra  if raw_ra  != 0.0 else None
                meta.dec = raw_dec if raw_dec != 0.0 else None
            except (ValueError, TypeError):
                pass

            # ── Capture parameters from headers ───────────────────────────────
            meta.exposure_time = get_exposure(header)
            meta.temperature   = get_temperature(header)
            meta.filter_name   = str(header.get('FILTER', '')).strip()
            try:
                raw_gain = header.get('GAIN')
                meta.gain = int(raw_gain) if raw_gain is not None else None
            except (ValueError, TypeError):
                pass

            # ── Fill missing capture params from filename (calibration files) ─
            if meta.frame_type in ('DARK', 'BIAS', 'FLAT'):
                cal = parse_calibration_filename(fits_path.name)
                if meta.exposure_time is None and 'exposure_time' in cal:
                    meta.exposure_time = cal['exposure_time']
                if meta.gain is None and 'gain' in cal:
                    meta.gain = cal['gain']
                if meta.temperature is None and 'temperature' in cal:
                    meta.temperature = cal['temperature']

            # ── Image properties ──────────────────────────────────────────────
            for attr, key in (('image_width',  'NAXIS1'),
                               ('image_height', 'NAXIS2'),
                               ('bit_depth',    'BITPIX')):
                try:
                    val = header.get(key)
                    setattr(meta, attr, int(val) if val is not None else None)
                except (ValueError, TypeError):
                    pass

            meta.bayer_pattern = str(header.get('BAYERPAT', '')).strip()
            for attr, key in (('focal_length', 'FOCALLEN'),
                               ('pixel_size_x', 'XPIXSZ'),
                               ('pixel_size_y', 'YPIXSZ')):
                try:
                    val = header.get(key)
                    setattr(meta, attr, float(val) if val is not None else None)
                except (ValueError, TypeError):
                    pass

            # ── Date / Time ───────────────────────────────────────────────────
            (meta.observation_date,
             meta.observation_year,
             meta.observation_month) = parse_observation_date(header)

            # ── Location (Seestar) ────────────────────────────────────────────
            try:
                lat = header.get('SITELAT')
                lon = header.get('SITELONG')
                meta.site_latitude  = float(lat) if lat is not None else None
                meta.site_longitude = float(lon) if lon is not None else None
            except (ValueError, TypeError):
                pass

            # ── Catch unknown headers → surprises.log ─────────────────────────
            for key in header.keys():
                if not _is_known_header(key):
                    val = str(header[key])
                    meta.unknown_headers[key] = val
                    logger.info(
                        f"Unknown header [{fits_path.name}] {key} = {val}")

    except Exception as exc:
        meta.is_valid = False
        meta.warnings.append(f"Error reading FITS: {exc}")
        logger.error(f"Failed to read {fits_path}: {exc}")

    return meta


# ──────────────────────────────────────────────────────────────────────────────
# Directory scanner
# ──────────────────────────────────────────────────────────────────────────────

def scan_directory(root_path: Path, recursive: bool = True) -> list:
    """
    Scan a directory tree and extract metadata from every FITS file.
    Handles mixed structures: Year/Month, Year/Device, Year/Device/Month, etc.
    Continues past unreadable files — errors captured per-file.
    """
    pattern = '**/*' if recursive else '*'
    fits_files = sorted(
        p for ext in FITS_EXTENSIONS
        for p in root_path.glob(f'{pattern}{ext}')
        if p.is_file()
    )

    logger.info(f"Found {len(fits_files)} FITS file(s) under {root_path}")

    results = []
    for i, fits_file in enumerate(fits_files, 1):
        logger.debug(f"[{i}/{len(fits_files)}] {fits_file.name}")
        results.append(extract_metadata(fits_file))

    return results


# ──────────────────────────────────────────────────────────────────────────────
# Reports
# ──────────────────────────────────────────────────────────────────────────────

def format_report(meta: FITSMetadata) -> str:
    """Human-readable single-file report."""
    W   = 64
    sep = '─' * W
    ln  = lambda label, val: f"  {label:<20}{val}"

    rows = [
        '=' * W,
        f"  FILE:  {meta.file_name}",
        '=' * W,
        ln('Device',      f"{meta.device_type}  (id: {meta.device_id})"),
        ln('Detected via', meta.device_source or 'header'),
        ln('Frame type',  meta.frame_type),
        ln('Target',      meta.object_name or '(none)'),
        ln('Date',        meta.observation_date or 'unknown'),
        sep,
        ln('Exposure',    f"{meta.exposure_time}s" if meta.exposure_time is not None else 'unknown'),
        ln('Gain',        str(meta.gain)            if meta.gain          is not None else 'unknown'),
        ln('Filter',      meta.filter_name          or '(none)'),
        ln('Sensor temp', f"{meta.temperature}°C"   if meta.temperature   is not None else 'unknown'),
        sep,
        ln('Image size',  f"{meta.image_width}×{meta.image_height} px"
                          if meta.image_width else 'unknown'),
        ln('Bit depth',   f"{meta.bit_depth} bit"   if meta.bit_depth else 'unknown'),
        ln('Bayer',       meta.bayer_pattern         or 'unknown'),
        ln('Focal length', f"{meta.focal_length}mm"  if meta.focal_length else 'unknown'),
        ln('Pixel size',   f"{meta.pixel_size_x}µm"  if meta.pixel_size_x else 'unknown'),
    ]

    if meta.firmware:
        rows.append(ln('Firmware',    meta.firmware))
    if meta.mac_address:
        rows.append(ln('MAC address', meta.mac_address))
    if meta.site_latitude is not None:
        rows.append(ln('Location',
                       f"{meta.site_latitude:.4f}°, {meta.site_longitude:.4f}°"))
    if meta.warnings:
        rows += [sep, '  ⚠  WARNINGS:']
        rows += [f"     • {w}" for w in meta.warnings]
    if meta.unknown_headers:
        rows += [sep, '  🔍 UNKNOWN HEADERS (also in surprises.log):']
        rows += [f"     {k} = {v}" for k, v in meta.unknown_headers.items()]

    rows.append('=' * W)
    return '\n'.join(rows)


def print_summary(results: list) -> None:
    """Aggregate statistics for a batch scan."""
    total   = len(results)
    valid   = sum(1 for r in results if r.is_valid)
    warn_total = sum(len(r.warnings) for r in results)   # Rev 3: warning count
    devices: dict = {}
    frames:  dict = {}
    years:   dict = {}
    targets: set  = set()

    for r in results:
        devices[r.device_type] = devices.get(r.device_type, 0) + 1
        frames[r.frame_type]   = frames.get(r.frame_type,   0) + 1
        if r.observation_year:
            years[r.observation_year] = years.get(r.observation_year, 0) + 1
        if r.object_name:
            targets.add(r.object_name)

    W = 64
    print('\n' + '=' * W)
    print('  SCAN SUMMARY')
    print('=' * W)
    print(f"  Total files : {total}  ({valid} valid, {total - valid} errors)")
    print(f"  Warnings    : {warn_total}  (see surprises.log for detail)")   # Rev 3
    print(f"  Devices     : {dict(sorted(devices.items()))}")
    print(f"  Frame types : {dict(sorted(frames.items()))}")
    print(f"  Years       : {dict(sorted(years.items()))}")
    print(f"  Targets     : {len(targets)} unique")
    if targets:
        for t in sorted(targets):
            print(f"    • {t}")
    print('=' * W)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description='Extract metadata from astrophotography FITS files.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single file
  python fits_metadata_extractor.py /path/to/file.fits

  # Scan a full year directory
  python fits_metadata_extractor.py ~/Astronomy/Astrocapture/2026 --scan --summary

  # JSON output for piping to other scripts
  python fits_metadata_extractor.py ~/Astronomy/Astrocapture/2026 --scan --json
        """
    )
    parser.add_argument('path',      help='FITS file or root directory')
    parser.add_argument('--scan',    action='store_true', help='Scan directory recursively')
    parser.add_argument('--json',    action='store_true', help='Output as JSON')
    parser.add_argument('--summary', action='store_true', help='Summary statistics only')
    args = parser.parse_args()

    target = Path(args.path).expanduser()

    if target.is_dir() or args.scan:
        results = scan_directory(target)
        if args.json:
            import json as _json
            print(_json.dumps([asdict(r) for r in results], indent=2, default=str))
        elif args.summary:
            print_summary(results)
        else:
            for r in results:
                print(format_report(r))
            print_summary(results)
    else:
        meta = extract_metadata(target)
        if args.json:
            import json as _json
            print(_json.dumps(asdict(meta), indent=2, default=str))
        else:
            print(format_report(meta))


if __name__ == '__main__':
    main()
