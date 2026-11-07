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
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import cv2
import numpy as np
import onnxruntime as ort
from aiohttp import web

HOST = "127.0.0.1"
PORT = 8088
BASE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE, "models", "realesr_animevideo_v3_fused.onnx")
MOONRAKER_URL = "http://192.168.78.50:7125"
WORKERS = 3

print("=" * 65)
print("Creality Real-ESRGAN AI Engine (RTX 4070, fused GPU pipeline)")
print("=" * 65)

if not os.path.exists(MODEL_PATH):
    raise SystemExit("Fused model missing: run build_fused_model.py first.")

so = ort.SessionOptions()
so.enable_mem_pattern = False  # required by the DirectML provider
so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
session = ort.InferenceSession(
    MODEL_PATH, so, providers=[("DmlExecutionProvider", {"device_id": 0}), "CPUExecutionProvider"]
)
print(f"[AI] Providers: {session.get_providers()}")
session_lock = threading.Lock()  # GPU queue is serialised, CPU stages run in parallel

for _w, _h in ((480, 270), (640, 360), (960, 540)):
    session.run(None, {"input_u8": np.zeros((1, _h, _w, 3), np.uint8)})
print("[AI] Warmup complete.")

executor = ThreadPoolExecutor(max_workers=WORKERS)

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

# Tunable from the UI. Defaults are deliberately neutral.
ai_config = {
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

    global latest_jpeg
    latest_jpeg = jpeg
    header = struct.pack("<IHH", seq, min(int(infer_ms), 65535), min(int(total_ms), 65535))
    return header + jpeg


async def set_moonraker_speed(speed_percent: int):
    global current_printer_speed
    speed_percent = max(25, min(190, int(speed_percent)))
    current_printer_speed = speed_percent
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{MOONRAKER_URL}/printer/gcode/script",
                json={"script": f"M220 S{speed_percent}"},
                timeout=aiohttp.ClientTimeout(total=2.0),
            ) as resp:
                if resp.status == 200:
                    print(f"[Moonraker] M220 S{speed_percent} sent.")
                    return True
                print(f"[Moonraker] speed status {resp.status}: {await resp.text()}")
    except Exception as e:
        print(f"[Moonraker] Error setting speed: {e}")
    return False


async def get_moonraker_speed():
    global current_printer_speed
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(
                f"{MOONRAKER_URL}/printer/objects/query?gcode_move",
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
    if "target_res" in data and data["target_res"] in TARGET_HEIGHT:
        ai_config["target_res"] = data["target_res"]
    for key in ("ai_blend", "hdr_shadows", "sharpness"):
        if key in data:
            ai_config[key] = max(0.0, min(1.0, float(data[key])))
    if "jpeg_quality" in data:
        ai_config["jpeg_quality"] = max(50, min(98, int(data["jpeg_quality"])))


async def ws_upscale_handler(request):
    ws = web.WebSocketResponse(max_msg_size=32 * 1024 * 1024)
    await ws.prepare(request)
    stats["active_clients"] += 1
    print(f"[WS] Client connected ({stats['active_clients']} active)")

    loop = asyncio.get_running_loop()
    fps = {"count": 0, "t": time.time()}

    async def handle_frame(data: bytes):
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

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                asyncio.ensure_future(handle_frame(msg.data))
            elif msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    action = data.get("action")
                    if action == "set_speed":
                        await set_moonraker_speed(data.get("speed", 100))
                        await ws.send_str(json.dumps({"type": "speed_ack", "speed": current_printer_speed}))
                    elif action == "get_speed":
                        await ws.send_str(json.dumps({"type": "speed_update", "speed": await get_moonraker_speed()}))
                    else:
                        apply_config(data)
                        await ws.send_str(json.dumps({"type": "config", "config": ai_config, "stats": stats}))
                except Exception as e:
                    await ws.send_str(json.dumps({"type": "error", "error": str(e)}))
    finally:
        stats["active_clients"] -= 1
        print(f"[WS] Client disconnected ({stats['active_clients']} active)")
    return ws


CORS = {"Access-Control-Allow-Origin": "*"}


async def handle_status(request):
    return web.json_response({"stats": stats, "config": ai_config, "speed": current_printer_speed}, headers=CORS)


async def handle_set_speed(request):
    try:
        ok = await set_moonraker_speed(int(request.query.get("speed", 100)))
        return web.json_response({"status": "ok" if ok else "error", "speed": current_printer_speed}, headers=CORS)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400, headers=CORS)


async def handle_get_speed(request):
    return web.json_response({"speed": await get_moonraker_speed()}, headers=CORS)


async def handle_snapshot(request):
    if latest_jpeg is None:
        return web.Response(status=503, text="No AI frame ready yet")
    return web.Response(body=latest_jpeg, content_type="image/jpeg", headers=CORS)


app = web.Application()
app.router.add_get("/ws_upscale", ws_upscale_handler)
app.router.add_get("/status", handle_status)
app.router.add_get("/snapshot", handle_snapshot)
app.router.add_get("/printer/set_speed", handle_set_speed)
app.router.add_get("/printer/get_speed", handle_get_speed)

if __name__ == "__main__":
    web.run_app(app, host=HOST, port=PORT)
