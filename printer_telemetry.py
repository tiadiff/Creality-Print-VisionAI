"""
Creality Print Vision AI: Synchronized Moonraker + G-code Telemetry Engine

Matches Klipper's real-time physical nozzle kinematics (`motion_report`) against
lightweight HTTP Range slices of the active `.gcode` file to pinpoint:
  1. Exact slicer feature currently printed (`Outer wall`, `Inner wall`,
     `Sparse infill`, `Ironing`, `Support`, `Top surface`, `Bridge`, etc.)
  2. True physical print/extrusion speed (mm/s), filament feed velocity (mm/s),
     and volumetric flow rate (mm^3/s).

Safety: strictly read-only HTTP GET requests (with bounded Range headers).
Never sends printer control or G-code mutation commands.
"""

from __future__ import annotations

import bisect
import ipaddress
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import cp_lifecycle


# Default nozzle filament cross-sectional area for 1.75 mm filament (mm^2)
DEFAULT_FILAMENT_DIAMETER_MM = 1.75

# Bounded Range fetch sizing (keeps printer HTTP/SD load minimal)
LOOKBACK_BYTES = 131072      # 128 KB lookback behind Klipper's virtual_sdcard cursor
LOOKAHEAD_BYTES = 8192       # Small prefetch ahead of virtual_sdcard.file_position
MAX_CACHE_BYTES = 262144     # 256 KB rolling in-memory buffer per active job
MAX_BACKWALK_CHUNKS = 3      # Fallback backward chunks if a section spans >128 KB

# Italian labels & semantic categories for Creality Print / OrcaSlicer / PrusaSlicer / Cura
FEATURE_MAP: Dict[str, Tuple[str, str]] = {
    "outer wall": ("Parete esterna", "wall"),
    "external perimeter": ("Parete esterna", "wall"),
    "wall-outer": ("Parete esterna", "wall"),
    "inner wall": ("Parete interna", "wall"),
    "perimeter": ("Parete interna", "wall"),
    "wall-inner": ("Parete interna", "wall"),
    "overhang wall": ("Parete a sbalzo", "wall"),
    "overhang perimeter": ("Parete a sbalzo", "wall"),
    "sparse infill": ("Riempimento", "infill"),
    "infill": ("Riempimento", "infill"),
    "fill": ("Riempimento", "infill"),
    "internal solid infill": ("Riempimento solido", "solid"),
    "solid infill": ("Riempimento solido", "solid"),
    "gap infill": ("Riempimento spazi", "infill"),
    "top surface": ("Superficie superiore", "surface"),
    "top solid infill": ("Superficie superiore", "surface"),
    "bottom surface": ("Superficie inferiore", "surface"),
    "bottom solid infill": ("Superficie inferiore", "surface"),
    "ironing": ("Stiratura (Ironing)", "ironing"),
    "bridge": ("Ponte", "bridge"),
    "internal bridge": ("Ponte interno", "bridge"),
    "bridge infill": ("Ponte", "bridge"),
    "support": ("Supporto", "support"),
    "support material": ("Supporto", "support"),
    "support interface": ("Interfaccia supporto", "support"),
    "support material interface": ("Interfaccia supporto", "support"),
    "support transition": ("Transizione supporto", "support"),
    "skirt": ("Skirt", "adhesion"),
    "brim": ("Brim", "adhesion"),
    "skirt/brim": ("Skirt / Brim", "adhesion"),
    "prime tower": ("Torre di spurgo", "prime"),
    "wipe tower": ("Torre di spurgo", "prime"),
    "custom": ("G-code personalizzato", "custom"),
    "travel": ("Spostamento a vuoto", "travel"),
    "multiple": ("Stampa multipla", "print"),
}

_PARAM_RE = re.compile(r"([XYZIJEFKR])\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))", re.IGNORECASE)


def validate_printer_ip(raw_ip: Optional[str], default_ip: str = "192.168.78.50") -> str:
    """Validate printer IP address (IPv4/IPv6 literal only, preventing SSRF)."""
    candidate = (raw_ip or "").strip() or default_ip
    # Allow optional :port stripping only if pure host wasn't passed? No, reject hostnames/ports for safety.
    addr = ipaddress.ip_address(candidate)
    if addr.is_multicast or addr.is_unspecified:
        raise ValueError(f"Unsupported printer IP: {candidate}")
    if isinstance(addr, ipaddress.IPv4Address) and str(addr).startswith("169.254."):
        raise ValueError(f"Unsupported link-local IP: {candidate}")
    return str(addr)


def translate_feature(raw_feature: Optional[str]) -> Tuple[str, str]:
    """Return (Italian display label, semantic category) for a G-code ;TYPE: value."""
    raw = (raw_feature or "").strip()
    if not raw:
        return ("In stampa", "print")
    key = raw.lower()
    if key in FEATURE_MAP:
        return FEATURE_MAP[key]
    # Partial matching for slicer variants
    if "outer" in key and ("wall" in key or "perim" in key):
        return ("Parete esterna", "wall")
    if "inner" in key and ("wall" in key or "perim" in key):
        return ("Parete interna", "wall")
    if "overhang" in key:
        return ("Parete a sbalzo", "wall")
    if "iron" in key:
        return ("Stiratura (Ironing)", "ironing")
    if "interface" in key and "support" in key:
        return ("Interfaccia supporto", "support")
    if "support" in key:
        return ("Supporto", "support")
    if "bridge" in key:
        return ("Ponte", "bridge")
    if "top" in key:
        return ("Superficie superiore", "surface")
    if "bottom" in key:
        return ("Superficie inferiore", "surface")
    if "solid" in key:
        return ("Riempimento solido", "solid")
    if "infill" in key or "fill" in key:
        return ("Riempimento", "infill")
    if "skirt" in key or "brim" in key:
        return (raw.title(), "adhesion")
    return (raw, "print")


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "00:00"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


@dataclass(slots=True)
class MoveSegment:
    offset: int
    x0: float
    y0: float
    x1: float
    y1: float
    z: Optional[float]
    ftype: str
    wipe: bool
    e_per_mm: float
    de: float
    nominal_speed_mm_s: float
    obj: str
    line_width: Optional[float]
    ux: float
    uy: float


class ParseModalState:
    __slots__ = (
        "x", "y", "z", "e",
        "has_xy", "has_e",
        "abs_xyz", "abs_e",
        "feed_mm_min",
        "ftype", "wipe", "obj", "layer_z", "line_width",
    )

    def __init__(self) -> None:
        self.x: float = 0.0
        self.y: float = 0.0
        self.z: Optional[float] = None
        self.e: float = 0.0
        self.has_xy: bool = False
        self.has_e: bool = False
        self.abs_xyz: bool = True
        self.abs_e: bool = False  # Creality/Orca default M83
        self.feed_mm_min: float = 0.0
        self.ftype: str = ""
        self.wipe: bool = False
        self.obj: str = ""
        self.layer_z: Optional[float] = None
        self.line_width: Optional[float] = None

    def copy(self) -> "ParseModalState":
        c = ParseModalState()
        c.x = self.x
        c.y = self.y
        c.z = self.z
        c.e = self.e
        c.has_xy = self.has_xy
        c.has_e = self.has_e
        c.abs_xyz = self.abs_xyz
        c.abs_e = self.abs_e
        c.feed_mm_min = self.feed_mm_min
        c.ftype = self.ftype
        c.wipe = self.wipe
        c.obj = self.obj
        c.layer_z = self.layer_z
        c.line_width = self.line_width
        return c


