"""
Creality SparkX i7 Deep AI Real-ESRGAN Upscaler Service
Runs on the NVIDIA RTX 4070 via ONNX Runtime DirectML.

Wire protocol (WebSocket /ws_upscale, binary frames):
  client -> server : u32 seq | u16 width | u16 height | RGBA bytes (width*height*4)
  server -> client : u32 seq | u16 infer_ms | u16 total_ms | JPEG bytes
Text frames carry JSON control messages (settings, print speed).

Performance notes:
  * Pre/post-processing (normalise, clip, quantise, NCHW->NHWC) is fused into the
    ONNX graph (see build_fused_model.py) so it runs on the GPU.
  * Input is raw RGBA, so there is no lossy JPEG round trip on the way in.
  * Several frames are processed concurrently: while one frame is on the GPU the
    others are being pre/post-processed on CPU threads.
"""

import asyncio
import json
import os
import struct
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import aiohttp
import cv2
import numpy as np
import onnxruntime as ort
from aiohttp import web

from printer_telemetry import PrinterTelemetryTracker, validate_printer_ip
from stringing_detector import StringingWatchdog, context_from_telemetry

import cp_lifecycle

HOST = "127.0.0.1"
PORT = 8088
BASE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = BASE
MODEL_PATH = os.path.join(BASE, "models", "realesr_animevideo_v3_fused.onnx")
MOONRAKER_URL = "http://192.168.78.50:7125"
DEFAULT_PRINTER_IP = urllib.parse.urlparse(MOONRAKER_URL).hostname or "192.168.78.50"
WORKERS = 3

session = None
session_lock = threading.Lock()  # GPU queue is serialised, CPU stages run in parallel
executor = ThreadPoolExecutor(max_workers=WORKERS)
telemetry_executor = ThreadPoolExecutor(max_workers=3)
telemetry_tracker = PrinterTelemetryTracker(default_ip=DEFAULT_PRINTER_IP, moonraker_port=7125)
stringing_watchdog = StringingWatchdog(default_ip=DEFAULT_PRINTER_IP, moonraker_port=7125)
active_printer_ip = DEFAULT_PRINTER_IP
_stringing_frame_lock = threading.Lock()
_last_stringing_capture = 0.0
_latest_stringing_rgb = None
STARTED_AT = time.time()
_shutdown_event: Optional[asyncio.Event] = None


def init_ai_session():
    global session
    if session is not None:
        return session

    print("=" * 65)
    print("Creality Real-ESRGAN AI Engine (RTX 4070, fused GPU pipeline)")
    print("=" * 65)

    if not os.path.exists(MODEL_PATH):
        raise SystemExit("Fused model missing: run build_fused_model.py first.")

    so = ort.SessionOptions()
    so.enable_mem_pattern = False  # required by the DirectML provider
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    sess = ort.InferenceSession(
        MODEL_PATH, so, providers=[("DmlExecutionProvider", {"device_id": 0}), "CPUExecutionProvider"]
    )
    print(f"[AI] Providers: {sess.get_providers()}")

    for _w, _h in ((480, 270), (640, 360), (960, 540)):
        sess.run(None, {"input_u8": np.zeros((1, _h, _w, 3), np.uint8)})
    print("[AI] Warmup complete.")
    session = sess
    return session

latest_jpeg = None
current_printer_speed = 100
stats = {
    "fps": 0.0,
    "inference_ms": 0.0,
    "total_ms": 0.0,
    "input_res": "-",
    "output_res": "-",
    "frames_processed": 0,
    "active_clients": 0,
}

# Tunable from the UI. Defaults are deliberately neutral and set to OFF by default.
ai_config = {
    "upscale_enabled": False,  # False = native video (default OFF); True = Real-ESRGAN GPU inference
    "target_res": "1080p",  # output resolution: 720p / 1080p / 1440p
    "ai_blend": 1.0,        # 0 = plain resize of the camera frame, 1 = full AI output
    "hdr_shadows": 0.0,     # 0..1 shadow lift, only affects dark pixels
    "sharpness": 0.35,      # 0..1 unsharp mask amount
    "jpeg_quality": 90,
}

