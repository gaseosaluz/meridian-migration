#!/usr/bin/env python3
"""
fits_metadata_extractor.py

Astrophotography FITS Metadata Extractor
Phase 1: Header Reading, Device Detection, Frame Classification

Built for Ed's Astrophotography Migration Project
Sprint 2, Phase 1 - Updated with real-world header validation

Validated against actual files from:
  - DWARF 3 (firmware pre-1.4 / TELESCOP='DWARFIII')
  - DWARF 3 (firmware 1.4.15.2 / TELESCOP='DWARF 3')
  - DWARF Mini (firmware 1.0.25.2 / TELESCOP='DWARF mini')
  - Seestar S50 (CREATOR='ZWO Seestar S50')
"""

import os
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
# No other code changes needed - device detection logic reads from here.
# ──────────────────────────────────────────────────────────────────────────────

DEVICE_SIGNATURES: dict = {
    "DWARF_3": {
        # 'DWARFIII' = pre-firmware 1.4 (2025)
        # 'DWARF 3'  = firmware 1.4+ (2026 onwards)
        "TELESCOPE":  ["DWARFIII", "DWARF 3"],
        "INSTRUMENT": ["DWARFIII", "DWARF 3"],
        "ORIGIN":     ["DWARFLAB"],
        "image_size": (3856, 2180),          # NAXIS1 x NAXIS2 as secondary check
    },
    "DWARF_MINI": {
        "TELESCOPE":  ["DWARF mini"],         # lowercase 'm' - exact match required
        "INSTRUMENT": ["DWARF mini"],
        "ORIGIN":     ["DWARFLAB"],
        "image_size": (1920, 1080),
    },
    "SEESTAR_S50": {
        # TELESCOP embeds serial: 'S50_40d24ed8' - handled separately
        "CREATOR":    ["ZWO Seestar S50"],    # most reliable field
        "INSTRUMENT": ["Seestar S50"],
        "image_size": (1080, 1920),           # portrait orientation
    },
    # ── Add future devices here without touching any other code ──────────────
    # "SEESTAR_S30_PRO": {
    #     "CREATOR":    ["ZWO Seestar S30 Pro"],
    #     "INSTRUMENT": ["Seestar S30 Pro"],
    # },
    # "ASIAIR": {
    #     "CREATOR":    ["ASIAIR"],
    # },
}

# All FITS file extensions we will process
FITS_EXTENSIONS: frozenset = frozenset({'.fits', '.fit', '.FITS', '.FIT'})


# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

