"""
Generates the architecture diagram for Creality Vision AI in PNG format.
Uses high-DPI HTML/SVG rendered via headless Edge for razor-sharp vector quality,
standardized scientific aesthetic, white background, and crisp typography.
"""

import os
import subprocess
from PIL import Image

DIAGRAM_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    width: 2200px;
    height: 1320px;
    background-color: #ffffff;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    color: #1e293b;
    padding: 35px 45px;
    position: relative;
    overflow: hidden;
  }

  /* Academic / Scientific Header */
  .header {
    border-bottom: 2px solid #0f172a;
    padding-bottom: 16px;
    margin-bottom: 26px;
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
  }
  .header-left h1 {
    font-size: 26px;
    font-weight: 800;
    letter-spacing: -0.5px;
    color: #0f172a;
    text-transform: uppercase;
  }
  .header-left p {
    font-size: 13.5px;
    color: #475569;
    font-weight: 500;
    margin-top: 4px;
    letter-spacing: 0.2px;
  }
  .header-right {
    text-align: right;
    font-size: 12px;
    color: #64748b;
    font-family: ui-monospace, "Cascadia Code", "SFMono-Regular", Menlo, monospace;
    line-height: 1.5;
  }

  /* Main Grid Layout */
  .grid-container {
    display: grid;
    grid-template-columns: 560px 480px 620px 410px;
    gap: 24px;
    height: 1080px;
  }

  /* Standard Scientific Module Card */
  .module-card {
    background: #ffffff;
    border: 1.5px solid #cbd5e1;
    border-radius: 8px;
    display: flex;
    flex-direction: column;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    position: relative;
  }
  .module-header {
    padding: 10px 16px;
    border-bottom: 1.5px solid #cbd5e1;
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: #f8fafc;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
  }
  .module-title {
    font-size: 13.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.6px;
  }
  .module-tag {
    font-size: 11px;
    font-weight: 600;
    padding: 2px 7px;
    border-radius: 4px;
    font-family: ui-monospace, monospace;
  }

  /* Color themes for modules */
  .mod-client { border-color: #3b82f6; }
  .mod-client .module-header { background: #eff6ff; border-color: #3b82f6; }
  .mod-client .module-title { color: #1d4ed8; }
  .mod-client .module-tag { background: #dbeafe; color: #1e40af; }

  .mod-net { border-color: #6366f1; }
  .mod-net .module-header { background: #eef2ff; border-color: #6366f1; }
  .mod-net .module-title { color: #4338ca; }
  .mod-net .module-tag { background: #e0e7ff; color: #3730a3; }

  .mod-ai { border-color: #0d9488; }
  .mod-ai .module-header { background: #f0fdfa; border-color: #0d9488; }
  .mod-ai .module-title { color: #0f766e; }
  .mod-ai .module-tag { background: #ccfbf1; color: #115e59; }

  .mod-hw { border-color: #d97706; }
  .mod-hw .module-header { background: #fffbeb; border-color: #d97706; }
  .mod-hw .module-title { color: #b45309; }
  .mod-hw .module-tag { background: #fef3c7; color: #92400e; }

  .module-body {
    padding: 14px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    flex: 1;
    background: #ffffff;
  }

  /* Internal Boxes / Nodes */
  .node {
    border: 1px solid #e2e8f0;
    background: #f8fafc;
    border-radius: 6px;
    padding: 9px 12px;
    position: relative;
  }
  .node-title {
    font-size: 12px;
    font-weight: 700;
    color: #1e293b;
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 4px;
  }
  .node-desc {
    font-size: 11px;
    color: #475569;
    line-height: 1.4;
  }
  .code-snip {
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 10.5px;
    background: #0f172a;
    color: #f8fafc;
    padding: 4px 8px;
    border-radius: 4px;
    margin-top: 5px;
    display: block;
    line-height: 1.4;
  }

  /* Flow indicators */
  .flow-item {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 11px;
    font-weight: 600;
    color: #334155;
    background: #ffffff;
    border: 1px dashed #cbd5e1;
    border-radius: 5px;
    padding: 6px 10px;
  }
  .flow-num {
    background: #0f172a;
    color: #ffffff;
    width: 17px;
    height: 17px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 10px;
    flex-shrink: 0;
  }

  /* Specific Layout Details */
  .highlight-box {
    background: #f1f5f9;
    border-left: 3px solid #0284c7;
    padding: 7px 10px;
    font-size: 11px;
    border-radius: 0 4px 4px 0;
  }
  .math-box {
    font-family: "Cambria Math", "Georgia", "Times New Roman", serif;
    font-size: 13px;
    background: #ffffff;
    border: 1px solid #cbd5e1;
    padding: 7px 10px;
    border-radius: 5px;
    color: #0f172a;
    margin-top: 5px;
    text-align: center;
    letter-spacing: 0.3px;
  }
  .packet-table {
    width: 100%;
    border-collapse: collapse;
    font-family: ui-monospace, monospace;
    font-size: 10px;
    margin-top: 5px;
  }
  .packet-table th, .packet-table td {
    border: 1px solid #cbd5e1;
    padding: 4px 6px;
    text-align: left;
  }
  .packet-table th {
    background: #f1f5f9;
    color: #475569;
    font-weight: 700;
  }

  .badge-pill {
    display: inline-block;
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 9.5px;
    font-weight: 600;
    text-transform: uppercase;
  }
  .pill-green { background: #dcfce7; color: #15803d; }
  .pill-blue { background: #e0f2fe; color: #0369a1; }
  .pill-purple { background: #f3e8ff; color: #7e22ce; }
  .pill-amber { background: #fef3c7; color: #b45309; }

  /* Footer Note */
  .footer {
    margin-top: 15px;
    border-top: 1px solid #e2e8f0;
    padding-top: 10px;
    display: flex;
    justify-content: space-between;
    font-size: 11px;
    color: #64748b;
  }
</style>
</head>
<body>

  <div class="header">
    <div class="header-left">
      <h1>System Architecture & Runtime Data-Flow Specification</h1>
      <p>Creality Vision AI: Chromium Embedded Framework (CEF) Hooking, Zero-Copy DirectML Tensor Fusion & Low-Latency IPC</p>
    </div>
    <div class="header-right">
      <div>SPECIFICATION VERSION: 1.0.0 (STANDARDIZED)</div>
      <div>COMPLIANCE: IEEE/ACM BLOCK ARCHITECTURE</div>
      <div>HARDWARE TARGET: NVIDIA RTX / DIRECTX 12 / ELECTRON CEF</div>
    </div>
  </div>

  <div class="grid-container">

    <!-- COLUMN 1: HOST APPLICATION & INJECTION -->
    <div class="module-card mod-client">
      <div class="module-header">
        <span class="module-title">Host Client & CEF Injection</span>
        <span class="module-tag">Creality Print.exe</span>
      </div>
      <div class="module-body">
        
        <div class="node">
          <div class="node-title">
            <span>1. Static Injection Vector</span>
            <span class="badge-pill pill-blue">PowerShell ACL & AST</span>
          </div>
          <div class="node-desc">
            <b>Target:</b> <code>resources/web/deviceMgr/index.html</code><br>
            <b>Installer:</b> <code>install_upscaler.ps1</code> creates atomic backup (<code>.bak</code>), updates DACL for <code>BUILTIN\\Users:Modify</code>, and patches body with:
          </div>
          <code class="code-snip">&lt;script defer src="./upscale.js"&gt;&lt;/script&gt;</code>
        </div>

        <div class="node">
          <div class="node-title">
            <span>2. Runtime DOM Observer & Stream Hook</span>
            <span class="badge-pill pill-green">V8 Execution</span>
          </div>
          <div class="node-desc">
            <b>Target Elements:</b> <code>#remoteVideo</code> or <code>video.video-player</code><br>
            • <b>MutationObserver:</b> Continuously monitors DOM subtree for dynamic camera mounting.<br>
            • <b>Hardware VSync Tap:</b> Synchronizes via <code>HTMLVideoElement.requestVideoFrameCallback()</code> to eliminate frame tearing and avoid redundant polling.
          </div>
        </div>

        <div class="node">
          <div class="node-title">
            <span>3. Frame Capture Pipeline</span>
            <span class="badge-pill pill-purple">Offscreen 2D Buffer</span>
          </div>
          <div class="node-desc">
            Raw frame rendered to offscreen canvas at configured input scale (480x270, 640x360, 960x540). Uncompressed RGBA byte array extracted directly via <code>getImageData()</code> with zero lossy transcoding.
          </div>
        </div>

        <div class="node">
          <div class="node-title">
            <span>4. Accelerated Presentation Layer</span>
            <span class="badge-pill pill-blue">Hardware Canvas 2D</span>
          </div>
          <div class="node-desc">
            • <b>Overlay:</b> Absolute positioned <code>&lt;canvas id="crealityUpscaleCanvas"&gt;</code>.<br>
            • <b>Fallback:</b> Sets native video <code>opacity: 0</code>; immediately restores <code>opacity: 1</code> if service is offline.<br>
            • <b>Smoothing:</b> Alpha cross-fade frame interpolation (&Delta;t &lt; 120 ms) synchronized with display refresh rate via <code>requestAnimationFrame()</code>.
          </div>
        </div>

        <div class="node">
          <div class="node-title">
            <span>5. Native UI Extension</span>
            <span class="badge-pill pill-amber">HUD & Speed Hook</span>
          </div>
          <div class="node-desc">
            • Floating Control Panel: HDR shadow boost, sharpness, resolution, blending, telemetry.<br>
            • Feedrate Hook: Injects fine-grained speed slider into native <code>.print-speed-wrapper</code> component.
          </div>
        </div>

      </div>
    </div>

    <!-- COLUMN 2: NETWORK & PROTOCOL SPECIFICATION -->
    <div class="module-card mod-net">
      <div class="module-header">
        <span class="module-title">Network & Wire Protocols</span>
        <span class="module-tag">Binary IPC / REST / WS</span>
      </div>
      <div class="module-body">

        <div class="node">
          <div class="node-title">
            <span>WebSocket Video Stream</span>
            <span class="badge-pill pill-purple">ws://127.0.0.1:8088/ws_upscale</span>
          </div>
          <div class="node-desc"><b>Upstream: Client &rarr; AI Server (Binary RGBA)</b></div>
          <table class="packet-table">
            <tr><th>Offset</th><th>Field</th><th>Type</th><th>Description</th></tr>
            <tr><td>0..3</td><td>seq</td><td>uint32 (LE)</td><td>Sequential frame ID</td></tr>
            <tr><td>4..5</td><td>width</td><td>uint16 (LE)</td><td>Input width (px)</td></tr>
            <tr><td>6..7</td><td>height</td><td>uint16 (LE)</td><td>Input height (px)</td></tr>
            <tr><td>8..N</td><td>pixels</td><td>uint8[]</td><td>Raw RGBA (W&times;H&times;4 B)</td></tr>
          </table>

          <div class="node-desc" style="margin-top: 10px;"><b>Downstream: AI Server &rarr; Client (Binary JPEG)</b></div>
          <table class="packet-table">
            <tr><th>Offset</th><th>Field</th><th>Type</th><th>Description</th></tr>
            <tr><td>0..3</td><td>seq</td><td>uint32 (LE)</td><td>Matching frame ID</td></tr>
            <tr><td>4..5</td><td>infer_ms</td><td>uint16 (LE)</td><td>GPU inference time</td></tr>
            <tr><td>6..7</td><td>total_ms</td><td>uint16 (LE)</td><td>End-to-end latency</td></tr>
            <tr><td>8..N</td><td>jpeg</td><td>uint8[]</td><td>Compressed JPEG frame</td></tr>
          </table>
        </div>

        <div class="node">
          <div class="node-title">
            <span>Bidirectional Control Channel</span>
            <span class="badge-pill pill-blue">WebSocket JSON</span>
          </div>
          <div class="node-desc">
            Transmits real-time parameter tuning and bidirectional printer telemetry:
          </div>
          <code class="code-snip">{"target_res":"1080p","hdr_shadows":0.35,"sharpness":0.4}</code>
          <code class="code-snip">{"action":"set_speed","speed":115} &harr; {"type":"speed_ack"}</code>
        </div>

        <div class="node">
          <div class="node-title">
            <span>Direct Hardware Control</span>
            <span class="badge-pill pill-amber">Dual-Path Control</span>
          </div>
          <div class="node-desc">
            <b>Path A: Direct Creality Daemon Socket</b><br>
            • <code>ws://&lt;printer-ip&gt;:9999</code><br>
            • JSON-RPC: <code>{"method":"set","params":{"setFeedratePct":V}}</code><br><br>
            <b>Path B: Moonraker / Klipper REST API</b><br>
            • <code>POST http://&lt;printer-ip&gt;:7125/printer/gcode/script</code><br>
            • G-code Dispatch: <code>{"script":"M220 S{speed}"}</code>
          </div>
        </div>

        <div class="node">
          <div class="node-title">
            <span>HTTP REST Service Gateway</span>
            <span class="badge-pill pill-green">Localhost 8088</span>
          </div>
          <div class="node-desc">
            • <code>GET /status</code> : Engine stats, active clients, telemetry.<br>
            • <code>GET /snapshot</code> : Instant high-resolution JPEG capture.<br>
            • <code>GET /printer/set_speed?speed=V</code> : G-code bridge.
          </div>
        </div>

      </div>
    </div>

    <!-- COLUMN 3: AI SUPER-RESOLUTION ENGINE -->
    <div class="module-card mod-ai">
      <div class="module-header">
        <span class="module-title">AI Engine & DirectML Pipeline</span>
        <span class="module-tag">ai_upscaler_service.py</span>
      </div>
      <div class="module-body">

        <div class="node">
          <div class="node-title">
            <span>Asynchronous Concurrency Architecture</span>
            <span class="badge-pill pill-green">aiohttp + ThreadPool</span>
          </div>
          <div class="node-desc">
            • <b>Async Event Loop:</b> Handles WebSocket multiplexing & client dispatch.<br>
            • <b>Worker Pool:</b> 3 parallel CPU worker threads decouple network I/O from post-processing and JPEG compression.
          </div>
        </div>

        <div class="node">
          <div class="node-title">
            <span>Non-Linear HDR Shadow Lift</span>
            <span class="badge-pill pill-blue">O(1) LUT Algorithm</span>
          </div>
          <div class="node-desc">
            Lifts crushed shadow details (essential for black PLA/PETG) while strictly preserving mid-tones and highlights:
          </div>
          <div class="math-box">
            <b><i>x'</i></b> = <i>x</i> + (255 &minus; <i>x</i>) &times; 0.40 &times; <i>k</i> &times; (1 &minus; <sup><i>x</i></sup>&frasl;<sub>255</sub>)<sup>2</sup>
          </div>
          <div class="node-desc" style="margin-top: 4px;">
            Implemented via pre-computed 256-entry Look-Up Table (LUT) cache evaluated in a single <code>cv2.LUT()</code> pass.
          </div>
        </div>

        <div class="node">
          <div class="node-title">
            <span>Fused ONNX DirectML Model</span>
            <span class="badge-pill pill-purple">GPU VRAM Pipeline</span>
          </div>
          <div class="node-desc">
            <b>Artifact:</b> <code>realesr_animevideo_v3_fused.onnx</code> (2.5 MB)<br>
            <b>Hardware Acceleration:</b> <code>DmlExecutionProvider</code> (NVIDIA RTX)<br>
            <b>Graph Fusion (Zero-Copy Host &harr; Device):</b>
          </div>
          <div class="highlight-box" style="margin-top: 6px;">
            <b>Input:</b> <code>uint8</code> [1, H, W, 3] (NHWC format)<br>
            <b>In-Graph Ops:</b> Cast(F32) &rarr; Mul(1/255) &rarr; Transpose(NCHW) &rarr; <b>Real-ESRGAN Core</b> &rarr; Clip(0,1) &rarr; Mul(255) &rarr; Add(0.5) &rarr; Transpose(NHWC) &rarr; Cast(U8)<br>
            <b>Output:</b> <code>uint8</code> [1, 4H, 4W, 3] (NHWC format)
          </div>
        </div>

        <div class="node">
          <div class="node-title">
            <span>Post-Processing & Synthesis</span>
            <span class="badge-pill pill-teal">CPU Workers</span>
          </div>
          <div class="node-desc">
            • <b>Resampling:</b> Lanczos-4 upsampling / Area downsampling to target (720p/1080p/1440p).<br>
            • <b>Dynamic Blend:</b> <i>I</i><sub>out</sub> = &alpha; &middot; <i>I</i><sub>AI</sub> + (1 &minus; &alpha;) &middot; <i>I</i><sub>orig</sub><br>
            • <b>Unsharp Spatial Filter:</b> <i>I</i><sub>sharp</sub> = (1 + 0.9<i>s</i>) &middot; <i>I</i> &minus; 0.9<i>s</i> &middot; Blur(<i>I</i>, &sigma; = 1.0)<br>
            • <b>Fast Encoding:</b> OpenCV turbo JPEG encoder (Quality 90).
          </div>
        </div>

      </div>
    </div>

    <!-- COLUMN 4: 3D PRINTER HARDWARE LAYER -->
    <div class="module-card mod-hw">
      <div class="module-header">
        <span class="module-title">3D Printer Subsystem</span>
        <span class="module-tag">Creality / Klipper</span>
      </div>
      <div class="module-body">

        <div class="node">
          <div class="node-title">
            <span>Chamber Video Stream</span>
            <span class="badge-pill pill-amber">WebRTC / RTSP</span>
          </div>
          <div class="node-desc">
            Chamber camera outputs 480p/720p MJPEG/H.264 stream. Creality Print ingests stream natively into Chromium media pipeline.
          </div>
        </div>

        <div class="node">
          <div class="node-title">
            <span>Moonraker API Server</span>
            <span class="badge-pill pill-amber">Port 7125</span>
          </div>
          <div class="node-desc">
            • Web API server for Klipper.<br>
            • Receives G-code commands via HTTP/WebSocket.<br>
            • Dispatches <code>M220 S{speed}</code> feedrate overrides to Klipper motion queue.
          </div>
        </div>

        <div class="node">
          <div class="node-title">
            <span>Native Creality Daemon</span>
            <span class="badge-pill pill-amber">Port 9999</span>
          </div>
          <div class="node-desc">
            Proprietary WebSocket JSON-RPC daemon on Creality OS (K1/K1 Max) managing printer state, telemetry, and speed modes (Silent, Standard, Sport).
          </div>
        </div>

        <div class="node">
          <div class="node-title">
            <span>Hardware Execution</span>
            <span class="badge-pill pill-amber">Klipper Firmware</span>
          </div>
          <div class="node-desc">
            Step generation, kinematic acceleration, pressure advance, and real-time print velocity execution on CoreXY motion controller.
          </div>
        </div>

      </div>
    </div>

  </div>

  <div class="footer">
    <div>Creality Vision AI Architecture Diagram &bull; Standardized System Specification</div>
    <div>All rights reserved &bull; MIT Licensed &bull; DirectML / ONNX Runtime / Creality Print 7.3</div>
  </div>

</body>
</html>
"""

def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(base_dir, "architecture.html")
    png_path = os.path.join(base_dir, "architecture_pipeline.png")
    edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(DIAGRAM_HTML)

    print(f"Written HTML template to {html_path}")

    cmd = [
        edge_path,
        "--headless=new",
        "--disable-gpu",
        "--window-size=2200,1320",
        f"--screenshot={png_path}",
        html_path
    ]
    print("Rendering PNG with headless Edge...")
    subprocess.run(cmd, check=True)

    if os.path.exists(png_path):
        im = Image.open(png_path)
        print(f"Generated architecture.png successfully: {im.size}, format: {im.format}")
        # Clean up temporary html
        if os.path.exists(html_path):
            os.remove(html_path)
    else:
        print("Error: architecture.png was not generated!")

if __name__ == "__main__":
    main()