TARGET_HEIGHT = {"720p": 720, "1080p": 1080, "1440p": 1440}

# Pre-computed shadow lift LUTs (per 1% step) so the per-frame cost is one table lookup.
_lut_cache = {}


def shadow_lut(strength: float) -> np.ndarray:
    key = int(round(strength * 100))
    lut = _lut_cache.get(key)
    if lut is None:
        x = np.arange(256, dtype=np.float32)
        t = x / 255.0
        # Lift is strongest at black and fades out quickly; mid-tones and highlights barely move.
        lifted = x + (255.0 - x) * 0.40 * (key / 100.0) * (1.0 - t) ** 2
        lut = np.clip(lifted, 0, 255).astype(np.uint8)
        _lut_cache[key] = lut
    return lut


def process_frame(payload: bytes):
    t_start = time.perf_counter()
    seq, w, h = struct.unpack_from("<IHH", payload, 0)
    rgba = np.frombuffer(payload, np.uint8, count=w * h * 4, offset=8).reshape(h, w, 4)
    rgb = np.ascontiguousarray(rgba[:, :, :3])

    cfg = ai_config
    hdr = float(cfg["hdr_shadows"])
    if hdr > 0.01:
        rgb = cv2.LUT(rgb, shadow_lut(hdr))

    t_gpu = time.perf_counter()
    with session_lock:
        out = session.run(None, {"input_u8": rgb[None]})[0][0]
    infer_ms = (time.perf_counter() - t_gpu) * 1000.0

    out_h = TARGET_HEIGHT.get(cfg["target_res"], 1080)
    out_w = int(round(out_h * w / h))
    if out.shape[0] != out_h:
        interp = cv2.INTER_AREA if out_h < out.shape[0] else cv2.INTER_LANCZOS4
        out = cv2.resize(out, (out_w, out_h), interpolation=interp)

    blend = float(cfg["ai_blend"])
    if blend < 0.99:
        base = cv2.resize(rgb, (out.shape[1], out.shape[0]), interpolation=cv2.INTER_CUBIC)
        out = cv2.addWeighted(out, blend, base, 1.0 - blend, 0)

    sharp = float(cfg["sharpness"])
    if sharp > 0.01:
        blur = cv2.GaussianBlur(out, (0, 0), 1.0)
        amount = sharp * 0.9
        out = cv2.addWeighted(out, 1.0 + amount, blur, -amount, 0)

    ok, enc = cv2.imencode(
        ".jpg", cv2.cvtColor(out, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, int(cfg["jpeg_quality"])]
    )
    jpeg = enc.tobytes()
    total_ms = (time.perf_counter() - t_start) * 1000.0

    stats["inference_ms"] = round(infer_ms, 1)
    stats["total_ms"] = round(total_ms, 1)
    stats["input_res"] = f"{w}x{h}"
    stats["output_res"] = f"{out.shape[1]}x{out.shape[0]}"
    stats["frames_processed"] += 1

    global latest_jpeg, _last_stringing_capture, _latest_stringing_rgb
    latest_jpeg = jpeg

    if stringing_watchdog.config.enabled:
        now_pf = time.monotonic()
        if (now_pf - _last_stringing_capture) >= 0.22:
            with _stringing_frame_lock:
                if (now_pf - _last_stringing_capture) >= 0.22:
                    _last_stringing_capture = now_pf
                    sh_cur, sw_cur = out.shape[:2]
                    if sw_cur > 640:
                        nh = max(64, int(round(sh_cur * (640.0 / float(sw_cur)))))
                        _latest_stringing_rgb = cv2.resize(out, (640, nh), interpolation=cv2.INTER_AREA)
                    else:
                        _latest_stringing_rgb = out.copy()

    header = struct.pack("<IHH", seq, min(int(infer_ms), 65535), min(int(total_ms), 65535))
    return header + jpeg