def _subdivide_arc(
    offset: int,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    i_val: float,
    j_val: float,
    clockwise: bool,
    z_end: Optional[float],
    de: float,
    ftype: str,
    wipe: bool,
    nominal_speed_mm_s: float,
    obj: str,
    line_width: Optional[float],
    out: List[MoveSegment],
) -> None:
    """Linearize a G2/G3 circular arc into short chord MoveSegments sharing exact metadata."""
    cx = x0 + i_val
    cy = y0 + j_val
    radius = math.hypot(i_val, j_val)
    if radius < 1e-5:
        dx = x1 - x0
        dy = y1 - y0
        length = math.hypot(dx, dy)
        if length > 1e-5:
            out.append(MoveSegment(
                offset=offset, x0=x0, y0=y0, x1=x1, y1=y1, z=z_end,
                ftype=ftype, wipe=wipe, e_per_mm=de / length, de=de,
                nominal_speed_mm_s=nominal_speed_mm_s, obj=obj, line_width=line_width,
                ux=dx / length, uy=dy / length,
            ))
        return

    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    two_pi = 2.0 * math.pi
    if clockwise:
        da = (a0 - a1) % two_pi
        if da < 1e-6 and math.hypot(x1 - x0, y1 - y0) < 1e-3:
            da = two_pi
        delta = -da
    else:
        da = (a1 - a0) % two_pi
        if da < 1e-6 and math.hypot(x1 - x0, y1 - y0) < 1e-3:
            da = two_pi
        delta = da

    arc_len = radius * abs(delta)
    if arc_len < 1e-5:
        return
    e_per_mm = de / arc_len
    n_steps = max(2, min(20, int(math.ceil(abs(delta) / 0.25))))
    px, py = x0, y0
    for step in range(1, n_steps + 1):
        frac = step / n_steps
        if step == n_steps:
            nx, ny = x1, y1
        else:
            ang = a0 + delta * frac
            nx = cx + radius * math.cos(ang)
            ny = cy + radius * math.sin(ang)
        cdx = nx - px
        cdy = ny - py
        clen = math.hypot(cdx, cdy)
        if clen > 1e-6:
            out.append(MoveSegment(
                offset=offset, x0=px, y0=py, x1=nx, y1=ny, z=z_end,
                ftype=ftype, wipe=wipe, e_per_mm=e_per_mm,
                de=de * (clen / arc_len),
                nominal_speed_mm_s=nominal_speed_mm_s, obj=obj, line_width=line_width,
                ux=cdx / clen, uy=cdy / clen,
            ))
        px, py = nx, ny


def parse_gcode_chunk(
    raw_bytes: bytes,
    base_offset: int,
    modal: ParseModalState,
    skip_partial_first_line: bool = False,
) -> Tuple[List[MoveSegment], int, ParseModalState]:
    """
    Parse complete lines from `raw_bytes` starting at `base_offset`.

    Returns `(segments, parsed_end_file_offset, updated_modal)`.
    Any trailing partial line without `\\n` is left unparsed so subsequent chunk
    appends can process the full line cleanly.
    """
    segments: List[MoveSegment] = []
    pos = 0
    total_len = len(raw_bytes)

    if skip_partial_first_line and total_len > 0:
        first_nl = raw_bytes.find(b"\n")
        if first_nl == -1:
            return segments, base_offset, modal
        pos = first_nl + 1

    last_complete_pos = pos
    while pos < total_len:
        nl = raw_bytes.find(b"\n", pos)
        if nl == -1:
            break
        line_offset = base_offset + pos
        raw_line = raw_bytes[pos:nl].decode("utf-8", "replace").strip()
        pos = nl + 1
        last_complete_pos = pos

        if not raw_line:
            continue

        # Fast-path comment handling
        if raw_line[0] == ";":
            if raw_line.startswith(";TYPE:"):
                modal.ftype = raw_line[6:].strip()
            elif raw_line.startswith(";FEATURE:"):
                modal.ftype = raw_line[9:].strip()
            elif raw_line.startswith(";Z:"):
                try:
                    zval = float(raw_line[3:].strip())
                    modal.layer_z = zval
                    if modal.z is None:
                        modal.z = zval
                except ValueError:
                    pass
            elif raw_line == ";WIPE_START":
                modal.wipe = True
            elif raw_line == ";WIPE_END":
                modal.wipe = False
            elif raw_line.startswith(";WIDTH:"):
                try:
                    modal.line_width = float(raw_line[7:].strip())
                except ValueError:
                    pass
            elif raw_line.startswith("; printing object "):
                # Format: ; printing object <name> id:0 copy 0
                rest = raw_line[18:].strip()
                id_idx = rest.find(" id:")
                modal.obj = rest[:id_idx].strip() if id_idx != -1 else rest
            continue

        # Split inline comment if present
        semi = raw_line.find(";")
        code_part = raw_line[:semi].strip() if semi != -1 else raw_line
        if semi != -1:
            comment = raw_line[semi:].strip()
            if comment.startswith(";TYPE:"):
                modal.ftype = comment[6:].strip()
            elif comment.startswith(";FEATURE:"):
                modal.ftype = comment[9:].strip()

        if not code_part:
            continue

        first_space = code_part.find(" ")
        cmd = (code_part if first_space == -1 else code_part[:first_space]).upper()

        if cmd in ("G1", "G01", "G0", "G00"):
            params_str = "" if first_space == -1 else code_part[first_space + 1:]
            vals = {m.group(1).upper(): float(m.group(2)) for m in _PARAM_RE.finditer(params_str)}
            if "F" in vals:
                modal.feed_mm_min = vals["F"]

            if not modal.abs_xyz:
                # Relative motion (brief macros only)
                if modal.has_xy and ("X" in vals or "Y" in vals):
                    x0, y0 = modal.x, modal.y
                    dx = vals.get("X", 0.0)
                    dy = vals.get("Y", 0.0)
                    modal.x = x0 + dx
                    modal.y = y0 + dy
                    if "Z" in vals and modal.z is not None:
                        modal.z += vals["Z"]
                    de = 0.0
                    if "E" in vals:
                        if modal.abs_e:
                            de = (vals["E"] - modal.e) if modal.has_e else 0.0
                            modal.e = vals["E"]
                        else:
                            de = vals["E"]
                        modal.has_e = True
                    length = math.hypot(dx, dy)
                    if length > 1e-5:
                        segments.append(MoveSegment(
                            offset=line_offset, x0=x0, y0=y0, x1=modal.x, y1=modal.y,
                            z=modal.z, ftype=modal.ftype, wipe=modal.wipe,
                            e_per_mm=de / length, de=de,
                            nominal_speed_mm_s=modal.feed_mm_min / 60.0 if modal.feed_mm_min > 0 else 0.0,
                            obj=modal.obj, line_width=modal.line_width,
                            ux=dx / length, uy=dy / length,
                        ))
                else:
                    if "Z" in vals and modal.z is not None:
                        modal.z += vals["Z"]
                continue

            # Absolute XYZ coordinates (standard mode)
            if "Z" in vals:
                modal.z = vals["Z"]

            de = 0.0
            if "E" in vals:
                if modal.abs_e:
                    de = (vals["E"] - modal.e) if modal.has_e else 0.0
                    modal.e = vals["E"]
                else:
                    de = vals["E"]
                modal.has_e = True

            has_x = "X" in vals
            has_y = "Y" in vals
            if has_x or has_y:
                x0, y0 = modal.x, modal.y
                nx = vals["X"] if has_x else x0
                ny = vals["Y"] if has_y else y0
                if modal.has_xy:
                    dx = nx - x0
                    dy = ny - y0
                    length = math.hypot(dx, dy)
                    if length > 1e-5:
                        segments.append(MoveSegment(
                            offset=line_offset, x0=x0, y0=y0, x1=nx, y1=ny,
                            z=modal.z if modal.z is not None else modal.layer_z,
                            ftype=modal.ftype, wipe=modal.wipe,
                            e_per_mm=de / length, de=de,
                            nominal_speed_mm_s=modal.feed_mm_min / 60.0 if modal.feed_mm_min > 0 else 0.0,
                            obj=modal.obj, line_width=modal.line_width,
                            ux=dx / length, uy=dy / length,
                        ))
                elif has_x and has_y:
                    modal.has_xy = True
                modal.x = nx
                modal.y = ny
                if has_x and has_y:
                    modal.has_xy = True

        elif cmd in ("G2", "G02", "G3", "G03"):
            params_str = "" if first_space == -1 else code_part[first_space + 1:]
            vals = {m.group(1).upper(): float(m.group(2)) for m in _PARAM_RE.finditer(params_str)}
            if "F" in vals:
                modal.feed_mm_min = vals["F"]
            if "Z" in vals:
                modal.z = vals["Z"] if modal.abs_xyz else ((modal.z or 0.0) + vals["Z"])

            de = 0.0
            if "E" in vals:
                if modal.abs_e:
                    de = (vals["E"] - modal.e) if modal.has_e else 0.0
                    modal.e = vals["E"]
                else:
                    de = vals["E"]
                modal.has_e = True

            x0, y0 = modal.x, modal.y
            nx = (vals["X"] if "X" in vals else x0) if modal.abs_xyz else (x0 + vals.get("X", 0.0))
            ny = (vals["Y"] if "Y" in vals else y0) if modal.abs_xyz else (y0 + vals.get("Y", 0.0))
            if modal.has_xy and ("I" in vals or "J" in vals):
                _subdivide_arc(
                    offset=line_offset, x0=x0, y0=y0, x1=nx, y1=ny,
                    i_val=vals.get("I", 0.0), j_val=vals.get("J", 0.0),
                    clockwise=cmd in ("G2", "G02"),
                    z_end=modal.z if modal.z is not None else modal.layer_z,
                    de=de, ftype=modal.ftype, wipe=modal.wipe,
                    nominal_speed_mm_s=modal.feed_mm_min / 60.0 if modal.feed_mm_min > 0 else 0.0,
                    obj=modal.obj, line_width=modal.line_width, out=segments,
                )
            modal.x, modal.y = nx, ny
            if "X" in vals and "Y" in vals:
                modal.has_xy = True

        elif cmd == "G90":
            modal.abs_xyz = True
        elif cmd == "G91":
            modal.abs_xyz = False
        elif cmd == "M82":
            modal.abs_e = True
        elif cmd == "M83":
            modal.abs_e = False
        elif cmd == "G92":
            params_str = "" if first_space == -1 else code_part[first_space + 1:]
            vals = {m.group(1).upper(): float(m.group(2)) for m in _PARAM_RE.finditer(params_str)}
            if "E" in vals:
                modal.e = vals["E"]
                modal.has_e = True
        elif cmd == "EXCLUDE_OBJECT_START":
            idx = code_part.find("NAME=")
            if idx != -1:
                modal.obj = code_part[idx + 5:].strip().upper()

    return segments, base_offset + last_complete_pos, modal


