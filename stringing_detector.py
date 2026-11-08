"""
Creality Vision AI - Real-Time Multi-Scale Stringing & Loose-Filament Detector.

Uses physics-grounded computer vision tuned specifically for 3D printer enclosure
cameras (Creality SparkX i7 / K1 / K1 Max):
  1. Multi-channel Luma + CIE Lab chroma normalization with gentle dark-filament
     shadow recovery.
  2. Moving toolhead shroud exclusion (cohesive high-motion block rejection) and
     exposure/LED-toggle jump guard.
  3. Clean scene baseline suppression (`bg_ridge`) frozen during anomalies so
     static printer hardware and PEI grid lines do not trigger false alarms.
  4. Symmetrical 8-orientation dual-scale filament ridge filters (invariant to
     step edges and smooth illumination ramps via side-shoulder symmetry and
     halo-asymmetry subtraction).
  5. Spatial tile morphology & anti-infill scoring (combines thin-component count,
     skeleton extent, orientation dispersion / multi-strand gap bridging, ridge-to-
     Sobel purity, and subtle fan-flutter cues).
  6. Printer telemetry fusion & temporal leaky-integrator confirmation state
     machine for zero-false-positive automatic printing pauses.
"""

from __future__ import annotations

import math
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np


ANALYZE_WIDTH = 448

# Preset sensitivity profiles: (threshold, confirm_seconds)
# Tuned specifically so textured PEI bed glare / minor shimmer never triggers
# false positives while genuine growing stringing and spaghetti do.
SENSITIVITY_PRESETS: Dict[str, Tuple[float, float]] = {
    "ultra_low": (0.85, 9.0),  # Post-recalibration step-down (~23% sensibilità)
    "low": (0.76, 7.0),        # Conservativa (~39% sensibilità, zero falsi allarmi)
    "balanced": (0.65, 5.2),   # Bilanciata (~59% sensibilità, default)
    "high": (0.52, 3.5),       # Sensibile (~83% sensibilità)
}
SENSITIVITY_ORDER = ("high", "balanced", "low", "ultra_low")


def threshold_to_sensitivity_pct(thr: float) -> int:
    """
    Map internal detection threshold (0.45..0.92) to a human-readable sensitivity % (90%..10%).
    Lower threshold = higher sensitivity.
    """
    pct = (0.92 - float(thr)) / (0.92 - 0.48) * 80.0 + 10.0
    return max(5, min(95, int(round(pct))))
_BASE_ENV_KERNEL = np.ones((5, 5), dtype=np.uint8)
_BACKOFF_ENV_KERNEL = np.ones((7, 7), dtype=np.uint8)
_CAND_DILATE_KERNEL = np.ones((11, 11), dtype=np.uint8)


def _build_oriented_kernels(length: int = 5, n_angles: int = 6) -> Tuple[List[np.ndarray], List[Tuple[float, float]]]:
    """
    Build N oriented unit tangent smoothing kernels (angles k*pi/N) and their
    perpendicular unit normal vectors.
    """
    half = length // 2
    kernels: List[np.ndarray] = []
    normals: List[Tuple[float, float]] = []
    for k in range(n_angles):
        theta = k * (math.pi / float(n_angles))
        ux, uy = math.cos(theta), math.sin(theta)
        ker = np.zeros((length, length), dtype=np.float32)
        for t in np.linspace(-half, half, num=length * 2 + 1):
            px = int(round(half + ux * t))
            py = int(round(half + uy * t))
            if 0 <= px < length and 0 <= py < length:
                w = math.exp(-0.5 * (t / max(1.0, half * 0.7)) ** 2)
                ker[py, px] = max(ker[py, px], w)
        s = float(ker.sum())
        if s > 0:
            ker /= s
        kernels.append(ker)
        nx, ny = -uy, ux
        normals.append((nx, ny))
    return kernels, normals


_N_ANGLES = 6
_ORIENTED_KERNELS, _ORIENTED_NORMALS = _build_oriented_kernels(5, _N_ANGLES)
_THIN_ERODE_KERNEL = np.ones((7, 7), dtype=np.uint8)
_THIN_DILATE_KERNEL = np.ones((5, 5), dtype=np.uint8)
_HEAD_ERODE_KERNEL = np.ones((9, 9), dtype=np.uint8)
_HEAD_DILATE_KERNEL = np.ones((17, 17), dtype=np.uint8)
_EDGE_RING_KERNEL = np.ones((3, 3), dtype=np.uint8)
_PAD = 4


def _shadow_lift_lut() -> np.ndarray:
    x = np.arange(256, dtype=np.float32)
    t = x / 255.0
    lifted = x + (255.0 - x) * 0.28 * (1.0 - t) ** 2.2
    return np.clip(lifted, 0, 255).astype(np.uint8)


_SHADOW_LUT = _shadow_lift_lut()


@dataclass
class StringingConfig:
    enabled: bool = True
    auto_pause: bool = True
    sensitivity: str = "balanced"  # "low" | "balanced" | "high"
    custom_threshold: Optional[float] = None
    confirm_seconds: Optional[float] = None
    cooldown_seconds: float = 30.0
    calibration_frames: int = 8

    def effective_sensitivity(self, backoff_level: int = 0) -> str:
        base = self.sensitivity if self.sensitivity in SENSITIVITY_PRESETS else "balanced"
        try:
            idx = SENSITIVITY_ORDER.index(base)
        except ValueError:
            idx = 1
        stepped = min(len(SENSITIVITY_ORDER) - 1, idx + max(0, int(backoff_level)))
        return SENSITIVITY_ORDER[stepped]

    def resolved_threshold(self, backoff_level: int = 0) -> float:
        if self.custom_threshold is not None:
            bump = 0.08 * max(0, int(backoff_level))
            return max(0.30, min(0.92, float(self.custom_threshold) + bump))
        eff = self.effective_sensitivity(backoff_level)
        return SENSITIVITY_PRESETS.get(eff, SENSITIVITY_PRESETS["balanced"])[0]

    def resolved_confirm_seconds(self, backoff_level: int = 0) -> float:
        if self.confirm_seconds is not None:
            bump = 1.5 * max(0, int(backoff_level))
            return max(1.5, min(15.0, float(self.confirm_seconds) + bump))
        eff = self.effective_sensitivity(backoff_level)
        return SENSITIVITY_PRESETS.get(eff, SENSITIVITY_PRESETS["balanced"])[1]

    def to_dict(self) -> Dict[str, Any]:
        base_thr = self.resolved_threshold(0)
        return {
            "enabled": self.enabled,
            "auto_pause": self.auto_pause,
            "sensitivity": self.sensitivity,
            "sensitivity_pct": threshold_to_sensitivity_pct(base_thr),
            "threshold": round(base_thr, 2),
            "confirm_seconds": round(self.resolved_confirm_seconds(0), 1),
            "cooldown_seconds": round(self.cooldown_seconds, 1),
        }


@dataclass
class PrinterContext:
    ip: str = "192.168.78.50"
    state: str = "unknown"          # "printing" | "paused" | "standby" | "complete" | ...
    activity_code: str = "unknown"  # "feature" | "travel" | "homing" | "heating" | "preparing" | ...
    feature_category: str = ""      # "wall_outer" | "infill" | "support" | "prime" | ...
    feature_label: str = ""
    layer: Optional[int] = None
    total_layers: Optional[int] = None
    nozzle_speed_mm_s: float = 0.0
    is_extruding: bool = False