def _moonraker_base_url(raw_ip: Optional[str] = None) -> str:
    ip = validate_printer_ip(raw_ip, DEFAULT_PRINTER_IP)
    return f"http://{ip}:7125"


async def set_moonraker_speed(speed_percent: int, raw_ip: Optional[str] = None):
    global current_printer_speed
    speed_percent = max(25, min(190, int(speed_percent)))
    current_printer_speed = speed_percent
    base_url = _moonraker_base_url(raw_ip)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{base_url}/printer/gcode/script",
                json={"script": f"M220 S{speed_percent}"},
                timeout=aiohttp.ClientTimeout(total=2.0),
            ) as resp:
                if resp.status == 200:
                    print(f"[Moonraker] M220 S{speed_percent} sent to {base_url}.")
                    return True
                print(f"[Moonraker] speed status {resp.status}: {await resp.text()}")
    except Exception as e:
        print(f"[Moonraker] Error setting speed: {e}")
    return False


async def get_moonraker_speed(raw_ip: Optional[str] = None):
    global current_printer_speed
    base_url = _moonraker_base_url(raw_ip)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(
                f"{base_url}/printer/objects/query?gcode_move",
                timeout=aiohttp.ClientTimeout(total=2.0),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    factor = data["result"]["status"]["gcode_move"].get("speed_factor", 1.0)
                    current_printer_speed = max(25, min(190, round(float(factor) * 100)))
    except Exception:
        pass
    return current_printer_speed


def apply_config(data: dict):
    if "upscale_enabled" in data:
        ai_config["upscale_enabled"] = bool(data["upscale_enabled"])
    if "target_res" in data and data["target_res"] in TARGET_HEIGHT:
        ai_config["target_res"] = data["target_res"]
    for key in ("ai_blend", "hdr_shadows", "sharpness"):
        if key in data:
            ai_config[key] = max(0.0, min(1.0, float(data[key])))
    if "jpeg_quality" in data:
        ai_config["jpeg_quality"] = max(50, min(98, int(data["jpeg_quality"])))


def _extract_rgb_for_stringing(data: bytes) -> Optional[np.ndarray]:
    try:
        w = struct.unpack_from("<H", data, 4)[0]
        h = struct.unpack_from("<H", data, 6)[0]
        raw_pixels = np.frombuffer(data, dtype=np.uint8, offset=8)
        if raw_pixels.size == w * h * 4:
            rgba = raw_pixels.reshape((h, w, 4))
            rgb = cv2.cvtColor(rgba, cv2.COLOR_RGBA2RGB)
            if w > 640:
                nh = max(64, int(round(h * (640.0 / float(w)))))
                rgb = cv2.resize(rgb, (640, nh), interpolation=cv2.INTER_AREA)
            return rgb
    except Exception:
        pass
    return None


def _analyze_frame_with_telemetry(rgb: np.ndarray, ip: str) -> dict:
    tel = telemetry_tracker.poll(ip)
    ctx = context_from_telemetry(tel, fallback_ip=ip)
    return stringing_watchdog.analyze_frame(rgb, ip=ip, ctx=ctx)


async def ws_upscale_handler(request):
    global active_printer_ip
    ws = web.WebSocketResponse(max_msg_size=32 * 1024 * 1024)
    await ws.prepare(request)
    stats["active_clients"] += 1
    print(f"[WS] Client connected ({stats['active_clients']} active)")

    loop = asyncio.get_running_loop()
    fps = {"count": 0, "t": time.time()}
    stringing_state = {"busy": False, "last_push": 0.0}

    async def maybe_run_stringing():
        global _latest_stringing_rgb
        if stringing_state["busy"] or not stringing_watchdog.config.enabled:
            return
        now_m = time.monotonic()
        if (now_m - stringing_state["last_push"]) < 0.22:
            return
        with _stringing_frame_lock:
            rgb_snap = _latest_stringing_rgb
            _latest_stringing_rgb = None
        if rgb_snap is None:
            return
        stringing_state["busy"] = True
        stringing_state["last_push"] = now_m
        try:
            res = await loop.run_in_executor(telemetry_executor, _analyze_frame_with_telemetry, rgb_snap, active_printer_ip)
            if not ws.closed:
                await ws.send_str(json.dumps({"type": "stringing_status", "data": res}))
        except Exception as exc:
            print(f"[StringingAI] WS inspect error: {exc}")
        finally:
            stringing_state["busy"] = False

    async def handle_frame(data: bytes):
        global _latest_stringing_rgb, _last_stringing_capture
        # When upscaling is disabled by the client, skip heavy Real-ESRGAN GPU inference completely
        if not ai_config.get("upscale_enabled", True):
            if stringing_watchdog.config.enabled and not stringing_state["busy"]:
                now_m = time.monotonic()
                if (now_m - _last_stringing_capture) >= 0.22:
                    _last_stringing_capture = now_m
                    rgb_frame = await loop.run_in_executor(telemetry_executor, _extract_rgb_for_stringing, data)
                    if rgb_frame is not None:
                        with _stringing_frame_lock:
                            _latest_stringing_rgb = rgb_frame
                        asyncio.ensure_future(maybe_run_stringing())
            return

        try:
            result = await loop.run_in_executor(executor, process_frame, data)
        except Exception as e:
            print(f"[AI] frame error: {e}")
            return
        if ws.closed:
            return
        fps["count"] += 1
        now = time.time()
        if now - fps["t"] >= 1.0:
            stats["fps"] = round(fps["count"] / (now - fps["t"]), 1)
            fps["count"], fps["t"] = 0, now
        await ws.send_bytes(result)
        if _latest_stringing_rgb is not None and not stringing_state["busy"]:
            asyncio.ensure_future(maybe_run_stringing())

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                asyncio.ensure_future(handle_frame(msg.data))
            elif msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    action = data.get("action")
                    if data.get("ip"):
                        active_printer_ip = validate_printer_ip(data.get("ip"), DEFAULT_PRINTER_IP)
                    if action == "set_speed":
                        await set_moonraker_speed(data.get("speed", 100), data.get("ip"))
                        await ws.send_str(json.dumps({"type": "speed_ack", "speed": current_printer_speed}))
                    elif action == "get_speed":
                        spd = await get_moonraker_speed(data.get("ip"))
                        await ws.send_str(json.dumps({"type": "speed_update", "speed": spd}))
                    elif action == "get_telemetry":
                        tel = await loop.run_in_executor(telemetry_executor, telemetry_tracker.poll, data.get("ip"))
                        await ws.send_str(json.dumps({"type": "telemetry", "data": tel}))
                    elif action == "get_stringing":
                        st = stringing_watchdog.get_status(data.get("ip") or active_printer_ip)
                        await ws.send_str(json.dumps({"type": "stringing_status", "data": st}))
                    elif action == "stringing_config":
                        cfg = stringing_watchdog.update_config(data.get("config") or data)
                        st = stringing_watchdog.get_status(data.get("ip") or active_printer_ip)
                        await ws.send_str(json.dumps({"type": "stringing_config", "config": cfg, "status": st}))
                        await ws.send_str(json.dumps({"type": "stringing_status", "data": st}))
                    elif action == "stringing_dismiss":
                        st = stringing_watchdog.dismiss_alert(data.get("ip") or active_printer_ip)
                        await ws.send_str(json.dumps({"type": "stringing_status", "data": st}))
                    elif action == "stringing_reset":
                        st = stringing_watchdog.reset_baseline(data.get("ip") or active_printer_ip)
                        await ws.send_str(json.dumps({"type": "stringing_status", "data": st}))
                    elif action == "stringing_ack":
                        st = stringing_watchdog.acknowledge_defects(data.get("ip") or active_printer_ip)
                        await ws.send_str(json.dumps({"type": "stringing_status", "data": st}))
                    elif action == "stringing_reset_sens":
                        st = stringing_watchdog.reset_sensitivity(data.get("ip") or active_printer_ip)
                        await ws.send_str(json.dumps({"type": "stringing_status", "data": st}))
                    elif action == "stringing_clear_ignored":
                        st = stringing_watchdog.clear_ignored_defects(data.get("ip") or active_printer_ip)
                        await ws.send_str(json.dumps({"type": "stringing_status", "data": st}))
                    elif action == "stringing_ignore_region":
                        st = stringing_watchdog.ignore_defect_region(data.get("data") or data, data.get("ip") or active_printer_ip)
                        await ws.send_str(json.dumps({"type": "stringing_status", "data": st}))
                    elif action == "stringing_unignore_region":
                        st = stringing_watchdog.unignore_defect_region(data.get("data") or data, data.get("ip") or active_printer_ip)
                        await ws.send_str(json.dumps({"type": "stringing_status", "data": st}))
                    elif action == "pause_printer":
                        res = await loop.run_in_executor(telemetry_executor, stringing_watchdog.execute_pause, data.get("ip") or active_printer_ip)
                        await ws.send_str(json.dumps({"type": "pause_ack", "data": res}))
                    elif action == "set_printer_ip":
                        await ws.send_str(json.dumps({"type": "printer_ip_ack", "ip": active_printer_ip}))
                    else:
                        apply_config(data)
                        await ws.send_str(json.dumps({"type": "config", "config": ai_config, "stats": stats}))
                except Exception as e:
                    await ws.send_str(json.dumps({"type": "error", "error": str(e)}))
    finally:
        stats["active_clients"] -= 1
        print(f"[WS] Client disconnected ({stats['active_clients']} active)")
    return ws


CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}