def _point_to_segment_distance(lx: float, ly: float, seg: MoveSegment) -> float:
    dx = seg.x1 - seg.x0
    dy = seg.y1 - seg.y0
    l2 = dx * dx + dy * dy
    if l2 <= 1e-9:
        return math.hypot(lx - seg.x0, ly - seg.y0)
    t = ((lx - seg.x0) * dx + (ly - seg.y0) * dy) / l2
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    return math.hypot(lx - (seg.x0 + t * dx), ly - (seg.y0 + t * dy))


def match_live_segment(
    segments: List[MoveSegment],
    segment_offsets: List[int],
    file_position: int,
    live_pos: Tuple[float, float, float],
    live_velocity: float,
    live_extruder_velocity: float,
    extrude_factor: float,
    last_matched_offset: int,
    motion_dir: Optional[Tuple[float, float]] = None,
    max_candidates: int = 2000,
) -> Tuple[Optional[MoveSegment], float]:
    """
    Find the MoveSegment best matching `(live_pos, live_velocity, live_extruder_velocity)`
    up to Klipper's `file_position`.
    """
    if not segments:
        return None, 999.0

    # Restrict search to segments already parsed by virtual_sdcard (offset <= file_position + 128)
    hi = bisect.bisect_right(segment_offsets, file_position + 128)
    if hi <= 0:
        return None, 999.0
    lo_bound = max(0, hi - max_candidates)

    lx, ly, lz = live_pos
    has_live_motion = live_velocity > 0.25
    actual_ratio = (
        (live_extruder_velocity / live_velocity) / max(0.05, extrude_factor)
        if has_live_motion else 0.0
    )

    best_seg: Optional[MoveSegment] = None
    best_score = float("inf")
    best_dist = 999.0

    for idx in range(lo_bound, hi):
        seg = segments[idx]
        dist_xy = _point_to_segment_distance(lx, ly, seg)

        # Quick geometric rejection only if we already have an exact close candidate
        if dist_xy > 8.0 and best_dist < 1.0:
            continue

        score = dist_xy * 20.0 + (dist_xy * dist_xy) * 25.0

        if seg.z is not None:
            dz = abs(lz - seg.z)
            # Tolerate up to ~0.28 mm bed_mesh compensation without steep Z penalty
            score += dz * 0.8 + max(0.0, dz - 0.28) * 18.0

        if has_live_motion:
            if live_extruder_velocity > 0.01:
                if seg.e_per_mm > 0.0005:
                    ratio_diff = abs(seg.e_per_mm - actual_ratio) / max(0.02, seg.e_per_mm)
                    score += min(3.0, ratio_diff * 1.5)
                else:
                    score += 8.0
            elif abs(live_extruder_velocity) <= 0.008:
                score += 0.0 if seg.e_per_mm <= 0.0005 else 6.0
            else:
                score += 0.0 if seg.e_per_mm < 0.0 else 5.0

        if motion_dir is not None and has_live_motion:
            dot = motion_dir[0] * seg.ux + motion_dir[1] * seg.uy
            score += (1.0 - dot) * 0.35

        if last_matched_offset > 0 and seg.offset < last_matched_offset - 512:
            score += 10.0 + (last_matched_offset - seg.offset) / 4096.0

        # Very slight preference for closer to lookahead cursor ONLY to break exact ties
        score += max(0, file_position - seg.offset) / 524288.0

        if score < best_score:
            best_score = score
            best_seg = seg
            best_dist = dist_xy

    return best_seg, best_dist


class _PrinterSessionCache:
    """Per-printer rolling G-code chunk cache and motion continuity tracker."""

    def __init__(self, ip: str) -> None:
        self.ip = ip
        self.lock = threading.Lock()
        self.filament_diameter: float = DEFAULT_FILAMENT_DIAMETER_MM
        self.filament_area: float = math.pi * (DEFAULT_FILAMENT_DIAMETER_MM / 2.0) ** 2
        self.config_queried: bool = False

        self.filename: str = ""
        self.file_size: int = 0
        self.gcode_start_byte: int = 0
        self.buf_start: int = 0
        self.raw_buf: bytearray = bytearray()
        self.parsed_end: int = 0
        self.modal: ParseModalState = ParseModalState()
        self.segments: List[MoveSegment] = []
        self.segment_offsets: List[int] = []

        self.last_matched_offset: int = 0
        self.last_feature_raw: str = ""
        self.last_layer_z: Optional[float] = None
        self.last_object: str = ""
        self.last_line_width: Optional[float] = None

        self.prev_live_xy: Optional[Tuple[float, float]] = None
        self.motion_dir: Optional[Tuple[float, float]] = None

        self.last_gcode_store_check: float = 0.0
        self.last_macro_stage: Optional[Tuple[str, str, str]] = None

        self.last_poll_ts: float = 0.0
        self.last_payload: Optional[dict] = None

    def reset_job(self) -> None:
        self.filename = ""
        self.file_size = 0
        self.gcode_start_byte = 0
        self.buf_start = 0
        self.raw_buf.clear()
        self.parsed_end = 0
        self.modal = ParseModalState()
        self.segments.clear()
        self.segment_offsets.clear()
        self.last_matched_offset = 0
        self.last_feature_raw = ""
        self.last_layer_z = None
        self.last_gcode_store_check = 0.0
        self.last_macro_stage = None
        self.last_object = ""
        self.last_line_width = None
        self.prev_live_xy = None
        self.motion_dir = None