@dataclass
class _IpSession:
    ip: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    prev_gray: Optional[np.ndarray] = None
    prev_mean_luma: Optional[float] = None
    bg_ridge: Optional[np.ndarray] = None
    bg_envelope: Optional[np.ndarray] = None
    bg_macro_lab: Optional[np.ndarray] = None
    backoff_level: int = 0
    calibration_left: int = 8
    exposure_skip_left: int = 0
    roi_mask_shape: Optional[Tuple[int, int]] = None
    roi_mask: Optional[np.ndarray] = None
    ema_score: float = 0.0
    last_raw_score: float = 0.0
    streak_sec: float = 0.0
    last_ts: float = 0.0
    last_eval_ts: float = 0.0
    frames_analyzed: int = 0
    avg_infer_ms: float = 0.0
    ai_state: str = "idle"
    cooldown_until: float = 0.0
    paused_by_ai: bool = False
    last_printer_state: str = ""
    last_regions: List[Dict[str, Any]] = field(default_factory=list)
    ignored_defects: List[Dict[str, Any]] = field(default_factory=list)
    alert_info: Optional[Dict[str, Any]] = None
    alert_snapshot_jpeg: Optional[bytes] = None
    latest_annotated_jpeg: Optional[bytes] = None
    last_result: Dict[str, Any] = field(default_factory=dict)


def _compute_roi_mask(h: int, w: int) -> np.ndarray:
    """
    Exclude outer enclosure edges where gantries/belts/bed metallic bezel sit
    and corner regions occupied by Creality Print native UI overlays (#aiBadge, .videoStatus).
    """
    mask = np.ones((h, w), dtype=np.float32)
    top = max(14, int(round(h * 0.12)))
    bot = max(10, int(round(h * 0.08)))
    side = max(8, int(round(w * 0.04)))
    mask[:top, :] = 0.0
    mask[h - bot :, :] = 0.0
    mask[:, :side] = 0.0
    mask[:, w - side :] = 0.0
    # Top-right overlay corner (#aiBadge + Creality icons) & top-left status pill
    mask[: int(round(h * 0.16)), int(round(w * 0.70)) :] = 0.0
    mask[: int(round(h * 0.14)), : int(round(w * 0.28))] = 0.0
    return mask


def _extract_moving_head_mask(gray: np.ndarray, prev_gray: Optional[np.ndarray]) -> Tuple[np.ndarray, float]:
    """
    Detect cohesive solid moving blocks (extruder carriage / fan duct / gantry)
    using morphological opening/erosion BEFORE grouping so thin wispy filaments
    or sudden spaghetti coils are never masked out as a solid toolhead.
    Returns (exclusion_bool_mask, mean_abs_diff).
    """
    h, w = gray.shape
    if prev_gray is None or prev_gray.shape != gray.shape:
        return np.zeros((h, w), dtype=bool), 0.0

    diff = cv2.absdiff(gray, prev_gray)
    mean_diff = float(diff.mean())
    motion = (diff > 24).astype(np.uint8)
    # Only solid objects (>= 9x9 filled interior) survive erosion; thin 1-4px strings vanish
    solid_motion = cv2.erode(motion, _HEAD_ERODE_KERNEL)
    if not solid_motion.any():
        return np.zeros((h, w), dtype=bool), mean_diff

    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(solid_motion, connectivity=8)
    head_bin = np.zeros((h, w), dtype=np.uint8)
    min_core_area = max(90, int(0.0025 * h * w))
    for idx in range(1, n_labels):
        area = int(stats[idx, cv2.CC_STAT_AREA])
        bw = max(1, int(stats[idx, cv2.CC_STAT_WIDTH]))
        bh = max(1, int(stats[idx, cv2.CC_STAT_HEIGHT]))
        solidity = area / float(bw * bh)
        if area >= min_core_area and solidity >= 0.35:
            head_bin[labels == idx] = 1

    if head_bin.any():
        head_bin = cv2.dilate(head_bin, _HEAD_DILATE_KERNEL, iterations=1)
    return head_bin.astype(bool), mean_diff