async def handle_options(request):
    return web.Response(status=204, headers=CORS)


async def handle_status(request):
    return web.json_response(
        {
            "ok": True,
            "mode": "full_gpu",
            "pid": os.getpid(),
            "uptime_sec": round(max(0.0, time.time() - STARTED_AT), 1),
            "telemetry_only": False,
            "gpu_upscaler": True,
            "upscale_enabled": bool(ai_config.get("upscale_enabled", True)),
            "stringing_available": True,
            "providers": session.get_providers() if session is not None else [],
            "stats": stats,
            "config": ai_config,
            "speed": current_printer_speed,
            "stringing": stringing_watchdog.get_status(active_printer_ip),
        },
        headers=CORS,
    )


async def handle_set_speed(request):
    try:
        ip = request.query.get("ip")
        ok = await set_moonraker_speed(int(request.query.get("speed", 100)), ip)
        return web.json_response({"status": "ok" if ok else "error", "speed": current_printer_speed}, headers=CORS)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400, headers=CORS)


async def handle_get_speed(request):
    try:
        ip = request.query.get("ip")
        return web.json_response({"speed": await get_moonraker_speed(ip)}, headers=CORS)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400, headers=CORS)


async def handle_telemetry(request):
    global active_printer_ip
    try:
        ip = validate_printer_ip(request.query.get("ip"), DEFAULT_PRINTER_IP)
        active_printer_ip = ip
        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(telemetry_executor, telemetry_tracker.poll, ip)
        payload["stringing"] = stringing_watchdog.get_status(ip)
        return web.json_response(payload, headers=CORS)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=CORS)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500, headers=CORS)