def _default_http_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 2.0) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class PrinterTelemetryTracker:
    """
    Thread-safe Moonraker + G-code lookahead telemetry tracker.

    Optionally accepts a custom `http_get(url, headers, timeout)` callable for
    deterministic offline unit testing.
    """

    def __init__(
        self,
        default_ip: str = "192.168.78.50",
        moonraker_port: int = 7125,
        min_poll_interval: float = 0.18,
        http_get: Optional[Callable[[str, Optional[Dict[str, str]], float], bytes]] = None,
    ) -> None:
        self.default_ip = validate_printer_ip(default_ip)
        self.moonraker_port = moonraker_port
        self.min_poll_interval = min_poll_interval
        self._http_get = http_get or _default_http_get
        self._sessions: Dict[str, _PrinterSessionCache] = {}
        self._global_lock = threading.Lock()

    def _get_session(self, ip: str) -> _PrinterSessionCache:
        with self._global_lock:
            sess = self._sessions.get(ip)
            if sess is None:
                sess = _PrinterSessionCache(ip)
                self._sessions[ip] = sess
            return sess

    def _query_moonraker_json(self, ip: str, path: str, timeout: float = 1.8) -> dict:
        url = f"http://{ip}:{self.moonraker_port}{path}"
        raw = self._http_get(url, None, timeout)
        return json.loads(raw.decode("utf-8", "replace"))

    def _fetch_gcode_range(self, ip: str, filename: str, start: int, end: int) -> bytes:
        quoted = urllib.parse.quote(filename.lstrip("/"), safe="/")
        url = f"http://{ip}:{self.moonraker_port}/server/files/gcodes/{quoted}"
        return self._http_get(url, {"Range": f"bytes={start}-{end}"}, 2.5)

    def _ensure_filament_diameter(self, sess: _PrinterSessionCache) -> None:
        if sess.config_queried:
            return
        sess.config_queried = True
        try:
            data = self._query_moonraker_json(sess.ip, "/printer/objects/query?configfile", timeout=1.5)
            ext_cfg = (
                data.get("result", {})
                .get("status", {})
                .get("configfile", {})
                .get("settings", {})
                .get("extruder", {})
            )
            diam = float(ext_cfg.get("filament_diameter", DEFAULT_FILAMENT_DIAMETER_MM))
            if 1.0 <= diam <= 4.0:
                sess.filament_diameter = diam
                sess.filament_area = math.pi * (diam / 2.0) ** 2
        except Exception:
            pass

    def _backfill_modal_context(self, sess: _PrinterSessionCache, filename: str, window_start: int) -> ParseModalState:
        """
        Walk backwards briefly ONLY if a mid-file slice lacked an initial `;TYPE:` or `;Z:`
        tag before its first segment (e.g. inside a >128 KB infill/wall section).
        """
        chunks: List[Tuple[int, bytes]] = []
        cursor = window_start
        for _ in range(MAX_BACKWALK_CHUNKS):
            if cursor <= 0:
                break
            prev_end = cursor - 1
            prev_start = max(0, prev_end - LOOKBACK_BYTES + 1)
            try:
                data = self._fetch_gcode_range(sess.ip, filename, prev_start, prev_end)
            except Exception:
                break
            if not data:
                break
            chunks.append((prev_start, data))
            cursor = prev_start
            if b";TYPE:" in data or b";FEATURE:" in data:
                break

        modal = ParseModalState()
        for start_off, data in reversed(chunks):
            _, _, modal = parse_gcode_chunk(
                data, start_off, modal, skip_partial_first_line=(start_off > 0)
            )
        return modal

    def _update_gcode_window(
        self,
        sess: _PrinterSessionCache,
        filename: str,
        file_size: int,
        file_position: int,
    ) -> None:
        if not filename or file_size <= 0:
            return

        # Reset if file changed or print restarted from an earlier offset
        if (
            filename != sess.filename
            or file_position + 4096 < sess.buf_start
            or file_position + 4096 < sess.last_matched_offset
        ):
            sess.reset_job()
            sess.filename = filename
            sess.file_size = file_size

        sess.file_size = file_size
        target_end = min(file_size - 1, file_position + LOOKAHEAD_BYTES)
        target_start = max(0, file_position - LOOKBACK_BYTES)

        cur_end = sess.buf_start + len(sess.raw_buf) - 1 if sess.raw_buf else -1

        # Case A: Empty buffer or large forward jump beyond current window
        if not sess.raw_buf or target_start > cur_end:
            data = self._fetch_gcode_range(sess.ip, filename, target_start, target_end)
            sess.buf_start = target_start
            sess.raw_buf = bytearray(data)
            sess.segments.clear()
            sess.segment_offsets.clear()
            modal = ParseModalState()
            segs, parsed_end, modal = parse_gcode_chunk(
                bytes(sess.raw_buf), sess.buf_start, modal, skip_partial_first_line=(target_start > 0)
            )
            # If the initial chunk has segments with unknown `;TYPE:` before the first tag, backfill once
            if target_start > 0 and any(not s.ftype for s in segs):
                back_modal = self._backfill_modal_context(sess, filename, target_start)
                if back_modal.ftype or back_modal.z is not None:
                    # Re-parse current window carrying backfilled modal context, but require an
                    # explicit XY anchor inside the window so we do not bridge across skipped lines.
                    re_modal = back_modal.copy()
                    re_modal.has_xy = False
                    segs, parsed_end, re_modal = parse_gcode_chunk(
                        bytes(sess.raw_buf), sess.buf_start, re_modal, skip_partial_first_line=True
                    )
                    modal = re_modal
            sess.segments = segs
            sess.segment_offsets = [s.offset for s in segs]
            sess.parsed_end = parsed_end
            sess.modal = modal
            return

        # Case B: `file_position` approached or passed our cached tail -> append new bytes incrementally
        if file_position + 512 > cur_end and cur_end < file_size - 1:
            fetch_start = cur_end + 1
            fetch_end = target_end
            if fetch_end >= fetch_start:
                add_data = self._fetch_gcode_range(sess.ip, filename, fetch_start, fetch_end)
                if add_data:
                    sess.raw_buf.extend(add_data)
                    # Parse from the start of the unparsed tail (keeping complete line alignment)
                    tail_rel = max(0, sess.parsed_end - sess.buf_start)
                    tail_bytes = bytes(sess.raw_buf[tail_rel:])
                    new_segs, parsed_end, sess.modal = parse_gcode_chunk(
                        tail_bytes, sess.buf_start + tail_rel, sess.modal, skip_partial_first_line=False
                    )
                    sess.segments.extend(new_segs)
                    sess.segment_offsets.extend(s.offset for s in new_segs)
                    sess.parsed_end = parsed_end

            # Trim oldest bytes if rolling buffer exceeds MAX_CACHE_BYTES
            if len(sess.raw_buf) > MAX_CACHE_BYTES:
                excess = len(sess.raw_buf) - MAX_CACHE_BYTES
                del sess.raw_buf[:excess]
                sess.buf_start += excess
                # Drop segments far behind the trimmed start
                cutoff = bisect.bisect_left(sess.segment_offsets, sess.buf_start)
                if cutoff > 0:
                    del sess.segments[:cutoff]
                    del sess.segment_offsets[:cutoff]

    def poll(self, raw_ip: Optional[str] = None, force: bool = False) -> dict:
        """
        Query Moonraker + synchronize G-code telemetry for `raw_ip` (or `default_ip`).
        Returns a structured dictionary ready for JSON serialization.
        """
        ip = validate_printer_ip(raw_ip, self.default_ip)
        sess = self._get_session(ip)

        with sess.lock:
            now = time.time()
            if not force and sess.last_payload is not None and (now - sess.last_poll_ts) < self.min_poll_interval:
                return sess.last_payload

            self._ensure_filament_diameter(sess)

            try:
                q = self._query_moonraker_json(
                    ip,
                    "/printer/objects/query?print_stats&virtual_sdcard&gcode_move&motion_report&extruder&toolhead&exclude_object&display_status&heater_bed&prtouch_v3&custom_macro",
                )
            except Exception as exc:
                payload = {
                    "ok": False,
                    "source": "moonraker",
                    "ip": ip,
                    "timestamp": now,
                    "state": "disconnected",
                    "activity_code": "disconnected",
                    "activity_label": "Stampante non raggiungibile",
                    "activity_detail": str(exc),
                    "feature_raw": "",
                    "feature_label": "",
                    "feature_category": "error",
                    "is_extruding": False,
                    "is_travel": False,
                    "is_retracting": False,
                    "is_wiping": False,
                    "nozzle_speed_mm_s": 0.0,
                    "extrude_speed_mm_s": 0.0,
                    "filament_speed_mm_s": 0.0,
                    "flow_mm3_s": 0.0,
                    "nominal_speed_mm_s": 0.0,
                    "speed_factor_pct": 100,
                    "extrude_factor_pct": 100,
                    "layer": None,
                    "total_layers": None,
                    "progress_pct": 0.0,
                    "print_duration_s": 0.0,
                    "elapsed_label": "00:00",
                    "filename": "",
                    "current_object": "",
                    "line_width_mm": None,
                    "match_confidence": "none",
                    "match_error_mm": None,
                    "file_position": 0,
                    "matched_offset": 0,
                    "nozzle_temp": None,
                    "nozzle_target": None,
                    "bed_temp": None,
                    "bed_target": None,
                    "error": str(exc),
                }
                sess.last_poll_ts = now
                sess.last_payload = payload
                return payload

            status = q.get("result", {}).get("status", {})
            ps = status.get("print_stats", {}) or {}
            vsd = status.get("virtual_sdcard", {}) or {}
            gm = status.get("gcode_move", {}) or {}
            mr = status.get("motion_report", {}) or {}
            ext = status.get("extruder", {}) or {}
            th = status.get("toolhead", {}) or {}
            eo = status.get("exclude_object", {}) or {}
            bed = status.get("heater_bed", {}) or {}
            ds = status.get("display_status", {}) or {}
            prtouch = status.get("prtouch_v3", {}) or {}
            cmacro = status.get("custom_macro", {}) or {}

            state = str(ps.get("state") or "standby").lower()
            raw_filename = str(ps.get("filename") or vsd.get("file_name") or "").strip()
            basename = raw_filename.rsplit("/", 1)[-1] if raw_filename else ""

            speed_factor = float(gm.get("speed_factor") or 1.0)
            extrude_factor = float(gm.get("extrude_factor") or 1.0)
            speed_factor_pct = max(25, min(190, int(round(speed_factor * 100))))
            extrude_factor_pct = int(round(extrude_factor * 100))

            live_vel = float(mr.get("live_velocity") or 0.0)
            live_evel = float(mr.get("live_extruder_velocity") or 0.0)
            live_pos_raw = mr.get("live_position") or gm.get("position") or [0.0, 0.0, 0.0]
            lx = float(live_pos_raw[0]) if len(live_pos_raw) > 0 else 0.0
            ly = float(live_pos_raw[1]) if len(live_pos_raw) > 1 else 0.0
            lz = float(live_pos_raw[2]) if len(live_pos_raw) > 2 else 0.0

            # Update empirical motion unit vector for crossing-path tie-breaking
            if sess.prev_live_xy is not None:
                mdx = lx - sess.prev_live_xy[0]
                mdy = ly - sess.prev_live_xy[1]
                mlen = math.hypot(mdx, mdy)
                if 0.05 <= mlen <= 40.0:
                    sess.motion_dir = (mdx / mlen, mdy / mlen)
            sess.prev_live_xy = (lx, ly)

            filament_speed = max(0.0, live_evel)
            flow_mm3_s = filament_speed * sess.filament_area
            gm_speed_mm_s = (float(gm.get("speed") or 0.0) / 60.0) * speed_factor

            is_extruding = live_evel > 0.01 and live_vel > 0.05
            is_retracting = live_evel < -0.02 and live_vel < 0.35
            is_travel = (live_vel > 1.0 and abs(live_evel) <= 0.01) or (live_vel > 5.0 and live_evel < -0.02)

            file_pos = int(vsd.get("file_position") or 0)
            file_size = int(vsd.get("file_size") or 0)
            is_vsd_active = bool(vsd.get("is_active"))
            cur_print_meta = (vsd.get("cur_print_data") or {}).get("metadata") or {}
            gcode_start_byte = int(cur_print_meta.get("gcode_start_byte") or 0)

            layer = vsd.get("layer")
            if layer is None:
                layer = (ps.get("info") or {}).get("current_layer")
            total_layers = vsd.get("layer_count") or cur_print_meta.get("layer_count") or (ps.get("info") or {}).get("total_layer")

            progress_val = vsd.get("progress")
            if progress_val is None:
                progress_val = ds.get("progress") or 0.0
            progress_pct = round(max(0.0, min(100.0, float(progress_val) * 100.0)), 1)

            print_dur = float(ps.get("print_duration") or 0.0)
            noz_temp = ext.get("temperature")
            noz_target = ext.get("target")
            bed_temp = bed.get("temperature")
            bed_target = bed.get("target")

            cur_obj = str(eo.get("current_object") or "").strip()

            # Match segment in G-code if actively printing a file
            matched_seg: Optional[MoveSegment] = None
            match_err: Optional[float] = None
            match_conf = "none"

            if state == "printing" and raw_filename and file_size > 0 and is_vsd_active:
                try:
                    self._update_gcode_window(sess, raw_filename, file_size, file_pos)
                    matched_seg, dist_err = match_live_segment(
                        sess.segments,
                        sess.segment_offsets,
                        file_pos,
                        (lx, ly, lz),
                        live_vel,
                        live_evel,
                        extrude_factor,
                        sess.last_matched_offset,
                        sess.motion_dir,
                    )
                    if matched_seg is not None:
                        match_err = round(dist_err, 3)
                        if dist_err <= 0.6:
                            match_conf = "high"
                        elif dist_err <= 2.5:
                            match_conf = "medium"
                        else:
                            match_conf = "low"
                        # Advance monotonic offset only on decent geometric match
                        if dist_err <= 2.5 and matched_seg.offset >= sess.last_matched_offset:
                            sess.last_matched_offset = matched_seg.offset
                        if matched_seg.ftype:
                            sess.last_feature_raw = matched_seg.ftype
                        if matched_seg.obj:
                            sess.last_object = matched_seg.obj
                        if matched_seg.line_width is not None:
                            sess.last_line_width = matched_seg.line_width
                except Exception:
                    pass

            if matched_seg and matched_seg.obj and not cur_obj:
                cur_obj = matched_seg.obj
            elif sess.last_object and not cur_obj:
                cur_obj = sess.last_object

            feature_raw = (matched_seg.ftype if matched_seg and matched_seg.ftype else sess.last_feature_raw)
            feature_it, feature_cat = translate_feature(feature_raw) if feature_raw else ("", "print")
            line_width = matched_seg.line_width if matched_seg and matched_seg.line_width else sess.last_line_width
            is_wiping = bool(matched_seg and matched_seg.wipe)

            # Determine clear Italian activity_code & activity_label
            homed_axes = str(th.get("homed_axes") or "").lower()
            activity_detail = ""
            if state == "paused":
                activity_code = "paused"
                activity_label = "Stampa in pausa"
                if noz_temp is not None and bed_temp is not None:
                    activity_detail = f"Ugello {float(noz_temp):.0f} °C · Piatto {float(bed_temp):.0f} °C"
            elif state == "complete":
                activity_code = "complete"
                activity_label = "Stampa completata"
            elif state == "cancelled":
                activity_code = "cancelled"
                activity_label = "Stampa annullata"
            elif state == "error":
                activity_code = "error"
                activity_label = "Errore stampa"
                activity_detail = str(ps.get("message") or "")
            elif state != "printing":
                # Manual extrusion / retraction check while idle or cooling
                if live_evel > 0.05:
                    activity_code = "manual_extrude"
                    activity_label = "Estrusione manuale"
                elif live_evel < -0.05:
                    activity_code = "manual_retract"
                    activity_label = "Ritrazione manuale"
                else:
                    activity_code = "idle"
                    activity_label = "In attesa"
            else:
                # Active printing state
                in_start_gcode = (
                    (not is_vsd_active)
                    or (gcode_start_byte > 0 and file_pos < gcode_start_byte)
                    or (feature_raw.lower() in ("custom", "") and (not layer or int(layer) == 0))
                )

                # During start preparation or early startup, inspect Klipper gcode_store macro events
                if in_start_gcode or (layer is not None and int(layer) == 0 and not is_extruding):
                    if (now - sess.last_gcode_store_check) > 1.2:
                        sess.last_gcode_store_check = now
                        try:
                            gs = self._query_moonraker_json(ip, "/server/gcode_store?count=8", timeout=0.8)
                            entries = gs.get("result", {}).get("gcode_store", [])
                            for entry in reversed(entries):
                                msg = str(entry.get("message") or "").strip()
                                if msg.startswith("B:") or msg.startswith("T0:") or msg == "ok":
                                    continue
                                ml = msg.lower()
                                if "nozzle_clean" in ml or "nozzle_clear" in ml or "clean_velocity" in ml or "box_nozzle_clean" in ml:
                                    sess.last_macro_stage = ("wipe", "Pulizia ugello", "Pulizia e rimozione residui")
                                    break
                                elif "g28_re_check" in ml or "probe at" in ml or "swap test" in ml or "[pres_" in ml:
                                    sess.last_macro_stage = ("homing", "Allineamento / Quota Z", "Controllo quota sensore Z")
                                    break
                                elif "bed_mesh" in ml or "cx_print_leveling" in ml:
                                    sess.last_macro_stage = ("homing", "Livellamento piatto (Bed Mesh)", "Mappatura piano di stampa")
                                    break
                                elif "load_material" in ml or "box_start_print" in ml or "smart_filament" in ml:
                                    sess.last_macro_stage = ("preparing", "Caricamento filamento", "Alimentazione filamento")
                                    break
                                elif "cancel_homez" in ml or "z-if_home" in ml or "z-home check" in ml or "x_axes" in ml or "if_need_home" in ml:
                                    sess.last_macro_stage = ("homing", "Allineamento assi (Homing)", "Azzeramento assi X/Y/Z")
                                    break
                                elif "do_cooling" in ml:
                                    sess.last_macro_stage = ("preparing", "Raffreddamento ugello", "Attesa stabilizzazione termica")
                                    break
                                elif "extruder_extrude" in ml or "flush_length" in ml or "material_flush" in ml:
                                    sess.last_macro_stage = ("feature", "Spurgo filamento", "Spurgo iniziale estrusore")
                                    break
                        except Exception:
                            pass

                is_leveling = bool(
                    prtouch.get("inside_calibrate")
                    or (int(prtouch.get("calibrate_progress") or 0) > 0)
                    or cmacro.get("leveling_calibration") == 1
                    or th.get("G29_flag")
                )
                bed_heating = bool(
                    bed_target and bed_temp is not None
                    and float(bed_target) > 0
                    and float(bed_temp) < float(bed_target) - 2.0
                )
                noz_heating = bool(
                    noz_target and noz_temp is not None
                    and float(noz_target) > 0
                    and float(noz_temp) < float(noz_target) - 3.0
                )

                if in_start_gcode and not is_extruding:
                    if bed_heating:
                        activity_code = "heating"
                        activity_label = "Riscaldamento piatto"
                        activity_detail = f"{float(bed_temp):.0f} / {float(bed_target):.0f} °C"
                    elif noz_heating:
                        activity_code = "heating"
                        activity_label = "Riscaldamento ugello"
                        activity_detail = f"{float(noz_temp):.0f} / {float(noz_target):.0f} °C"
                    elif is_leveling:
                        activity_code = "homing"
                        activity_label = "Livellamento piatto (Bed Mesh)"
                        activity_detail = "Calibrazione piano in corso"
                    elif sess.last_macro_stage is not None and (now - sess.last_gcode_store_check) < 15.0:
                        activity_code, activity_label, activity_detail = sess.last_macro_stage
                    elif "x" not in homed_axes or "y" not in homed_axes or "z" not in homed_axes:
                        activity_code = "homing"
                        activity_label = "Allineamento assi (Homing)"
                        activity_detail = "Azzeramento assi X/Y/Z"
                    else:
                        activity_code = "preparing"
                        activity_label = "Preparazione stampa"
                        activity_detail = "Inizializzazione stampante"
                elif (in_start_gcode or (layer is not None and int(layer) <= 1 and feature_raw.lower() in ("custom", ""))) and is_extruding:
                    activity_code = "feature"
                    activity_label = "Linea di spurgo iniziale"
                    activity_detail = "Estrusione di avvio"
                    feature_cat = "adhesion"
                elif is_wiping:
                    activity_code = "wipe"
                    activity_label = "Pulizia ugello (Wipe)"
                elif is_retracting and feature_raw:
                    activity_code = "retract"
                    activity_label = "Ritrazione filamento"
                elif is_travel and not is_extruding:
                    activity_code = "travel"
                    activity_label = "Spostamento a vuoto"
                elif feature_raw:
                    activity_code = "feature"
                    activity_label = feature_it
                elif is_extruding:
                    activity_code = "feature"
                    activity_label = "Estrusione in corso"
                    feature_cat = "print"
                elif live_vel > 0.2:
                    activity_code = "travel"
                    activity_label = "Movimento ugello"
                else:
                    activity_code = "preparing"
                    activity_label = "In esecuzione"

            extrude_speed = live_vel if is_extruding else 0.0
            nominal_spd = (
                matched_seg.nominal_speed_mm_s * speed_factor
                if matched_seg and matched_seg.nominal_speed_mm_s > 0
                else gm_speed_mm_s
            )

            payload = {
                "ok": True,
                "source": "moonraker",
                "ip": ip,
                "timestamp": now,
                "state": state,
                "activity_code": activity_code,
                "activity_label": activity_label,
                "activity_detail": activity_detail,
                "feature_raw": feature_raw,
                "feature_label": feature_it,
                "feature_category": feature_cat,
                "is_extruding": is_extruding,
                "is_travel": is_travel,
                "is_retracting": is_retracting,
                "is_wiping": is_wiping,
                "nozzle_speed_mm_s": round(live_vel, 1),
                "extrude_speed_mm_s": round(extrude_speed, 1),
                "filament_speed_mm_s": round(filament_speed, 2),
                "flow_mm3_s": round(flow_mm3_s, 2),
                "nominal_speed_mm_s": round(nominal_spd, 1),
                "speed_factor_pct": speed_factor_pct,
                "extrude_factor_pct": extrude_factor_pct,
                "layer": int(layer) if layer is not None else None,
                "total_layers": int(total_layers) if total_layers is not None else None,
                "progress_pct": progress_pct,
                "print_duration_s": round(print_dur, 1),
                "elapsed_label": format_duration(print_dur),
                "filename": basename,
                "current_object": cur_obj,
                "line_width_mm": line_width,
                "match_confidence": match_conf,
                "match_error_mm": match_err,
                "file_position": file_pos,
                "matched_offset": matched_seg.offset if matched_seg else 0,
                "nozzle_temp": round(float(noz_temp), 1) if noz_temp is not None else None,
                "nozzle_target": round(float(noz_target), 1) if noz_target is not None else None,
                "bed_temp": round(float(bed_temp), 1) if bed_temp is not None else None,
                "bed_target": round(float(bed_target), 1) if bed_target is not None else None,
                "error": None,
            }
            sess.last_poll_ts = now
            sess.last_payload = payload
            return payload