def setup_logging(log_file: Optional[str] = 'surprises.log') -> logging.Logger:
    """
    Two-channel logging:
      - Console: INFO and above (normal progress)
      - File:    DEBUG and above (full detail including unknown headers)
    The 'surprises.log' file is your early-warning system for firmware changes.
    """
    logger = logging.getLogger('fits_extractor')
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter('%(asctime)s [%(levelname)-8s] %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    if log_file:
        fh = logging.FileHandler(log_file, mode='a')
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

    # ── File ──────────────────────────────────────────────────
    file_path:      str = ""
    file_name:      str = ""
    file_extension: str = ""

    # ── Device ────────────────────────────────────────────────
    device_type:  str = "UNKNOWN"   # DWARF_3 | DWARF_MINI | SEESTAR_S50 | UNKNOWN
    device_id:    str = ""          # unique per physical unit (MAC or serial)
    telescope:    str = ""
    instrument:   str = ""
    origin:       str = ""
    firmware:     str = ""
    mac_address:  str = ""

    # ── Frame ─────────────────────────────────────────────────
    frame_type: str = "UNKNOWN"     # LIGHT | DARK | FLAT | BIAS | UNKNOWN

    # ── Target ────────────────────────────────────────────────
    object_name: str             = ""
    ra:          Optional[float] = None
    dec:         Optional[float] = None

    # ── Capture parameters ────────────────────────────────────
    exposure_time: Optional[float] = None
    gain:          Optional[int]   = None
    filter_name:   str             = ""
    temperature:   Optional[float] = None   # sensor temp in °C

    # ── Image properties ──────────────────────────────────────
    image_width:  Optional[int]   = None
    image_height: Optional[int]   = None
    bit_depth:    Optional[int]   = None
    bayer_pattern: str            = ""
    focal_length: Optional[float] = None
    pixel_size_x: Optional[float] = None
    pixel_size_y: Optional[float] = None

    # ── Date / Time ───────────────────────────────────────────
    observation_date:  Optional[str] = None
    observation_year:  Optional[int] = None
    observation_month: Optional[int] = None

    # ── Location (Seestar provides this) ──────────────────────
    site_latitude:  Optional[float] = None
    site_longitude: Optional[float] = None

    # ── Quality / diagnostics ─────────────────────────────────
    is_valid:        bool             = True
    warnings:        list             = field(default_factory=list)
    unknown_headers: dict             = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────────
# Core extraction helpers
# ──────────────────────────────────────────────────────────────────────────────

def detect_device(header: fits.Header) -> tuple:
    """
    Identify the capture device from FITS headers.

    Detection order:
      1. TELESCOPE field exact match
      2. Seestar's serial-embedded TELESCOPE ('S50_...')
      3. CREATOR field (Seestar)
      4. INSTRUMENT field
      5. Fallback: image dimensions (with warning - possible new firmware)
      6. UNKNOWN (with warning logged to surprises.log)

    Returns: (device_type: str, warnings: list[str])
    """
    warnings: list = []

    # Normalise field names - astropy uses the 8-char FITS key
    telescop   = str(header.get('TELESCOP',  header.get('TELESCOPE',  ''))).strip()
    instrument = str(header.get('INSTRUME',  header.get('INSTRUMENT', ''))).strip()
    creator    = str(header.get('CREATOR',   '')).strip()
    naxis1     = int(header.get('NAXIS1', 0))
    naxis2     = int(header.get('NAXIS2', 0))

    for device_name, sigs in DEVICE_SIGNATURES.items():
        # 1. TELESCOPE exact match
        if telescop and telescop in sigs.get('TELESCOPE', []):
            return device_name, warnings

        # 2. Seestar serial pattern: 'S50_xxxxxxxx'
        if device_name == 'SEESTAR_S50' and telescop.upper().startswith('S50_'):
            return device_name, warnings

        # 3. CREATOR field
        if creator and creator in sigs.get('CREATOR', []):
            return device_name, warnings

        # 4. INSTRUMENT field
        if instrument and instrument in sigs.get('INSTRUMENT', []):
            return device_name, warnings

    # 5. Fallback: image dimensions (firmware may have renamed the telescope field)
    dim_map = {
        (3856, 2180): 'DWARF_3',
        (1920, 1080): 'DWARF_MINI',
        (1080, 1920): 'SEESTAR_S50',
    }
    if (naxis1, naxis2) in dim_map:
        device = dim_map[(naxis1, naxis2)]
        msg = (f"Device identified by image size ({naxis1}×{naxis2}) as {device}. "
               f"TELESCOP='{telescop}' is not in known signatures — "
               f"possible firmware rename. Please update DEVICE_SIGNATURES.")
        warnings.append(msg)
        logger.warning(msg)
        return device, warnings

    # 6. Truly unknown
    msg = (f"UNKNOWN device — TELESCOP='{telescop}', INSTRUMENT='{instrument}', "
           f"CREATOR='{creator}', dimensions={naxis1}×{naxis2}. "
           f"Check surprises.log and update DEVICE_SIGNATURES if this is a new device.")
    warnings.append(msg)
    logger.warning(msg)
    return 'UNKNOWN', warnings


def classify_frame(header: fits.Header) -> tuple:
    """
    Classify frame type: LIGHT | DARK | FLAT | BIAS | UNKNOWN.

    Strategy:
      1. IMAGETYP field if present (Seestar provides this; DWARF does not)
      2. OBJECT field populated → LIGHT (DWARF)
      3. OBJECT empty + RA≈0 + DEC≈0 → DARK (DWARF)
      4. Anything else → UNKNOWN (logged)

    Returns: (frame_type: str, warnings: list[str])
    """
    warnings: list = []

    # ── Method 1: explicit IMAGETYP (Seestar) ─────────────────────────────────
    imagetyp = str(header.get('IMAGETYP', '')).strip()
    if imagetyp:
        normalised = imagetyp.upper()
        type_map = {
            'LIGHT':      'LIGHT',
            'DARK':       'DARK',
            'FLAT':       'FLAT',
            'FLAT FIELD': 'FLAT',
            'BIAS':       'BIAS',
            'OFFSET':     'BIAS',
        }
        if normalised in type_map:
            return type_map[normalised], warnings

        # Unrecognised value - log it and fall through
        msg = f"Unrecognised IMAGETYP value: '{imagetyp}' — falling back to inference."
        warnings.append(msg)
        logger.warning(msg)

    # ── Method 2: infer from OBJECT + RA/DEC (DWARF) ─────────────────────────
    object_name = str(header.get('OBJECT', '')).strip()
    try:
        ra  = float(header.get('RA',  -999))
        dec = float(header.get('DEC', -999))
        ra_zero  = abs(ra)  < 0.001
        dec_zero = abs(dec) < 0.001
    except (ValueError, TypeError):
        ra_zero = dec_zero = False

    if object_name:
        return 'LIGHT', warnings

    if ra_zero and dec_zero:
        return 'DARK', warnings

    # ── Can't determine ───────────────────────────────────────────────────────
    msg = (f"Cannot classify frame — IMAGETYP='{imagetyp}', "
           f"OBJECT='{object_name}', RA={header.get('RA')}, DEC={header.get('DEC')}.")
    warnings.append(msg)
    logger.warning(msg)
    return 'UNKNOWN', warnings


def get_temperature(header: fits.Header) -> Optional[float]:
    """
    Extract sensor temperature (°C).
    DWARF uses DET-TEMP; Seestar uses CCD-TEMP.
    Checks all known variants so new field names only need adding here.
    """
    for field_name in ('DET-TEMP', 'CCD-TEMP', 'CCDTEMP', 'TEMPERAT', 'SET-TEMP'):
        val = header.get(field_name)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                logger.warning(f"Could not convert temperature field {field_name}='{val}' to float.")
    return None


def get_exposure(header: fits.Header) -> Optional[float]:
    """
    Extract exposure time in seconds.
    DWARF uses EXPTIME; Seestar has both EXPOSURE and EXPTIME.
    EXPTIME is the more standard FITS keyword so it takes priority.
    """
    for field_name in ('EXPTIME', 'EXPOSURE', 'EXP_TIME', 'EXPTIMEE'):
        val = header.get(field_name)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                logger.warning(f"Could not convert exposure field {field_name}='{val}' to float.")
    return None


def parse_observation_date(header: fits.Header) -> tuple:
    """
    Parse observation date/time from header.
    Handles ISO 8601 with and without fractional seconds.

    Returns: (date_string, year, month)
    """
    # Try standard and device-specific field names
    for field_name in ('DATE-OBS', 'DATE_OBS', 'DATEOBS'):
        date_str = str(header.get(field_name, '')).strip()
        if date_str:
            break
    else:
        return None, None, None

    try:
        # Remove trailing Z if present; handle microseconds
        clean = date_str.rstrip('Z').replace(' ', 'T')
        dt = datetime.fromisoformat(clean)
        return date_str, dt.year, dt.month
    except ValueError:
        logger.warning(f"Could not parse observation date: '{date_str}'")
        return date_str, None, None


# ──────────────────────────────────────────────────────────────────────────────
# Main extraction function
# ──────────────────────────────────────────────────────────────────────────────

# Headers we know about - anything else goes to unknown_headers and surprises.log
_KNOWN_HEADERS: frozenset = frozenset({
    'SIMPLE', 'BITPIX', 'NAXIS', 'NAXIS1', 'NAXIS2', 'NAXIS3', 'EXTEND',
    'BZERO', 'BSCALE',
    'TELESCOP', 'TELESCOPE', 'INSTRUME', 'INSTRUMENT', 'CREATOR', 'PRODUCER',
    'ORIGIN', 'OBJECT', 'RA', 'DEC',
    'EXPTIME', 'EXPOSURE', 'EXP_TIME', 'GAIN',
    'FILTER', 'DATE-OBS', 'DATE_OBS', 'DATEOBS',
    'DET-TEMP', 'CCD-TEMP', 'CCDTEMP', 'SET-TEMP',
    'BAYERPAT', 'FOCALLEN', 'XPIXSZ', 'YPIXSZ',
    'XBINNING', 'YBINNING', 'CCDXBIN', 'CCDYBIN',
    'IMAGETYP', 'SITELAT', 'SITELONG', 'SITEELEV',
    'FIRMWARE', 'MACADDR', 'EQMODE', 'PROGRAM',
    'RESTACK', 'CAMERA', 'STACKCNT', 'TOTALEXP',
    'FOCUSPOS', 'APERTURE', 'XORGSUBF', 'YORGSUBF',
    'COMMENT', 'HISTORY', 'END',
})


def extract_metadata(fits_path: Path) -> FITSMetadata:
    """
    Extract all available metadata from a FITS file.

    Safe to call on any file - errors are captured into the returned
    FITSMetadata object rather than raised, so a batch scan can continue
    past bad files.
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
        msg = f"Unexpected file extension '{fits_path.suffix}' — attempting to read anyway."
        meta.warnings.append(msg)
        logger.warning(msg)

    try:
        with fits.open(fits_path, ignore_missing_simple=True) as hdul:
            header = hdul[0].header

            # ── Device ────────────────────────────────────────────────────────
            meta.device_type, dev_w = detect_device(header)
            meta.warnings.extend(dev_w)

            meta.telescope   = str(header.get('TELESCOP',  header.get('TELESCOPE',  ''))).strip()
            meta.instrument  = str(header.get('INSTRUME',  header.get('INSTRUMENT', ''))).strip()
            meta.origin      = str(header.get('ORIGIN',    '')).strip()
            meta.firmware    = str(header.get('FIRMWARE',  '')).strip()
            meta.mac_address = str(header.get('MACADDR',   '')).strip()

            # Unique per-instrument ID for tracking individual scopes
            if meta.mac_address:
                meta.device_id = f"{meta.device_type}_{meta.mac_address[-6:]}"
            elif meta.telescope.upper().startswith('S50_'):
                meta.device_id = meta.telescope          # e.g. S50_40d24ed8
            else:
                meta.device_id = meta.device_type

            # ── Frame type ────────────────────────────────────────────────────
            meta.frame_type, cls_w = classify_frame(header)
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

            # ── Capture parameters ────────────────────────────────────────────
            meta.exposure_time = get_exposure(header)
            meta.temperature   = get_temperature(header)
            meta.filter_name   = str(header.get('FILTER', '')).strip()
            try:
                raw_gain = header.get('GAIN')
                meta.gain = int(raw_gain) if raw_gain is not None else None
            except (ValueError, TypeError):
                pass

            # ── Image properties ──────────────────────────────────────────────
            for attr, key in (('image_width',  'NAXIS1'),
                               ('image_height', 'NAXIS2'),
                               ('bit_depth',    'BITPIX')):
                try:
                    val = header.get(key)
                    setattr(meta, attr, int(val) if val else None)
                except (ValueError, TypeError):
                    pass

            meta.bayer_pattern = str(header.get('BAYERPAT', '')).strip()
            for attr, key in (('focal_length', 'FOCALLEN'),
                               ('pixel_size_x', 'XPIXSZ'),
                               ('pixel_size_y', 'YPIXSZ')):
                try:
                    val = header.get(key)
                    setattr(meta, attr, float(val) if val else None)
                except (ValueError, TypeError):
                    pass

            # ── Date / Time ───────────────────────────────────────────────────
            meta.observation_date, meta.observation_year, meta.observation_month = \
                parse_observation_date(header)

            # ── Location (Seestar) ────────────────────────────────────────────
            try:
                lat = header.get('SITELAT')
                lon = header.get('SITELONG')
                meta.site_latitude  = float(lat) if lat is not None else None
                meta.site_longitude = float(lon) if lon is not None else None
            except (ValueError, TypeError):
                pass

            # ── Unknown headers → surprises.log ──────────────────────────────
            for key in header.keys():
                if key.upper() not in _KNOWN_HEADERS:
                    val = str(header[key])
                    meta.unknown_headers[key] = val
                    logger.info(f"Unknown header [{fits_path.name}] {key} = {val}")

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
    Recursively scan a directory and extract metadata from every FITS file.
    Handles mixed structures: Year/Month, Year/Device, Year/Device/Month, etc.
    Continues past unreadable files (errors captured in each FITSMetadata).
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
# Formatted report
# ──────────────────────────────────────────────────────────────────────────────

def format_report(meta: FITSMetadata) -> str:
    """Human-readable single-file report."""
    W = 62
    sep  = '─' * W
    line = lambda label, val: f"  {label:<18}{val}"

    rows = [
        '=' * W,
        f"  FILE:  {meta.file_name}",
        '=' * W,
        line('Device',    f"{meta.device_type}  (id: {meta.device_id})"),
        line('Frame type', meta.frame_type),
        line('Target',     meta.object_name or '(none)'),
        line('Date',       meta.observation_date or 'unknown'),
        sep,
        line('Exposure',   f"{meta.exposure_time}s" if meta.exposure_time is not None else 'unknown'),
        line('Gain',       str(meta.gain)           if meta.gain          is not None else 'unknown'),
        line('Filter',     meta.filter_name         or '(none)'),
        line('Sensor temp', f"{meta.temperature}°C" if meta.temperature   is not None else 'unknown'),
        sep,
        line('Image size',  f"{meta.image_width}×{meta.image_height} px"
                            if meta.image_width else 'unknown'),
        line('Bit depth',   f"{meta.bit_depth} bit" if meta.bit_depth else 'unknown'),
        line('Bayer',       meta.bayer_pattern       or 'unknown'),
        line('Focal length', f"{meta.focal_length}mm" if meta.focal_length else 'unknown'),
        line('Pixel size',   f"{meta.pixel_size_x}µm" if meta.pixel_size_x else 'unknown'),
    ]

    if meta.firmware:
        rows.append(line('Firmware', meta.firmware))
    if meta.mac_address:
        rows.append(line('MAC address', meta.mac_address))
    if meta.site_latitude is not None:
        rows.append(line('Location', f"{meta.site_latitude:.4f}°, {meta.site_longitude:.4f}°"))

    if meta.warnings:
        rows += [sep, '  ⚠  WARNINGS:']
        rows += [f"     • {w}" for w in meta.warnings]

    if meta.unknown_headers:
        rows += [sep, '  🔍 UNKNOWN HEADERS (also in surprises.log):']
        rows += [f"     {k} = {v}" for k, v in meta.unknown_headers.items()]

    rows.append('=' * W)
    return '\n'.join(rows)


def print_summary(results: list) -> None:
    """Print aggregate statistics for a batch scan."""
    total   = len(results)
    valid   = sum(1 for r in results if r.is_valid)
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

    W = 62
    print('\n' + '=' * W)
    print(f"  SCAN SUMMARY")
    print('=' * W)
    print(f"  Total files : {total}  ({valid} valid, {total-valid} errors)")
    print(f"  Devices     : {dict(sorted(devices.items()))}")
    print(f"  Frame types : {dict(sorted(frames.items()))}")
    print(f"  Years       : {dict(sorted(years.items()))}")
    print(f"  Targets     : {len(targets)} unique  {sorted(targets)}")
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
  # Single file report
  python fits_metadata_extractor.py /path/to/file.fits

  # Scan entire astrophotography root
  python fits_metadata_extractor.py ~/Astronomy/Astrocapture --scan

  # Summary statistics only
  python fits_metadata_extractor.py ~/Astronomy/Astrocapture --scan --summary

  # JSON output (for piping to other scripts)
  python fits_metadata_extractor.py ~/Astronomy/Astrocapture --scan --json
        """
    )
    parser.add_argument('path',    help='FITS file or root directory to scan')
    parser.add_argument('--scan',  action='store_true', help='Scan directory recursively')
    parser.add_argument('--json',  action='store_true', help='Output as JSON')
    parser.add_argument('--summary', action='store_true', help='Summary statistics only')
    args = parser.parse_args()

    target = Path(args.path).expanduser()

    if target.is_dir() or args.scan:
        results = scan_directory(target)
        if args.json:
            print(json.dumps([asdict(r) for r in results], indent=2, default=str))
        elif args.summary:
            print_summary(results)
        else:
            for r in results:
                print(format_report(r))
            print_summary(results)
    else:
        meta = extract_metadata(target)
        if args.json:
            print(json.dumps(asdict(meta), indent=2, default=str))
        else:
            print(format_report(meta))


if __name__ == '__main__':
    main()