async def handle_stringing_status(request):
    try:
        ip = validate_printer_ip(request.query.get("ip"), active_printer_ip)
        return web.json_response(stringing_watchdog.get_status(ip), headers=CORS)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=CORS)


async def handle_stringing_analyze(request):
    global active_printer_ip
    try:
        ip = validate_printer_ip(request.query.get("ip"), active_printer_ip)
        active_printer_ip = ip
        raw_bytes = await request.read()
        rgb = decode_image_bytes(raw_bytes)
        if rgb is None:
            return web.json_response({"ok": False, "error": "Invalid or empty image payload"}, status=400, headers=CORS)
        assume_printing = request.query.get("assume_printing") in ("1", "true")
        loop = asyncio.get_running_loop()

        def _run():
            tel = telemetry_tracker.poll(ip)
            ctx = context_from_telemetry(tel, fallback_ip=ip)
            return stringing_watchdog.analyze_frame(rgb, ip=ip, ctx=ctx, assume_printing_if_no_ctx=assume_printing)

        res = await loop.run_in_executor(telemetry_executor, _run)
        return web.json_response(res, headers=CORS)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=CORS)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500, headers=CORS)


async def handle_stringing_config(request):
    try:
        if request.method == "POST":
            data = await request.json()
            cfg = stringing_watchdog.update_config(data or {})
        else:
            cfg = stringing_watchdog.get_config()
        ip = validate_printer_ip(request.query.get("ip"), active_printer_ip)
        st = stringing_watchdog.get_status(ip)
        return web.json_response({"ok": True, "config": cfg, "status": st}, headers=CORS)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=CORS)