def _compute_symmetrical_filament_map(rgb: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Compute the 8-orientation symmetrical ridge response across luminance + CIE Lab
    chroma channels alongside the raw luminance gradient magnitude.

    Returns (filament_map, best_orient_idx, sobel_mag, luma_for_motion) inside
    the unpadded [H, W] grid.
    """
    h, w = rgb.shape[:2]
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2Lab)
    l_chan, a_chan, b_chan = cv2.split(lab)

    # Soft dark lift + gentle local contrast helps black PLA filaments stand out
    l_lifted = cv2.LUT(l_chan, _SHADOW_LUT)
    clahe = cv2.createCLAHE(clipLimit=1.8, tileGridSize=(8, 8))
    l_clahe = clahe.apply(l_lifted)
    l_enh = cv2.addWeighted(l_lifted, 0.55, l_clahe, 0.45, 0.0).astype(np.float32)

    a_s = cv2.GaussianBlur(a_chan.astype(np.float32) - 128.0, (3, 3), 0.7)
    b_s = cv2.GaussianBlur(b_chan.astype(np.float32) - 128.0, (3, 3), 0.7)

    # Sobel edge magnitude captures step-edge density (solid printed objects/supports)
    gx = cv2.Sobel(l_enh, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(l_enh, cv2.CV_32F, 0, 1, ksize=3)
    sobel_mag = cv2.magnitude(gx, gy) * 0.25

    ch, cw = h - 2 * _PAD, w - 2 * _PAD
    ridge_stack = np.zeros((_N_ANGLES, ch, cw), dtype=np.float32)
    c_a = a_s[_PAD : h - _PAD, _PAD : w - _PAD]
    c_b = b_s[_PAD : h - _PAD, _PAD : w - _PAD]

    # Scales (2, 3) target 1-3.5px filament strings while flat-topped >=6px trunks/supports score near zero
    for k in range(_N_ANGLES):
        ker = _ORIENTED_KERNELS[k]
        nx, ny = _ORIENTED_NORMALS[k]
        s_l = cv2.filter2D(l_enh, cv2.CV_32F, ker, borderType=cv2.BORDER_REFLECT)
        c_l = s_l[_PAD : h - _PAD, _PAD : w - _PAD]

        r_by_scale: List[np.ndarray] = []
        for dist in (2, 3):
            ox = int(round(nx * dist))
            oy = int(round(ny * dist))
            if ox == 0 and oy == 0:
                ox = 1 if abs(nx) >= abs(ny) else 0
                oy = 0 if abs(nx) >= abs(ny) else 1

            lp_l = s_l[_PAD + oy : h - _PAD + oy, _PAD + ox : w - _PAD + ox]
            rm_l = s_l[_PAD - oy : h - _PAD - oy, _PAD - ox : w - _PAD - ox]
            asym = 0.28 * np.abs(lp_l - rm_l)

            r_pos = np.maximum(0.0, np.minimum(c_l - lp_l, c_l - rm_l) - asym)
            r_neg = np.maximum(0.0, np.minimum(lp_l - c_l, rm_l - c_l) - asym)
            r_luma = np.maximum(r_pos, r_neg)

            # Chroma ridge using pre-blurred CIE a/b channels
            lp_a = a_s[_PAD + oy : h - _PAD + oy, _PAD + ox : w - _PAD + ox]
            rm_a = a_s[_PAD - oy : h - _PAD - oy, _PAD - ox : w - _PAD - ox]
            lp_b = b_s[_PAD + oy : h - _PAD + oy, _PAD + ox : w - _PAD + ox]
            rm_b = b_s[_PAD - oy : h - _PAD - oy, _PAD - ox : w - _PAD - ox]

            v1_a, v1_b = c_a - lp_a, c_b - lp_b
            v2_a, v2_b = c_a - rm_a, c_b - rm_b
            sum_a, sum_b = v1_a + v2_a, v1_b + v2_b
            norm_s = np.sqrt(sum_a * sum_a + sum_b * sum_b) + 1e-5
            ua, ub = sum_a / norm_s, sum_b / norm_s
            proj1 = v1_a * ua + v1_b * ub
            proj2 = v2_a * ua + v2_b * ub
            c_asym = 0.25 * np.hypot(lp_a - rm_a, lp_b - rm_b)
            r_chr = 0.72 * np.maximum(0.0, np.minimum(proj1, proj2) - c_asym)

            r_by_scale.append(np.maximum(r_luma, r_chr))

        # Suppress flat-topped wide structures (>=5px solid branches): require genuine sharp
        # ridge spine at d=2 alongside any wider d=3 response
        r_d2, r_d3 = r_by_scale[0], r_by_scale[1]
        ridge_stack[k] = np.maximum(r_d2, np.minimum(r_d3, r_d2 * 1.45 + 4.0))

    r_max = ridge_stack.max(axis=0)
    r_med = np.partition(ridge_stack, _N_ANGLES // 2, axis=0)[_N_ANGLES // 2]
    best_orient = np.argmax(ridge_stack, axis=0).astype(np.uint8)

    core_map = np.maximum(0.0, r_max - 0.76 * r_med)
    full_map = np.zeros((h, w), dtype=np.float32)
    full_map[_PAD : h - _PAD, _PAD : w - _PAD] = core_map

    full_orient = np.zeros((h, w), dtype=np.uint8)
    full_orient[_PAD : h - _PAD, _PAD : w - _PAD] = best_orient

    return full_map, full_orient, sobel_mag, l_chan


def _matches_ignored(box: Dict[str, Any], ignored_list: List[Dict[str, Any]], iou_thr: float = 0.14) -> Optional[Dict[str, Any]]:
    """
    Check whether candidate box matches an acknowledged/ignored defect footprint
    by spatial overlap (IoU) or bounding-box / centroid proximity.
    """
    if not ignored_list:
        return None
    bx = float(box["x"])
    by = float(box["y"])
    bw = float(box["w"])
    bh = float(box["h"])
    bx2 = bx + bw
    by2 = by + bh
    bcx = bx + bw * 0.5
    bcy = by + bh * 0.5

    for ign in ignored_list:
        ix = float(ign["x"])
        iy = float(ign["y"])
        iw_box = float(ign["w"])
        ih_box = float(ign["h"])
        ix2 = ix + iw_box
        iy2 = iy + ih_box

        # Intersection over union
        inter_x1 = max(bx, ix)
        inter_y1 = max(by, iy)
        inter_x2 = min(bx2, ix2)
        inter_y2 = min(by2, iy2)
        inter_w = max(0.0, inter_x2 - inter_x1)
        inter_h = max(0.0, inter_y2 - inter_y1)
        inter_area = inter_w * inter_h
        union_area = (bw * bh) + (iw_box * ih_box) - inter_area
        if union_area > 0 and (inter_area / union_area) >= iou_thr:
            return ign

        # Check if candidate center falls inside the ignored box (with margin for camera flutter)
        pad_x = max(0.035, iw_box * 0.20)
        pad_y = max(0.035, ih_box * 0.20)
        if (ix - pad_x) <= bcx <= (ix2 + pad_x) and (iy - pad_y) <= bcy <= (iy2 + pad_y):
            return ign

        # Centroid distance tolerance
        icx = float(ign.get("cx", ix + iw_box * 0.5))
        icy = float(ign.get("cy", iy + ih_box * 0.5))
        dist = math.hypot(bcx - icx, bcy - icy)
        tol_rad = max(0.065, float(ign.get("radius", 0.08)) * 1.05)
        if dist <= tol_rad:
            return ign

    return None


def _merge_boxes(boxes: List[Dict[str, Any]], iou_thr: float = 0.22) -> List[Dict[str, Any]]:
    """Greedily merge overlapping spatial candidate tiles into clean HUD boxes, keeping active and ignored separate."""
    if not boxes:
        return []

    active = [b for b in boxes if not b.get("ignored")]
    ignored = [b for b in boxes if b.get("ignored")]

    def _merge_group(group: List[Dict[str, Any]], max_out: int = 6) -> List[Dict[str, Any]]:
        if not group:
            return []
        ordered = sorted(group, key=lambda b: b.get("score", 0.0), reverse=True)
        merged: List[Dict[str, Any]] = []
        for box in ordered:
            bx1, by1, bx2, by2 = box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"]
            absorbed = False
            for m in merged:
                mx1, my1, mx2, my2 = m["x"], m["y"], m["x"] + m["w"], m["y"] + m["h"]
                ix1, iy1 = max(bx1, mx1), max(by1, my1)
                ix2, iy2 = min(bx2, mx2), min(by2, my2)
                iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
                inter = iw * ih
                union = (bx2 - bx1) * (by2 - by1) + (mx2 - mx1) * (my2 - my1) - inter
                if union > 0 and (inter / union) >= iou_thr:
                    nx1, ny1 = min(bx1, mx1), min(by1, my1)
                    nx2, ny2 = max(bx2, mx2), max(by2, my2)
                    m["x"], m["y"] = round(nx1, 4), round(ny1, 4)
                    m["w"], m["h"] = round(nx2 - nx1, 4), round(ny2 - ny1, 4)
                    m["score"] = round(max(m["score"], box["score"]), 3)
                    m["strands"] = max(m.get("strands", 0), box.get("strands", 0))
                    if box.get("ignored"):
                        m["ignored"] = True
                        if box.get("defect_id"):
                            m["defect_id"] = box["defect_id"]
                    absorbed = True
                    break
            if not absorbed and len(merged) < max_out:
                merged.append(dict(box))
        return merged

    return _merge_group(active, max_out=6) + _merge_group(ignored, max_out=6)


def _render_evidence_jpeg(rgb: np.ndarray, regions: List[Dict[str, Any]], score: float, label: str) -> bytes:
    """Render annotated BGR JPEG snapshot for audit inspection inside Creality Print."""
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    for r in regions:
        x1 = max(0, int(round(r["x"] * w)))
        y1 = max(0, int(round(r["y"] * h)))
        x2 = min(w - 1, int(round((r["x"] + r["w"]) * w)))
        y2 = min(h - 1, int(round((r["y"] + r["h"]) * h)))
        if r.get("ignored"):
            col = (140, 140, 140)
            cv2.rectangle(bgr, (x1, y1), (x2, y2), col, 1)
            tag = "Letto - Ignorato"
            cv2.putText(bgr, tag, (x1 + 4, max(14, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.40, col, 1, cv2.LINE_AA)
        else:
            col = (60, 68, 239) if r["score"] >= 0.55 else (36, 191, 251)
            cv2.rectangle(bgr, (x1, y1), (x2, y2), col, 2)
            tag = f"Stringing {int(round(r['score'] * 100))}%"
            cv2.putText(bgr, tag, (x1 + 4, max(16, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.48, col, 1, cv2.LINE_AA)

    banner = f"AI Vision | {label} | Score {int(round(score * 100))}%"
    cv2.rectangle(bgr, (0, h - 26), (w, h), (18, 18, 22), -1)
    cv2.putText(bgr, banner, (10, h - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (235, 235, 240), 1, cv2.LINE_AA)
    ok, enc = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return enc.tobytes() if ok else b""


class StringingWatchdog:
    """
    Thread-safe, per-IP stringing & loose-filament analyzer with Moonraker pause hook.
    """

    def __init__(
        self,
        default_ip: str = "192.168.78.50",
        moonraker_port: int = 7125,
        http_post: Optional[Callable[[str, dict, float], Tuple[int, bytes]]] = None,
        http_get: Optional[Callable[[str, float], bytes]] = None,
    ):
        self.default_ip = default_ip
        self.moonraker_port = moonraker_port
        self.config = StringingConfig()
        self._cfg_lock = threading.Lock()
        self._sessions_lock = threading.Lock()
        self._sessions: Dict[str, _IpSession] = {}
        self._http_post = http_post or self._default_http_post
        self._http_get = http_get or self._default_http_get

    @staticmethod
    def _default_http_post(url: str, payload: dict, timeout: float) -> Tuple[int, bytes]:
        import json as _json

        data = _json.dumps(payload).encode("utf-8") if payload else b""
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "CrealityVisionAI-Guard/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()

    @staticmethod
    def _default_http_get(url: str, timeout: float) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "CrealityVisionAI-Guard/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def _get_session(self, ip: Optional[str]) -> _IpSession:
        resolved = ip or self.default_ip
        with self._sessions_lock:
            sess = self._sessions.get(resolved)
            if sess is None:
                sess = _IpSession(ip=resolved)
                self._sessions[resolved] = sess
            return sess

    def get_config(self) -> Dict[str, Any]:
        with self._cfg_lock:
            return self.config.to_dict()

    def update_config(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self._cfg_lock:
            if "enabled" in data:
                self.config.enabled = bool(data["enabled"])
                if not self.config.enabled:
                    with self._sessions_lock:
                        for s in self._sessions.values():
                            with s.lock:
                                s.ai_state = "disabled"
                                s.streak_sec = 0.0
                                s.ema_score = 0.0
                                s.last_regions = []
            if "auto_pause" in data:
                self.config.auto_pause = bool(data["auto_pause"])
            if "sensitivity" in data and str(data["sensitivity"]) in SENSITIVITY_PRESETS:
                new_sens = str(data["sensitivity"])
                sens_changed = new_sens != self.config.sensitivity
                self.config.sensitivity = new_sens
                self.config.custom_threshold = None
                self.config.confirm_seconds = None
                if sens_changed:
                    with self._sessions_lock:
                        for s in self._sessions.values():
                            s.backoff_level = 0
            if "threshold" in data and data["threshold"] is not None:
                self.config.custom_threshold = max(0.25, min(0.92, float(data["threshold"])))
            if "confirm_seconds" in data and data["confirm_seconds"] is not None:
                self.config.confirm_seconds = max(1.5, min(15.0, float(data["confirm_seconds"])))
            if "cooldown_seconds" in data and data["cooldown_seconds"] is not None:
                self.config.cooldown_seconds = max(5.0, min(180.0, float(data["cooldown_seconds"])))
            return self.config.to_dict()

    def reset_baseline(self, ip: Optional[str] = None, clear_alert: bool = True, step_down: bool = False, now: Optional[float] = None) -> Dict[str, Any]:
        sess = self._get_session(ip)
        cur_time = time.time() if now is None else float(now)
        with self._cfg_lock:
            cfg = StringingConfig(**self.config.__dict__)
        with sess.lock:
            cur_thr = cfg.resolved_threshold(sess.backoff_level)
            had_detection = (
                sess.paused_by_ai
                or (sess.alert_info is not None)
                or (sess.ai_state in ("warning", "paused_by_ai", "alert_only"))
                or (sess.streak_sec > 0.25)
                or (sess.ema_score >= cur_thr * 0.55)
                or (sess.last_raw_score >= cur_thr * 0.55)
                or bool(sess.last_regions)
            )
            if step_down and had_detection:
                sess.backoff_level = min(2, sess.backoff_level + 1)

            sess.bg_ridge = None
            sess.bg_envelope = None
            sess.bg_macro_lab = None
            sess.prev_gray = None
            sess.calibration_left = max(6, cfg.calibration_frames)
            sess.ema_score = 0.0
            sess.last_raw_score = 0.0
            sess.streak_sec = 0.0
            sess.last_regions = []
            sess.paused_by_ai = False
            sess.cooldown_until = cur_time + 6.0
            sess.ai_state = "calibrating"
            if clear_alert:
                sess.alert_info = None
                sess.alert_snapshot_jpeg = None
            return self._build_status_locked(sess, cfg, cur_time=cur_time)

    def acknowledge_defects(self, ip: Optional[str] = None, now: Optional[float] = None) -> Dict[str, Any]:
        """
        Mark all currently active stringing regions as acknowledged/ignored defects.
        Subsequent detections within these regions will be suppressed from scoring
        WITHOUT lowering the sensitivity of the rest of the build plate.
        """
        sess = self._get_session(ip)
        cur_time = time.time() if now is None else float(now)
        with self._cfg_lock:
            cfg = StringingConfig(**self.config.__dict__)
        with sess.lock:
            candidates: List[Dict[str, Any]] = []
            for r in sess.last_regions:
                if not r.get("ignored"):
                    candidates.append(r)
            if not candidates and sess.alert_info and sess.alert_info.get("regions"):
                for r in sess.alert_info["regions"]:
                    if not r.get("ignored"):
                        candidates.append(r)

            for cand in candidates:
                already_covered = False
                for existing in sess.ignored_defects:
                    e_x1 = max(float(cand["x"]), float(existing["x"]))
                    e_y1 = max(float(cand["y"]), float(existing["y"]))
                    e_x2 = min(float(cand["x"]) + float(cand["w"]), float(existing["x"]) + float(existing["w"]))
                    e_y2 = min(float(cand["y"]) + float(cand["h"]), float(existing["y"]) + float(existing["h"]))
                    iw = max(0.0, e_x2 - e_x1)
                    ih = max(0.0, e_y2 - e_y1)
                    inter = iw * ih
                    union = (float(cand["w"]) * float(cand["h"])) + (float(existing["w"]) * float(existing["h"])) - inter
                    if union > 0 and (inter / union) >= 0.60:
                        already_covered = True
                        break

                if not already_covered:
                    idx = len(sess.ignored_defects) + 1
                    cw = float(cand.get("w", 0.12))
                    ch = float(cand.get("h", 0.12))
                    cx = float(cand.get("x", 0.0)) + cw * 0.5
                    cy = float(cand.get("y", 0.0)) + ch * 0.5
                    sess.ignored_defects.append(
                        {
                            "id": f"DEF-{idx}",
                            "x": round(float(cand.get("x", 0.0)), 4),
                            "y": round(float(cand.get("y", 0.0)), 4),
                            "w": round(cw, 4),
                            "h": round(ch, 4),
                            "cx": round(cx, 4),
                            "cy": round(cy, 4),
                            "radius": round(math.hypot(cw, ch) * 0.5, 4),
                            "score": round(float(cand.get("score", 0.0)), 3),
                            "strands": int(cand.get("strands", 1)),
                            "acknowledged_at": round(cur_time, 2),
                        }
                    )

            for r in sess.last_regions:
                r["ignored"] = True

            sess.streak_sec = 0.0
            sess.ema_score = 0.0
            sess.last_raw_score = 0.0
            sess.paused_by_ai = False
            sess.alert_info = None
            sess.alert_snapshot_jpeg = None
            sess.cooldown_until = cur_time + 4.0
            sess.ai_state = "monitoring"
            return self._build_status_locked(sess, cfg, cur_time=cur_time)

    def reset_sensitivity(self, ip: Optional[str] = None) -> Dict[str, Any]:
        """
        Restore original sensitivity preset by clearing backoff level back to 0.
        """
        sess = self._get_session(ip)
        with self._cfg_lock:
            cfg = StringingConfig(**self.config.__dict__)
        with sess.lock:
            sess.backoff_level = 0
            return self._build_status_locked(sess, cfg)

    def clear_ignored_defects(self, ip: Optional[str] = None) -> Dict[str, Any]:
        """
        Clear the list of acknowledged/ignored defect zones.
        """
        sess = self._get_session(ip)
        with self._cfg_lock:
            cfg = StringingConfig(**self.config.__dict__)
        with sess.lock:
            sess.ignored_defects.clear()
            return self._build_status_locked(sess, cfg)

    def get_status(self, ip: Optional[str] = None) -> Dict[str, Any]:
        sess = self._get_session(ip)
        with self._cfg_lock:
            cfg = StringingConfig(**self.config.__dict__)
        with sess.lock:
            return self._build_status_locked(sess, cfg)

    def get_snapshot(self, ip: Optional[str] = None, alert_only: bool = False) -> Optional[bytes]:
        sess = self._get_session(ip)
        with sess.lock:
            if alert_only:
                return sess.alert_snapshot_jpeg
            return sess.alert_snapshot_jpeg or sess.latest_annotated_jpeg

    def _trigger_moonraker_pause(self, ip: str) -> Tuple[bool, str]:
        """Safely pause active print via Moonraker only if printer is actively printing."""
        import json as _json

        base = f"http://{ip}:{self.moonraker_port}"
        try:
            raw = self._http_get(f"{base}/printer/objects/query?print_stats&pause_resume", 2.0)
            st = _json.loads(raw.decode("utf-8")).get("result", {}).get("status", {})
            pstate = str((st.get("print_stats") or {}).get("state") or "").lower()
            is_paused = bool((st.get("pause_resume") or {}).get("is_paused"))
            if is_paused or pstate == "paused":
                return True, "already_paused"
            if pstate != "printing":
                return False, f"not_printing:{pstate}"
            status, _ = self._http_post(f"{base}/printer/print/pause", {}, 2.5)
            if 200 <= status < 300:
                return True, "moonraker"
            return False, f"http_{status}"
        except Exception as exc:
            return False, f"err:{exc}"

    def execute_pause(self, ip: Optional[str] = None) -> Dict[str, Any]:
        resolved = ip or self.default_ip
        ok, detail = self._trigger_moonraker_pause(resolved)
        return {"ok": ok, "ip": resolved, "method": detail}

    def _score_frame_cv(
        self,
        sess: _IpSession,
        rgb: np.ndarray,
        ctx: Optional[PrinterContext],
        now: float,
        eff_thr: float = 0.64,
    ) -> Tuple[float, List[Dict[str, Any]], float, int, int]:
        """
        Core CV evaluation on normalized image.
        Returns (frame_score, regions, mean_luma, total_strands, anomaly_tiles_count).
        """
        h, w = rgb.shape[:2]
        if w != ANALYZE_WIDTH and w > 0:
            new_h = max(64, int(round(h * (ANALYZE_WIDTH / float(w)))))
            rgb_small = cv2.resize(rgb, (ANALYZE_WIDTH, new_h), interpolation=cv2.INTER_AREA)
        else:
            rgb_small = rgb
        sh, sw = rgb_small.shape[:2]

        if sess.roi_mask is None or sess.roi_mask_shape != (sh, sw):
            sess.roi_mask = _compute_roi_mask(sh, sw)
            sess.roi_mask_shape = (sh, sw)

        filament_map, best_orient, sobel_mag, l_chan = _compute_symmetrical_filament_map(rgb_small)
        macro_blur = cv2.GaussianBlur(rgb_small, (11, 11), 3.5).astype(np.float32)
        mean_luma = float(l_chan.mean())

        # Detect abrupt LED switch / exposure jump
        if sess.prev_mean_luma is not None and abs(mean_luma - sess.prev_mean_luma) > 16.0:
            sess.bg_ridge = None
            sess.bg_envelope = None
            sess.bg_macro_lab = None
            sess.prev_gray = l_chan
            sess.prev_mean_luma = mean_luma
            sess.calibration_left = max(sess.calibration_left, 8)
            sess.exposure_skip_left = 4
            sess.streak_sec = 0.0
            sess.ema_score *= 0.25
            return 0.0, [], mean_luma, 0, 0

        sess.prev_mean_luma = mean_luma

        head_mask, _ = _extract_moving_head_mask(l_chan, sess.prev_gray)
        luma_diff = cv2.absdiff(l_chan, sess.prev_gray) if sess.prev_gray is not None and sess.prev_gray.shape == l_chan.shape else np.zeros((sh, sw), dtype=np.uint8)
        sess.prev_gray = l_chan

        valid = (sess.roi_mask > 0.5) & (~head_mask)
        masked_map = np.where(valid, filament_map, 0.0)

        if sess.exposure_skip_left > 0:
            sess.exposure_skip_left -= 1
            return 0.0, [], mean_luma, 0, 0

        # Baseline scene initialization & calibration window
        if sess.bg_ridge is None or sess.bg_ridge.shape != masked_map.shape:
            sess.bg_ridge = masked_map.copy()
            sess.bg_envelope = masked_map.copy()
            sess.bg_macro_lab = macro_blur.copy()
            sess.calibration_left = max(1, sess.calibration_left - 1)
            return 0.0, [], mean_luma, 0, 0

        if sess.calibration_left > 0:
            sess.bg_ridge = np.where(valid, cv2.addWeighted(sess.bg_ridge, 0.65, masked_map, 0.35, 0.0), sess.bg_ridge)
            if sess.bg_envelope is None or sess.bg_envelope.shape != masked_map.shape:
                sess.bg_envelope = masked_map.copy()
            else:
                sess.bg_envelope = np.where(valid, np.maximum(sess.bg_envelope, masked_map), sess.bg_envelope)
            if sess.bg_macro_lab is None or sess.bg_macro_lab.shape != macro_blur.shape:
                sess.bg_macro_lab = macro_blur.copy()
            else:
                v3 = valid[:, :, None]
                sess.bg_macro_lab = np.where(v3, 0.65 * sess.bg_macro_lab + 0.35 * macro_blur, sess.bg_macro_lab)
            sess.calibration_left -= 1
            return 0.0, [], mean_luma, 0, 0

        # Spatially tolerant baseline subtraction: absorb +-2px bed Z-drop, shimmer,
        # and previously accepted post-recalibration strands without muting new growth.
        backoff = max(0, int(sess.backoff_level))
        bg_ref = (
            0.50 * sess.bg_ridge + 0.50 * sess.bg_envelope
            if sess.bg_envelope is not None
            else sess.bg_ridge
        )
        bg_env = cv2.dilate(bg_ref, _BASE_ENV_KERNEL)
        margin_mult = 1.04 + 0.02 * min(2, backoff)
        margin_add = 4.2 + 0.8 * min(2, backoff)
        novel_ridge = np.maximum(0.0, masked_map - np.maximum(5.5, margin_mult * bg_env + margin_add))

        valid_vals = novel_ridge[valid]
        noise_p75 = float(np.percentile(valid_vals, 75)) if valid_vals.size > 0 else 0.0
        thr = max(12.5 + 0.8 * min(2, backoff), (3.0 + 0.12 * min(2, backoff)) * noise_p75)

        cand = ((novel_ridge > thr) & valid).astype(np.uint8)

        # Reject thick solid extrusions: genuine strings dissolve under 7x7 erosion
        solid_core = cv2.erode(cand, _THIN_ERODE_KERNEL)
        if solid_core.any():
            cand = cand & (~cv2.dilate(solid_core, _THIN_DILATE_KERNEL))

        # Keep only coherent elongated strands; filter out short PEI glitter speckles
        min_area = 12
        min_diag = 11.5
        n_cc, cc_labels, cc_stats, _ = cv2.connectedComponentsWithStats(cand, connectivity=8)
        thin_mask = np.zeros((sh, sw), dtype=np.uint8)
        kept_ids: List[int] = []
        comp_diag: Dict[int, float] = {}
        for cid in range(1, n_cc):
            area = int(cc_stats[cid, cv2.CC_STAT_AREA])
            bw = int(cc_stats[cid, cv2.CC_STAT_WIDTH])
            bh = int(cc_stats[cid, cv2.CC_STAT_HEIGHT])
            diag = math.hypot(bw, bh)
            if area < min_area or diag < min_diag:
                continue
            # Reject wide blocky chunks with high fill solidity
            if area > 1200 and (area / float(max(1, bw * bh))) > 0.45:
                continue
            thin_mask[cc_labels == cid] = 1
            kept_ids.append(cid)
            comp_diag[cid] = diag

        if sess.bg_macro_lab is not None and sess.bg_macro_lab.shape == macro_blur.shape:
            macro_diff_map = np.mean(np.abs(macro_blur - sess.bg_macro_lab), axis=2)
        else:
            macro_diff_map = np.zeros((sh, sw), dtype=np.float32)

        if not kept_ids:
            sess.bg_ridge = np.where(valid, cv2.addWeighted(sess.bg_ridge, 0.985, masked_map, 0.015, 0.0), sess.bg_ridge)
            if sess.bg_envelope is not None and backoff == 0:
                sess.bg_envelope = np.where(valid, cv2.addWeighted(sess.bg_envelope, 0.992, masked_map, 0.008, 0.0), sess.bg_envelope)
            if sess.bg_macro_lab is not None and backoff == 0:
                v3 = valid[:, :, None]
                sess.bg_macro_lab = np.where(v3, 0.985 * sess.bg_macro_lab + 0.015 * macro_blur, sess.bg_macro_lab)
            return 0.0, [], mean_luma, 0, 0

        # Spatial sliding tiles (approx 84x64 with 42x32 stride)
        tile_w = max(60, int(round(sw * 0.175)))
        tile_h = max(48, int(round(sh * 0.24)))
        step_x = max(30, tile_w // 2)
        step_y = max(24, tile_h // 2)

        flutter_mask = (luma_diff >= 3) & (luma_diff <= 34) & (~head_mask)
        tile_scores: List[float] = []
        raw_boxes: List[Dict[str, Any]] = []
        detected_cid_set = set()
        box_min_score = max(0.45, eff_thr * 0.72)

        infill_penalty = 0.0
        if ctx and ctx.feature_category == "infill" and ctx.is_extruding:
            infill_penalty = 0.08

        top_crop = max(14, int(round(sh * 0.12)))
        bot_crop = max(10, int(round(sh * 0.08)))
        side_crop = max(8, int(round(sw * 0.04)))

        for y0 in range(top_crop, sh - bot_crop - tile_h + 1, step_y):
            y1 = y0 + tile_h
            for x0 in range(side_crop, sw - side_crop - tile_w + 1, step_x):
                x1 = x0 + tile_w
                t_valid = valid[y0:y1, x0:x1]
                if t_valid.mean() < 0.45:
                    continue

                t_thin = thin_mask[y0:y1, x0:x1]
                active_px = int(t_thin.sum())
                if active_px < 18:
                    continue

                t_novel = novel_ridge[y0:y1, x0:x1]
                t_raw_fmap = masked_map[y0:y1, x0:x1]
                t_sobel = sobel_mag[y0:y1, x0:x1]
                t_orient = best_orient[y0:y1, x0:x1]
                t_cc = cc_labels[y0:y1, x0:x1]
                t_mdiff = macro_diff_map[y0:y1, x0:x1]

                present_cids = np.unique(t_cc[t_thin > 0])
                strand_count = int(len(present_cids))
                if strand_count == 0:
                    continue
                diags = [comp_diag.get(int(cid), 12.0) for cid in present_cids]
                span_sum = sum(diags)
                max_diag = max(diags)

                qh, qw = tile_h // 2, tile_w // 2
                quads_hit = sum(
                    1
                    for qa in (
                        t_thin[:qh, :qw],
                        t_thin[:qh, qw:],
                        t_thin[qh:, :qw],
                        t_thin[qh:, qw:],
                    )
                    if int(qa.sum()) >= 5
                )

                orient_vals = t_orient[t_thin > 0]
                bins = np.bincount(orient_vals, minlength=_N_ANGLES).astype(np.float32)
                probs = bins / float(max(1, bins.sum()))
                nz = probs[probs > 0]
                entropy = float(-(nz * np.log(nz)).sum() / math.log(float(_N_ANGLES))) if nz.size > 0 else 0.0

                t_ring = cv2.dilate(t_thin, _EDGE_RING_KERNEL) > 0
                mean_ridge_on_strand = float(t_novel[t_thin > 0].mean())
                p85_ridge_on_strand = float(np.percentile(t_novel[t_thin > 0], 85))
                mean_sobel_on_strand = float(t_sobel[t_ring].mean()) + 1e-3
                edge_purity = min(1.0, (mean_ridge_on_strand / (mean_sobel_on_strand * 1.15 + 4.0)))

                flut_frac = float(flutter_mask[y0:y1, x0:x1][t_thin > 0].mean())
                flutter_bonus = min(0.08, flut_frac * 0.20)

                length_term = 1.0 - math.exp(-span_sum / 92.0)
                strand_term = min(1.0, (strand_count - 0.2) / 4.2)
                quad_term = quads_hit / 4.0
                strength_term = min(1.0, max(0.0, (mean_ridge_on_strand - 11.0) / 34.0))
                pattern_term = max(entropy, 0.90 * min(1.0, strand_count / 3.2) * quad_term)

                score = (
                    0.30 * length_term
                    + 0.25 * strand_term
                    + 0.19 * edge_purity
                    + 0.16 * pattern_term
                    + 0.10 * strength_term
                    + flutter_bonus
                    - infill_penalty
                )

                # Textured PEI plate / granular surface suppression:
                macro_delta_strand = float(t_mdiff[t_thin > 0].mean())
                tile_fmap_med = float(np.median(t_raw_fmap))
                tile_fmap_p75 = float(np.percentile(t_raw_fmap, 75))

                # If virtually no macro appearance change from baseline, reject specular sparkle
                if macro_delta_strand < 7.5:
                    score *= 0.30
                elif macro_delta_strand < 12.0 and max_diag < 28.0:
                    score *= 0.45

                if tile_fmap_med >= 6.5:
                    peak_over_carpet = p85_ridge_on_strand / (tile_fmap_p75 + 4.0)
                    if macro_delta_strand < 11.0 and (peak_over_carpet < 1.45 or max_diag < 28.0):
                        score *= 0.28
                    elif tile_fmap_med >= 9.5 and macro_delta_strand < 14.0 and peak_over_carpet < 1.65:
                        score *= 0.38

                # Active extrusion support/infill regular pattern gating
                if ctx and ctx.is_extruding and ctx.feature_category in ("infill", "support"):
                    if entropy < 0.44:
                        score *= 0.42

                # Gate out isolated weak lines or pure high-gradient solid textures
                if edge_purity < 0.28 or (strand_count < 2 and span_sum < 48.0):
                    score *= 0.45
                if quads_hit < 2 and strand_count < 2:
                    score *= 0.60
                if max_diag < 18.0 and span_sum < 42.0:
                    score *= 0.35

                score = max(0.0, min(1.0, score))

                cand_box = {
                    "x": round(x0 / float(sw), 4),
                    "y": round(y0 / float(sh), 4),
                    "w": round(tile_w / float(sw), 4),
                    "h": round(tile_h / float(sh), 4),
                    "score": round(score, 3),
                    "strands": strand_count,
                }
                matched_ign = _matches_ignored(cand_box, sess.ignored_defects)
                if matched_ign is not None:
                    # Matched an acknowledged/ignored defect footprint: exclude from frame score
                    cand_box["ignored"] = True
                    cand_box["defect_id"] = matched_ign.get("id", "DEF")
                    raw_boxes.append(cand_box)
                else:
                    cand_box["ignored"] = False
                    tile_scores.append(score)
                    if score >= box_min_score:
                        for cid in present_cids:
                            detected_cid_set.add(int(cid))
                        raw_boxes.append(cand_box)

        total_strands = len(detected_cid_set)

        regions = _merge_boxes(raw_boxes)
        if tile_scores:
            tile_scores.sort(reverse=True)
            top1 = tile_scores[0]
            top_k = tile_scores[:3]
            cluster_mean = sum(top_k) / float(len(top_k))
            multi_tile_bonus = 0.05 if len([s for s in tile_scores if s >= max(0.45, box_min_score)]) >= 2 else 0.0
            frame_score = min(1.0, 0.62 * top1 + 0.38 * cluster_mean + multi_tile_bonus)
        else:
            frame_score = 0.0

        # Region-selective background adaptation: even if another area has mild strings,
        # clean empty bed tiles outside detected clusters gently adapt to Z-shift & shimmer.
        if frame_score < eff_thr * 0.60:
            non_anom = valid & (cv2.dilate(thin_mask, _CAND_DILATE_KERNEL) == 0)
            if non_anom.any():
                sess.bg_ridge = np.where(non_anom, cv2.addWeighted(sess.bg_ridge, 0.985, masked_map, 0.015, 0.0), sess.bg_ridge)
                if sess.bg_envelope is not None and backoff == 0:
                    sess.bg_envelope = np.where(non_anom, cv2.addWeighted(sess.bg_envelope, 0.992, masked_map, 0.008, 0.0), sess.bg_envelope)
                if sess.bg_macro_lab is not None and backoff == 0:
                    na3 = non_anom[:, :, None]
                    sess.bg_macro_lab = np.where(na3, 0.985 * sess.bg_macro_lab + 0.015 * macro_blur, sess.bg_macro_lab)

        return frame_score, regions, mean_luma, total_strands, len(raw_boxes)

    def _build_status_locked(self, sess: _IpSession, cfg: StringingConfig, cur_time: Optional[float] = None) -> Dict[str, Any]:
        confirm_s = cfg.resolved_confirm_seconds(sess.backoff_level)
        eff_sens = cfg.effective_sensitivity(sess.backoff_level)
        eff_thr = cfg.resolved_threshold(sess.backoff_level)
        base_thr = cfg.resolved_threshold(0)
        sens_pct = threshold_to_sensitivity_pct(eff_thr)
        base_sens_pct = threshold_to_sensitivity_pct(base_thr)
        confirm_pct = min(1.0, max(0.0, sess.streak_sec / confirm_s)) if confirm_s > 0 else 0.0
        now_sec = time.time() if cur_time is None else float(cur_time)
        return {
            "ok": True,
            "ip": sess.ip,
            "enabled": cfg.enabled,
            "auto_pause": cfg.auto_pause,
            "sensitivity": cfg.sensitivity,
            "effective_sensitivity": eff_sens,
            "sensitivity_pct": sens_pct,
            "base_sensitivity_pct": base_sens_pct,
            "backoff_level": int(sess.backoff_level),
            "threshold": round(eff_thr, 2),
            "base_threshold": round(base_thr, 2),
            "confirm_seconds": round(confirm_s, 1),
            "ai_state": sess.ai_state,
            "score": round(sess.last_raw_score, 3),
            "ema_score": round(sess.ema_score, 3),
            "confidence_pct": int(round(sess.ema_score * 100)),
            "streak_sec": round(sess.streak_sec, 2),
            "confirm_pct": round(confirm_pct, 3),
            "regions": list(sess.last_regions),
            "ignored_count": len(sess.ignored_defects),
            "ignored_defects": list(sess.ignored_defects),
            "frames_analyzed": sess.frames_analyzed,
            "avg_infer_ms": round(sess.avg_infer_ms, 1),
            "calibration_left": sess.calibration_left,
            "paused_by_ai": sess.paused_by_ai,
            "cooldown_remaining_s": round(max(0.0, sess.cooldown_until - now_sec), 1),
            "alert": sess.alert_info,
            "has_snapshot": sess.alert_snapshot_jpeg is not None or sess.latest_annotated_jpeg is not None,
            "printer_state": sess.last_printer_state or "unknown",
        }

    def analyze_frame(
        self,
        frame_rgb: np.ndarray,
        ip: Optional[str] = None,
        ctx: Optional[PrinterContext] = None,
        now: Optional[float] = None,
        assume_printing_if_no_ctx: bool = False,
    ) -> Dict[str, Any]:
        """
        Inspect one decoded video frame (RGB uint8) and update temporal state.
        Returns rich status dict including `auto_pause_triggered` & `should_pause_ui`
        if sustained stringing crosses confirmation threshold during active printing.
        """
        t0 = time.perf_counter()
        cur_time = time.time() if now is None else float(now)
        sess = self._get_session(ip or (ctx.ip if ctx else None))

        with self._cfg_lock:
            cfg = StringingConfig(**self.config.__dict__)

        if not cfg.enabled:
            with sess.lock:
                sess.ai_state = "disabled"
                sess.streak_sec = 0.0
                sess.ema_score = 0.0
                sess.last_regions = []
                res = self._build_status_locked(sess, cfg, cur_time=cur_time)
                res["auto_pause_triggered"] = False
                res["should_pause_ui"] = False
                return res

        with sess.lock:
            dt = 0.4
            if sess.last_eval_ts > 0.0:
                dt = max(0.05, min(2.0, cur_time - sess.last_eval_ts))
            sess.last_eval_ts = cur_time

            printer_state = (ctx.state.lower() if ctx else ("printing" if assume_printing_if_no_ctx else "unknown"))
            # Detect transition into printing:
            # - From paused: preserve/step-down sensitivity if resuming from AI detection & relock baseline
            # - From standby/idle/complete: fresh job starts cleanly at full configured sensitivity
            if sess.last_printer_state in ("paused", "standby", "idle", "complete") and printer_state == "printing":
                if sess.last_printer_state in ("standby", "idle", "complete"):
                    sess.backoff_level = 0
                    sess.ignored_defects.clear()
                elif sess.paused_by_ai or sess.alert_info is not None or sess.streak_sec > 0.2:
                    sess.backoff_level = min(2, sess.backoff_level + 1)
                sess.bg_ridge = None
                sess.bg_envelope = None
                sess.bg_macro_lab = None
                sess.calibration_left = max(6, cfg.calibration_frames)
                sess.streak_sec = 0.0
                sess.ema_score = 0.0
                sess.paused_by_ai = False
                sess.cooldown_until = max(sess.cooldown_until, cur_time + cfg.cooldown_seconds * 0.5)
            sess.last_printer_state = printer_state

            eff_thr = cfg.resolved_threshold(sess.backoff_level)
            eff_confirm = cfg.resolved_confirm_seconds(sess.backoff_level)

            # Compute CV vision score
            frame_score, regions, mean_luma, total_strands, anomaly_tiles = self._score_frame_cv(
                sess, frame_rgb, ctx, cur_time, eff_thr=eff_thr
            )
            infer_ms = (time.perf_counter() - t0) * 1000.0
            sess.frames_analyzed += 1
            sess.avg_infer_ms = infer_ms if sess.frames_analyzed == 1 else (0.85 * sess.avg_infer_ms + 0.15 * infer_ms)

            # Update context gating
            in_prep = bool(
                ctx
                and (
                    ctx.activity_code in ("homing", "heating", "preparing")
                    or ctx.feature_category == "prime"
                    or (ctx.layer is not None and ctx.layer < 1 and not ctx.is_extruding)
                )
            )
            is_active_print = (printer_state == "printing") and (not in_prep)

            sess.last_raw_score = frame_score
            sess.last_regions = regions

            auto_pause_triggered = False
            should_pause_ui = False

            if sess.paused_by_ai and printer_state == "paused":
                sess.ai_state = "paused_by_ai"
                sess.streak_sec = 0.0
            elif not is_active_print:
                sess.ema_score = 0.25 * frame_score + 0.75 * sess.ema_score
                sess.streak_sec = max(0.0, sess.streak_sec - dt * 2.0)
                sess.ai_state = "calibrating" if sess.calibration_left > 0 else ("paused" if printer_state == "paused" else "standby")
            elif sess.calibration_left > 0 or sess.exposure_skip_left > 0:
                sess.ai_state = "calibrating"
                sess.streak_sec = 0.0
                sess.ema_score = 0.0
            else:
                sess.ema_score = 0.42 * frame_score + 0.58 * sess.ema_score

                if sess.ema_score >= eff_thr or (frame_score >= eff_thr and sess.ema_score >= eff_thr * 0.85):
                    sess.streak_sec += dt
                    sess.ai_state = "warning"
                elif frame_score >= eff_thr * 0.78:
                    sess.streak_sec = max(0.0, sess.streak_sec - dt * 0.35)
                    sess.ai_state = "warning" if sess.streak_sec > 0.4 else "monitoring"
                else:
                    sess.streak_sec = max(0.0, sess.streak_sec - dt * 2.2)
                    sess.ai_state = "monitoring"

                if regions and frame_score >= max(0.46, eff_thr * 0.75):
                    sess.latest_annotated_jpeg = _render_evidence_jpeg(
                        frame_rgb, regions, max(frame_score, sess.ema_score), "Rilevamento Stringing"
                    )

                if sess.streak_sec >= eff_confirm:
                    # Sustained stringing confirmed!
                    snap = _render_evidence_jpeg(
                        frame_rgb, regions, max(frame_score, sess.ema_score), "AUTO-PAUSA STRINGING"
                    )
                    sess.alert_snapshot_jpeg = snap
                    sess.latest_annotated_jpeg = snap

                    can_pause = cfg.auto_pause and (not sess.paused_by_ai) and (cur_time >= sess.cooldown_until)
                    alert_ts = time.strftime("%H:%M:%S", time.localtime(cur_time))
                    sess.alert_info = {
                        "timestamp": cur_time,
                        "time_str": alert_ts,
                        "score": round(max(frame_score, sess.ema_score), 3),
                        "confidence_pct": int(round(max(frame_score, sess.ema_score) * 100)),
                        "strands": total_strands,
                        "layer": ctx.layer if ctx else None,
                        "feature_label": ctx.feature_label if ctx else "",
                        "regions": list(regions),
                        "paused": False,
                        "reason": f"Stringing persistente rilevato ({int(round(max(frame_score, sess.ema_score) * 100))}%)",
                    }

                    if can_pause and ctx is not None:
                        paused_ok, pause_method = self._trigger_moonraker_pause(sess.ip)
                        if pause_method.startswith("not_printing:"):
                            # Race guard: printer stopped/finished before frame completed analysis
                            sess.ai_state = "standby"
                            sess.streak_sec = 0.0
                        else:
                            sess.paused_by_ai = True
                            sess.cooldown_until = cur_time + cfg.cooldown_seconds
                            sess.ai_state = "paused_by_ai"
                            sess.alert_info["paused"] = True
                            sess.alert_info["pause_method"] = pause_method
                            auto_pause_triggered = True
                            should_pause_ui = not paused_ok
                    elif can_pause:
                        # No Moonraker context available: delegate pause execution to UI (:9999)
                        sess.paused_by_ai = True
                        sess.cooldown_until = cur_time + cfg.cooldown_seconds
                        sess.ai_state = "paused_by_ai"
                        sess.alert_info["paused"] = True
                        sess.alert_info["pause_method"] = "ui_delegate"
                        auto_pause_triggered = True
                        should_pause_ui = True
                    else:
                        sess.ai_state = "alert_only"

            res = self._build_status_locked(sess, cfg, cur_time=cur_time)
            res["frame_score"] = round(frame_score, 3)
            res["mean_luma"] = round(mean_luma, 1)
            res["strands_detected"] = total_strands
            res["anomaly_tiles"] = anomaly_tiles
            res["auto_pause_triggered"] = auto_pause_triggered
            res["should_pause_ui"] = should_pause_ui
            sess.last_result = res
            return res


def context_from_telemetry(payload: Optional[Dict[str, Any]], fallback_ip: str = "192.168.78.50") -> Optional[PrinterContext]:
    """Convert a `PrinterTelemetryTracker.poll()` dict into a `PrinterContext`."""
    if not payload or not payload.get("ok"):
        return None
    return PrinterContext(
        ip=str(payload.get("ip") or fallback_ip),
        state=str(payload.get("state") or "unknown").lower(),
        activity_code=str(payload.get("activity_code") or "unknown").lower(),
        feature_category=str(payload.get("feature_category") or "").lower(),
        feature_label=str(payload.get("activity_label") or payload.get("feature_label") or ""),
        layer=payload.get("layer"),
        total_layers=payload.get("total_layers"),
        nozzle_speed_mm_s=float(payload.get("nozzle_speed_mm_s") or 0.0),
        is_extruding=bool(payload.get("is_extruding")),
    )


def decode_image_bytes(raw_bytes: bytes) -> Optional[np.ndarray]:
    """Decode JPEG/PNG/WebP bytes into RGB uint8 ndarray, or return None."""
    if not raw_bytes:
        return None
    arr = np.frombuffer(raw_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None or bgr.size == 0:
        return None
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