def _send_moonraker_speed(ip: str, speed_percent: int, moonraker_port: int = 7125) -> Tuple[bool, int]:
    """Send M220 speed override to Moonraker, clamped cleanly to [25, 190]."""
    clamped = max(25, min(190, int(speed_percent)))
    url = f"http://{ip}:{moonraker_port}/printer/gcode/script"
    body = json.dumps({"script": f"M220 S{clamped}"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "CrealityVisionAI-Telemetry/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            return 200 <= int(resp.status) < 300, clamped
    except Exception:
        return False, clamped


def create_standalone_server(
    host: str = "127.0.0.1",
    port: int = 8088,
    default_ip: str = "192.168.78.50",
    tracker: Optional[PrinterTelemetryTracker] = None,
    watchdog: Optional[Any] = None,
):
    """
    Create the lightweight telemetry & Stringing AI HTTP server instance
    so both production CLI execution and unit tests can bind/shutdown cleanly.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _ReuseThreadingHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    started_at = time.time()
    srv_holder: Dict[str, Any] = {"srv": None}

    tracker = tracker or PrinterTelemetryTracker(default_ip=default_ip)
    ctx_builder = None
    img_decoder = None
    try:
        from stringing_detector import StringingWatchdog, context_from_telemetry, decode_image_bytes

        if watchdog is None:
            watchdog = StringingWatchdog(default_ip=default_ip, moonraker_port=tracker.moonraker_port)
        ctx_builder = context_from_telemetry
        img_decoder = decode_image_bytes
    except Exception as exc:
        print(f"[StringingAI] Unavailable in standalone mode ({exc})")

    class Handler(BaseHTTPRequestHandler):
        def _send_cors_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")

        def _send_json(self, obj: dict, status: int = 200) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self, max_bytes: int = 12 * 1024 * 1024) -> bytes:
            try:
                clen = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                clen = 0
            if clen <= 0:
                return b""
            if clen > max_bytes:
                raise ValueError(f"Payload too large: {clen} bytes")
            return self.rfile.read(clen)

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self._send_cors_headers()
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            ip_raw = (qs.get("ip") or [None])[0]
            try:
                if parsed.path == "/printer/telemetry":
                    payload = tracker.poll(ip_raw)
                    if watchdog is not None:
                        resolved_ip = payload.get("ip") or validate_printer_ip(ip_raw, default_ip)
                        payload["stringing"] = watchdog.get_status(resolved_ip)
                    self._send_json(payload)
                elif parsed.path == "/printer/get_speed":
                    data = tracker.poll(ip_raw)
                    self._send_json({"speed": data.get("speed_factor_pct", 100)})
                elif parsed.path == "/printer/set_speed":
                    ip = validate_printer_ip(ip_raw, default_ip)
                    sp = int((qs.get("speed") or ["100"])[0])
                    ok, clamped = _send_moonraker_speed(ip, sp, moonraker_port=tracker.moonraker_port)
                    self._send_json({"status": "ok" if ok else "error", "speed": clamped})
                elif parsed.path == "/ai/stringing/status":
                    if watchdog is None:
                        self._send_json({"ok": False, "error": "Stringing AI module unavailable"}, status=503)
                        return
                    ip = validate_printer_ip(ip_raw, default_ip)
                    self._send_json(watchdog.get_status(ip))
                elif parsed.path == "/ai/stringing/config":
                    if watchdog is None:
                        self._send_json({"ok": False, "error": "Stringing AI module unavailable"}, status=503)
                        return
                    self._send_json({"ok": True, "config": watchdog.get_config()})
                elif parsed.path == "/ai/stringing/reset":
                    if watchdog is None:
                        self._send_json({"ok": False, "error": "Stringing AI module unavailable"}, status=503)
                        return
                    ip = validate_printer_ip(ip_raw, default_ip)
                    self._send_json(watchdog.reset_baseline(ip))
                elif parsed.path == "/ai/stringing/ack":
                    if watchdog is None:
                        self._send_json({"ok": False, "error": "Stringing AI module unavailable"}, status=503)
                        return
                    ip = validate_printer_ip(ip_raw, default_ip)
                    self._send_json(watchdog.acknowledge_defects(ip))
                elif parsed.path == "/ai/stringing/reset_sensitivity":
                    if watchdog is None:
                        self._send_json({"ok": False, "error": "Stringing AI module unavailable"}, status=503)
                        return
                    ip = validate_printer_ip(ip_raw, default_ip)
                    self._send_json(watchdog.reset_sensitivity(ip))
                elif parsed.path == "/ai/stringing/clear_ignored":
                    if watchdog is None:
                        self._send_json({"ok": False, "error": "Stringing AI module unavailable"}, status=503)
                        return
                    ip = validate_printer_ip(ip_raw, default_ip)
                    self._send_json(watchdog.clear_ignored_defects(ip))
                elif parsed.path == "/ai/stringing/ignore_region":
                    if watchdog is None:
                        self._send_json({"ok": False, "error": "Stringing AI module unavailable"}, status=503)
                        return
                    ip = validate_printer_ip(ip_raw, default_ip)
                    data = {}
                    if length > 0:
                        try:
                            data = json.loads(self.rfile.read(length).decode("utf-8"))
                        except Exception:
                            data = {}
                    if not data and "id" in qs:
                        data["id"] = qs["id"][0]
                    if not data and "point_x" in qs:
                        data["point_x"] = float(qs["point_x"][0])
                        data["point_y"] = float(qs.get("point_y", ["0.0"])[0])
                    if not data and "x" in qs:
                        data["x"] = float(qs["x"][0])
                        data["y"] = float(qs.get("y", ["0.0"])[0])
                        data["w"] = float(qs.get("w", ["0.12"])[0])
                        data["h"] = float(qs.get("h", ["0.12"])[0])
                    self._send_json(watchdog.ignore_defect_region(data, ip))
                elif parsed.path == "/ai/stringing/unignore_region":
                    if watchdog is None:
                        self._send_json({"ok": False, "error": "Stringing AI module unavailable"}, status=503)
                        return
                    ip = validate_printer_ip(ip_raw, default_ip)
                    data = {}
                    if length > 0:
                        try:
                            data = json.loads(self.rfile.read(length).decode("utf-8"))
                        except Exception:
                            data = {}
                    if not data and "id" in qs:
                        data["id"] = qs["id"][0]
                    self._send_json(watchdog.unignore_defect_region(data, ip))
                elif parsed.path == "/ai/stringing/snapshot":
                    if watchdog is None:
                        self._send_json({"ok": False, "error": "Stringing AI module unavailable"}, status=503)
                        return
                    ip = validate_printer_ip(ip_raw, default_ip)
                    alert_only = (qs.get("alert_only") or ["0"])[0] in ("1", "true", "yes")
                    jpeg = watchdog.get_snapshot(ip, alert_only=alert_only)
                    if not jpeg:
                        self._send_json({"ok": False, "error": "No snapshot available yet"}, status=404)
                        return
                    self._send_bytes(jpeg, "image/jpeg")
                elif parsed.path in ("/cronoprint", "/cronoprint/", "/cronoprint/history.html"):
                    html_path = os.path.abspath(os.path.join(REPO_DIR, "CronoPrint", "printer_history.html"))
                    if os.path.isfile(html_path):
                        with open(html_path, "rb") as f:
                            self._send_bytes(f.read(), "text/html; charset=utf-8")
                        return
                    self._send_json({"ok": False, "error": "Report CronoPrint non ancora generato"}, status=404)
                elif parsed.path == "/cronoprint/launch":
                    html_path = os.path.abspath(os.path.join(REPO_DIR, "CronoPrint", "printer_history.html"))
                    opened = False
                    if os.path.isfile(html_path):
                        if os.name == "nt":
                            try:
                                os.startfile(html_path)
                                opened = True
                            except Exception:
                                pass
                        if not opened:
                            try:
                                import webbrowser
                                webbrowser.open(f"file:///{html_path.replace(os.sep, '/')}")
                                opened = True
                            except Exception:
                                pass
                    self._send_json({"ok": True, "opened": opened, "html_path": html_path, "type": "html_report"})
                elif parsed.path == "/status":
                    self._send_json(
                        {
                            "ok": True,
                            "mode": "standalone_cpu",
                            "pid": os.getpid(),
                            "uptime_sec": round(max(0.0, time.time() - started_at), 1),
                            "telemetry_only": True,
                            "gpu_upscaler": False,
                            "upscale_enabled": False,
                            "stringing_available": watchdog is not None,
                            "default_ip": default_ip,
                            "stringing": watchdog.get_status(default_ip) if watchdog is not None else None,
                        }
                    )
                else:
                    self._send_json({"error": "Not found"}, status=404)
            except ValueError as ve:
                self._send_json({"ok": False, "error": str(ve)}, status=400)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            ip_raw = (qs.get("ip") or [None])[0]
            try:
                if parsed.path == "/cronoprint/launch":
                    html_path = os.path.abspath(os.path.join(REPO_DIR, "CronoPrint", "printer_history.html"))
                    opened = False
                    if os.path.isfile(html_path):
                        if os.name == "nt":
                            try:
                                os.startfile(html_path)
                                opened = True
                            except Exception:
                                pass
                        if not opened:
                            try:
                                import webbrowser
                                webbrowser.open(f"file:///{html_path.replace(os.sep, '/')}")
                                opened = True
                            except Exception:
                                pass
                    self._send_json({"ok": True, "opened": opened, "html_path": html_path, "type": "html_report"})
                elif parsed.path == "/ai/stringing/analyze":
                    if watchdog is None or img_decoder is None or ctx_builder is None:
                        self._send_json({"ok": False, "error": "Stringing AI module unavailable"}, status=503)
                        return
                    ip = validate_printer_ip(ip_raw, default_ip)
                    raw_body = self._read_body()
                    rgb = img_decoder(raw_body)
                    if rgb is None:
                        self._send_json({"ok": False, "error": "Invalid or empty image payload"}, status=400)
                        return
                    tel = tracker.poll(ip)
                    ctx = ctx_builder(tel, fallback_ip=ip)
                    assume_printing = (qs.get("assume_printing") or ["0"])[0] in ("1", "true")
                    res = watchdog.analyze_frame(rgb, ip=ip, ctx=ctx, assume_printing_if_no_ctx=assume_printing)
                    self._send_json(res)
                elif parsed.path == "/ai/stringing/config":
                    if watchdog is None:
                        self._send_json({"ok": False, "error": "Stringing AI module unavailable"}, status=503)
                        return
                    raw_body = self._read_body()
                    cfg_data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
                    self._send_json({"ok": True, "config": watchdog.update_config(cfg_data)})
                elif parsed.path == "/ai/stringing/reset":
                    if watchdog is None:
                        self._send_json({"ok": False, "error": "Stringing AI module unavailable"}, status=503)
                        return
                    ip = validate_printer_ip(ip_raw, default_ip)
                    self._send_json(watchdog.reset_baseline(ip))
                elif parsed.path == "/ai/stringing/ack":
                    if watchdog is None:
                        self._send_json({"ok": False, "error": "Stringing AI module unavailable"}, status=503)
                        return
                    ip = validate_printer_ip(ip_raw, default_ip)
                    self._send_json(watchdog.acknowledge_defects(ip))
                elif parsed.path == "/ai/stringing/reset_sensitivity":
                    if watchdog is None:
                        self._send_json({"ok": False, "error": "Stringing AI module unavailable"}, status=503)
                        return
                    ip = validate_printer_ip(ip_raw, default_ip)
                    self._send_json(watchdog.reset_sensitivity(ip))
                elif parsed.path == "/ai/stringing/clear_ignored":
                    if watchdog is None:
                        self._send_json({"ok": False, "error": "Stringing AI module unavailable"}, status=503)
                        return
                    ip = validate_printer_ip(ip_raw, default_ip)
                    self._send_json(watchdog.clear_ignored_defects(ip))
                elif parsed.path == "/ai/stringing/ignore_region":
                    if watchdog is None:
                        self._send_json({"ok": False, "error": "Stringing AI module unavailable"}, status=503)
                        return
                    ip = validate_printer_ip(ip_raw, default_ip)
                    data = {}
                    if length > 0:
                        try:
                            data = json.loads(self.rfile.read(length).decode("utf-8"))
                        except Exception:
                            data = {}
                    if not data and "id" in qs:
                        data["id"] = qs["id"][0]
                    if not data and "point_x" in qs:
                        data["point_x"] = float(qs["point_x"][0])
                        data["point_y"] = float(qs.get("point_y", ["0.0"])[0])
                    if not data and "x" in qs:
                        data["x"] = float(qs["x"][0])
                        data["y"] = float(qs.get("y", ["0.0"])[0])
                        data["w"] = float(qs.get("w", ["0.12"])[0])
                        data["h"] = float(qs.get("h", ["0.12"])[0])
                    self._send_json(watchdog.ignore_defect_region(data, ip))
                elif parsed.path == "/ai/stringing/unignore_region":
                    if watchdog is None:
                        self._send_json({"ok": False, "error": "Stringing AI module unavailable"}, status=503)
                        return
                    ip = validate_printer_ip(ip_raw, default_ip)
                    data = {}
                    if length > 0:
                        try:
                            data = json.loads(self.rfile.read(length).decode("utf-8"))
                        except Exception:
                            data = {}
                    if not data and "id" in qs:
                        data["id"] = qs["id"][0]
                    self._send_json(watchdog.unignore_defect_region(data, ip))
                elif parsed.path == "/printer/pause":
                    if watchdog is None:
                        self._send_json({"ok": False, "error": "Pause helper unavailable"}, status=503)
                        return
                    ip = validate_printer_ip(ip_raw, default_ip)
                    self._send_json(watchdog.execute_pause(ip))
                elif parsed.path == "/server/stop":
                    self._send_json({"ok": True, "stopping": True, "pid": os.getpid(), "mode": "standalone_cpu"})
                    srv_obj = srv_holder.get("srv")
                    if srv_obj is not None:
                        t = threading.Timer(0.12, srv_obj.shutdown)
                        t.daemon = True
                        t.start()
                else:
                    self._send_json({"error": "Not found"}, status=404)
            except ValueError as ve:
                self._send_json({"ok": False, "error": str(ve)}, status=400)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)

        def log_message(self, format: str, *args) -> None:
            pass

    srv = _ReuseThreadingHTTPServer((host, port), Handler)
    srv_holder["srv"] = srv
    return srv


def run_standalone_server(
    host: str = "127.0.0.1",
    port: int = 8088,
    default_ip: str = "192.168.78.50",
    tracker: Optional[PrinterTelemetryTracker] = None,
    watchdog: Optional[Any] = None,
) -> None:
    srv = create_standalone_server(host=host, port=port, default_ip=default_ip, tracker=tracker, watchdog=watchdog)
    print(f"[Telemetry] Standalone server (Telemetry + Stringing AI) running on http://{host}:{port}")

    def _on_creality_close():
        try:
            threading.Thread(target=srv.shutdown, name="OrphanShutdown", daemon=True).start()
        except Exception:
            pass

    orphan_watcher = cp_lifecycle.start_orphan_guard(_on_creality_close, close_grace=8.0)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if orphan_watcher:
            orphan_watcher.stop()
        srv.server_close()


if __name__ == "__main__":
    run_standalone_server()