async def handle_stringing_dismiss(request):
    try:
        ip = validate_printer_ip(request.query.get("ip"), active_printer_ip)
        return web.json_response(stringing_watchdog.dismiss_alert(ip), headers=CORS)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=CORS)


async def handle_stringing_reset(request):
    try:
        ip = validate_printer_ip(request.query.get("ip"), active_printer_ip)
        return web.json_response(stringing_watchdog.reset_baseline(ip), headers=CORS)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=CORS)


async def handle_stringing_ack(request):
    try:
        ip = validate_printer_ip(request.query.get("ip"), active_printer_ip)
        return web.json_response(stringing_watchdog.acknowledge_defects(ip), headers=CORS)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=CORS)


async def handle_stringing_reset_sens(request):
    try:
        ip = validate_printer_ip(request.query.get("ip"), active_printer_ip)
        return web.json_response(stringing_watchdog.reset_sensitivity(ip), headers=CORS)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=CORS)


async def handle_stringing_clear_ignored(request):
    try:
        ip = validate_printer_ip(request.query.get("ip"), active_printer_ip)
        return web.json_response(stringing_watchdog.clear_ignored_defects(ip), headers=CORS)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=CORS)


async def handle_stringing_ignore_region(request):
    try:
        ip = validate_printer_ip(request.query.get("ip"), active_printer_ip)
        data = {}
        if request.can_read_body:
            try:
                data = await request.json()
            except Exception:
                data = {}
        if not data and "id" in request.query:
            data["id"] = request.query.get("id")
        if not data and "point_x" in request.query:
            data["point_x"] = float(request.query.get("point_x"))
            data["point_y"] = float(request.query.get("point_y"))
        if not data and "x" in request.query:
            data["x"] = float(request.query.get("x"))
            data["y"] = float(request.query.get("y"))
            data["w"] = float(request.query.get("w", 0.12))
            data["h"] = float(request.query.get("h", 0.12))
        return web.json_response(stringing_watchdog.ignore_defect_region(data, ip), headers=CORS)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=CORS)


async def handle_stringing_unignore_region(request):
    try:
        ip = validate_printer_ip(request.query.get("ip"), active_printer_ip)
        data = {}
        if request.can_read_body:
            try:
                data = await request.json()
            except Exception:
                data = {}
        if not data and "id" in request.query:
            data["id"] = request.query.get("id")
        return web.json_response(stringing_watchdog.unignore_defect_region(data, ip), headers=CORS)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=CORS)


async def handle_stringing_snapshot(request):
    try:
        ip = validate_printer_ip(request.query.get("ip"), active_printer_ip)
        alert_only = request.query.get("alert_only") in ("1", "true", "yes")
        jpeg = stringing_watchdog.get_snapshot(ip, alert_only=alert_only)
        if not jpeg:
            return web.json_response({"ok": False, "error": "No snapshot available yet"}, status=404, headers=CORS)
        snap_headers = dict(CORS)
        snap_headers["Cache-Control"] = "no-store"
        return web.Response(body=jpeg, content_type="image/jpeg", headers=snap_headers)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=CORS)


async def handle_printer_pause(request):
    try:
        ip = validate_printer_ip(request.query.get("ip"), active_printer_ip)
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(telemetry_executor, stringing_watchdog.execute_pause, ip)
        return web.json_response(res, headers=CORS)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=CORS)


async def handle_snapshot(request):
    if latest_jpeg is None:
        return web.Response(status=503, text="No AI frame ready yet", headers=CORS)
    return web.Response(body=latest_jpeg, content_type="image/jpeg", headers=CORS)


async def handle_server_manager_launch(request):
    bat_path = os.path.abspath(os.path.join(REPO_DIR, "Avvia_Server_Manager.bat"))
    opened = False
    if os.path.isfile(bat_path):
        try:
            subprocess.Popen(["cmd.exe", "/c", "start", "", bat_path], cwd=REPO_DIR)
            opened = True
        except Exception:
            opened = False
    return web.json_response({
        "ok": True,
        "opened": opened,
        "bat_path": bat_path,
    }, headers=CORS)


async def handle_cronoprint_launch(request):
    html_path = os.path.abspath(os.path.join(REPO_DIR, "CronoPrint", "printer_history.html"))
    opened = False
    if os.path.isfile(html_path):
        # 1. First probe Windows UserChoice for default browser (e.g. Opera GX, Chrome, Edge)
        if os.name == "nt":
            import winreg

            for reg_path in [
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice",
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.html\UserChoice",
            ]:
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path) as k:
                        prog_id, _ = winreg.QueryValueEx(k, "ProgId")
                        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as ck:
                            raw_cmd, _ = winreg.QueryValueEx(ck, "")
                            exe = raw_cmd.split('"')[1] if '"' in raw_cmd else raw_cmd.split()[0]
                            if os.path.isfile(exe):
                                subprocess.Popen([exe, html_path])
                                opened = True
                                break
                except Exception:
                    pass

        if not opened:
            local_app = os.environ.get("LOCALAPPDATA", "")
            prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
            prog_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")

            browser_candidates = [
                os.path.join(local_app, r"Programs\Opera GX\opera.exe"),
                os.path.join(local_app, r"Programs\Opera\launcher.exe"),
                os.path.join(prog_files, r"Google\Chrome\Application\chrome.exe"),
                os.path.join(prog_files_x86, r"Google\Chrome\Application\chrome.exe"),
                os.path.join(prog_files, r"Microsoft\Edge\Application\msedge.exe"),
                os.path.join(prog_files_x86, r"Microsoft\Edge\Application\msedge.exe"),
                os.path.join(prog_files, r"Mozilla Firefox\firefox.exe"),
                os.path.join(prog_files, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
                os.path.join(local_app, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
            ]
            for b in browser_candidates:
                if b and os.path.isfile(b):
                    try:
                        subprocess.Popen([b, html_path])
                        opened = True
                        break
                    except Exception:
                        pass
        if not opened:
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", f"Start-Process '{html_path}'"],
                    check=False,
                    timeout=5,
                )
                opened = True
            except Exception:
                pass
        if not opened and os.name == "nt":
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
    return web.json_response(
        {"ok": True, "opened": opened, "html_path": html_path, "type": "html_report"},
        headers=CORS,
    )


async def handle_cronoprint_html(request):
    html_path = os.path.abspath(os.path.join(REPO_DIR, "CronoPrint", "printer_history.html"))
    if os.path.isfile(html_path):
        try:
            with open(html_path, "rb") as f:
                content = f.read()
            return web.Response(body=content, content_type="text/html", charset="utf-8", headers=CORS)
        except Exception as exc:
            return web.Response(status=500, text=f"Error reading report: {exc}", headers=CORS)
    return web.Response(status=404, text="Report CronoPrint non ancora generato", headers=CORS)


async def handle_server_stop(request):
    global _shutdown_event
    loop = asyncio.get_running_loop()
    if _shutdown_event is not None:
        loop.call_later(0.15, _shutdown_event.set)
    return web.json_response(
        {"ok": True, "stopping": True, "pid": os.getpid(), "mode": "full_gpu"},
        headers=CORS,
    )


app = web.Application()
app.router.add_get("/ws_upscale", ws_upscale_handler)
app.router.add_get("/status", handle_status)
app.router.add_get("/snapshot", handle_snapshot)
app.router.add_post("/server/stop", handle_server_stop)
app.router.add_get("/printer/set_speed", handle_set_speed)
app.router.add_get("/printer/get_speed", handle_get_speed)
app.router.add_get("/printer/telemetry", handle_telemetry)
app.router.add_post("/printer/pause", handle_printer_pause)
app.router.add_get("/ai/stringing/status", handle_stringing_status)
app.router.add_post("/ai/stringing/analyze", handle_stringing_analyze)
app.router.add_get("/ai/stringing/config", handle_stringing_config)
app.router.add_post("/ai/stringing/config", handle_stringing_config)
app.router.add_get("/ai/stringing/reset", handle_stringing_reset)
app.router.add_post("/ai/stringing/reset", handle_stringing_reset)
app.router.add_get("/ai/stringing/ack", handle_stringing_ack)
app.router.add_post("/ai/stringing/ack", handle_stringing_ack)
app.router.add_get("/ai/stringing/dismiss", handle_stringing_dismiss)
app.router.add_post("/ai/stringing/dismiss", handle_stringing_dismiss)
app.router.add_get("/ai/stringing/reset_sensitivity", handle_stringing_reset_sens)
app.router.add_post("/ai/stringing/reset_sensitivity", handle_stringing_reset_sens)
app.router.add_get("/ai/stringing/clear_ignored", handle_stringing_clear_ignored)
app.router.add_post("/ai/stringing/clear_ignored", handle_stringing_clear_ignored)
app.router.add_get("/ai/stringing/ignore_region", handle_stringing_ignore_region)
app.router.add_post("/ai/stringing/ignore_region", handle_stringing_ignore_region)
app.router.add_get("/ai/stringing/unignore_region", handle_stringing_unignore_region)
app.router.add_post("/ai/stringing/unignore_region", handle_stringing_unignore_region)
app.router.add_get("/ai/stringing/snapshot", handle_stringing_snapshot)
app.router.add_get("/server_manager/launch", handle_server_manager_launch)
app.router.add_post("/server_manager/launch", handle_server_manager_launch)
app.router.add_get("/cronoprint/launch", handle_cronoprint_launch)
app.router.add_post("/cronoprint/launch", handle_cronoprint_launch)
app.router.add_get("/cronoprint", handle_cronoprint_html)
app.router.add_get("/cronoprint/history.html", handle_cronoprint_html)
for _cors_route in (
    "/status",
    "/server/stop",
    "/server_manager/launch",
    "/cronoprint/launch",
    "/cronoprint",
    "/printer/telemetry",
    "/printer/get_speed",
    "/printer/set_speed",
    "/printer/pause",
    "/ai/stringing/status",
    "/ai/stringing/analyze",
    "/ai/stringing/config",
    "/ai/stringing/reset",
    "/ai/stringing/ack",
    "/ai/stringing/reset_sensitivity",
    "/ai/stringing/clear_ignored",
    "/ai/stringing/ignore_region",
    "/ai/stringing/unignore_region",
    "/ai/stringing/snapshot",
):
    app.router.add_route("OPTIONS", _cors_route, handle_options)


async def _serve_app():
    global _shutdown_event
    _shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_creality_close():
        if _shutdown_event is not None and not _shutdown_event.is_set():
            try:
                loop.call_soon_threadsafe(_shutdown_event.set)
            except Exception:
                pass

    orphan_watcher = cp_lifecycle.start_orphan_guard(_on_creality_close, close_grace=8.0)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT, reuse_address=True)
    await site.start()
    print(f"======== Running on http://{HOST}:{PORT} ========")
    try:
        await _shutdown_event.wait()
    finally:
        if orphan_watcher:
            orphan_watcher.stop()
        await runner.cleanup()


if __name__ == "__main__":
    init_ai_session()
    try:
        asyncio.run(_serve_app())
    except KeyboardInterrupt:
        pass
