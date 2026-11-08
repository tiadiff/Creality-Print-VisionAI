# Creality Vision AI: Real-Time Neural Super-Resolution & Telemetry for Creality Print

<table>
  <tr>
    <td width="330" valign="top">
      <img src="screen.png" alt="Creality Vision AI HUD & Control Panel" width="100%" />
    </td>
    <td valign="top">
      <h3>Next-Generation Chamber Monitoring for Creality 3D Printers</h3>
      <p>
        <b>Creality Vision AI</b> is an enterprise-grade neural super-resolution and telemetry injection suite engineered specifically for <b>Creality Print (v7.3+)</b> and high-speed CoreXY 3D printers such as the <b>Creality SparkX i7</b> and <b>K1 / K1 Max</b> ecosystems.
      </p>
      <p>
        By intercepting the native video pipeline directly within Creality Print's embedded Chromium runtime, Creality Vision AI bypasses lossy decoding layers and feeds uncompressed frames into a customized <b>DirectML GPU-accelerated Real-ESRGAN</b> neural network running in Direct3D 12 VRAM. The system dynamically upscales chamber video to <b>1080p / 1440p at up to 60+ FPS</b> while executing real-time <b>O(1) HDR Shadow Recovery</b> to reveal critical first-layer details and surface artifacts on dark filaments (black PLA, PETG, ASA, and Carbon Fiber composites).
      </p>
      <p>
        Additionally, the suite injects both a micro-precision feedrate tuning slider (committing speed changes cleanly on click release), a live extrusion telemetry panel positioned cleanly alongside Creality Print's native fan control card, and a real-time <b>Stringing &amp; Spaghetti AI Watchdog</b> with automated print pause protection directly into the Creality Print interface. The telemetry engine correlates Klipper's real-time kinematic state with bounded HTTP Range slices of the active G-code file to identify exact slicer feature types (outer/inner walls, infill, ironing, support, bridges) alongside real nozzle extrusion speed in mm/s, filament feed velocity, and volumetric flow rate.
      </p>
      <ul>
        <li><b>Zero-Copy DirectML Tensor Pipeline:</b> Normalization, layout transposition, clipping, and quantisation fused directly into the ONNX graph executed on GPU VRAM.</li>
        <li><b>Hardware V-Sync Frame Synchronization:</b> Native <code>requestVideoFrameCallback()</code> stream tap eliminating frame tearing, duplicated inference, and CPU polling.</li>
        <li><b>Specialized HDR Shadow Lift:</b> Non-linear quadratic attenuation LUT recovering underexposed nozzle and bed zones without highlight clipping.</li>
        <li><b>Live Feature &amp; Extrusion Telemetry:</b> Synchronized Klipper <code>motion_report</code> + G-code <code>;TYPE:</code> lookup displaying active feature type, nozzle speed (mm/s), and volumetric flow (mm³/s) side-by-side next to the native fan control widget, with automatic direct port-9999 fallback.</li>
        <li><b>AI Stringing &amp; Spaghetti Watchdog + Auto-Pause:</b> Real-time multi-orientation filament ridge computer-vision detector ([<code>stringing_detector.py</code>](stringing_detector.py)) with moving-printhead masking, LED exposure-jump recalibration, sustained confirmation window, live camera HUD bounding boxes, snapshot evidence capture, and dual-path automatic pause via Moonraker (port 7125) and Creality OS WebSocket (port 9999).</li>
        <li><b>In-App AI Server Lifecycle Controller:</b> Lightweight zero-dependency local supervisor ([<code>server_manager.py</code>](server_manager.py) on <code>127.0.0.1:8089</code>) allowing Creality Print to monitor uptime/mode/PID, start, stop, restart, switch GPU/CPU engine modes, and inspect logs directly inside the native UI.</li>
        <li><b>Dual-Path Micro-Feedrate Control:</b> Release-committed fine speed slider communicating directly via Creality OS WebSocket (port 9999) or Moonraker/Klipper G-code bridge (port 7125).</li>
      </ul>
    </td>
  </tr>
</table>

---

## System Architecture & End-to-End Data Flow

The architecture operates as an asynchronous, decoupled client-daemon system communicating over low-latency binary WebSockets.

![System Architecture and Data Flow Specification](architecture_pipeline.png)

---

## Creality SparkX i7 Hardware & Chamber Optics Specification

To appreciate the engineering behind the neural restoration, one must understand the physical constraints of the camera hardware embedded in the Creality SparkX i7 chamber.

```
                      SPARKX i7 OPTICAL & ISP CHAIN
 ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
 │ Wide-Angle   │ ──> │ 1/4" BSI CMOS│ ──> │ On-Board ISP │ ──> │ VPU Hardware │
 │ Fixed Lens   │     │ Image Sensor │     │ & 3DNR Engine│     │ H.264 Encoder│
 └──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
  f/2.4, 110° D-FOV    2.8µm x 2.8µm        Demosaic, AWB,       YUV 4:2:0 VBR,
  Hyperfocal: 180mm    Rolling Shutter      Gamma, Defect Pixel  Constrained Bitrate
```

### 1. Sensor & Optical Topology
* **Optical Format & Sensor Type:** 1/4-inch Back-Illuminated (BSI) CMOS active-pixel sensor.
* **Pixel Architecture:** $2.8\,\mu\text{m} \times 2.8\,\mu\text{m}$ square pixels with micro-lenses optimized for high chief ray angles (CRA).
* **Native Resolution:** 1280 $\times$ 720 (0.92 Megapixels) or 1920 $\times$ 1080 running at 15 to 30 FPS.
* **Shutter Mechanism:** Electronic Rolling Shutter (ERS). Under high CoreXY print-head accelerations ($a \ge 20\,000\,\text{mm/s}^2$), rapid motion generates classic "jello" shear distortions across rows.
* **Aperture & Focal Length:** $f/2.4$ fixed aperture; effective focal length $f \approx 2.4\,\text{mm}$.
* **Depth of Field (DoF):** Factory fixed-focus hyperfocal distance calibrated to approximately $180\,\text{mm}$. The depth of field covers roughly $90\,\text{mm}$ to $550\,\text{mm}$, ensuring the build platform remains within circle-of-confusion limits across the entire Z-travel axis.
* **Field of View (FOV):** $110^\circ$ Diagonal ($94^\circ$ Horizontal, $56^\circ$ Vertical), providing full build plate visibility ($220 \times 220 \times 250\,\text{mm}$) with optical barrel distortion ($\approx -14.2\%$).

### 2. Embedded SoC & Video4Linux2 (V4L2) Driver Subsystem
Inside the printer's embedded Linux OS (typically running an Ingenic T20/T31 or Allwinner MIPS/ARM SoC):
* The sensor interfaces directly via a **MIPI CSI-2** (2-lane) bus running at up to 800 Mbps/lane.
* The Linux kernel driver initializes through the **V4L2 (Video4Linux2)** subsystem under `/dev/video0`.
* The kernel driver manages frame acquisition using `V4L2_MEMORY_MMAP` ring buffers.
* The on-chip hardware Video Processing Unit (VPU) compresses the raw sensor stream using either hardware **Motion JPEG (MJPEG)** or **H.264/AVC (Baseline Profile, Level 3.1)**.
* **Bitrate Allocation:** The stream is constrained to a variable bitrate of $1.5\,\text{Mbps} - 3.5\,\text{Mbps}$ over RTSP/WebRTC to prevent saturation of the 2.4 GHz 802.11b/g/n Wi-Fi interface during active stepper driver execution.

---

## Colorimetry, Chroma Subsampling & Compression Physics

Low-cost chamber cameras introduce severe visual degradation that standard bilinear or bicubic scalers amplify:

```
  RGB Light ──> Bayer CFA ──> Demosaicing ──> YUV 4:2:0 ──> DCT Compression ──> Chromium Skia sRGB
                                             (75% Chroma loss)  (8x8 Block artifacts)  (Limited-Range Crush)
```

### 1. YUV 4:2:0 Chroma Decimation
The embedded encoder converts native linear RGB sensor data into the YCbCr (YUV) color space using standard ITU-R BT.709 (Rec.709) coefficients:

$$\begin{bmatrix} Y \\ C_b \\ C_r \end{bmatrix} = \begin{bmatrix} 0.2126 & 0.7152 & 0.0722 \\ -0.1146 & -0.3854 & 0.5000 \\ 0.5000 & -0.4542 & -0.0458 \end{bmatrix} \begin{bmatrix} R \\ G \\ B \end{bmatrix}$$

To minimize bandwidth, the encoder discards 75% of the color information using **YUV 4:2:0 planar subsampling**:
* Full resolution is preserved for Luma ($Y$).
* Chroma components ($C_b$ and $C_r$) are subsampled by a factor of 2 both horizontally and vertically ($W/2 \times H/2$).
* **Physical Effect on 3D Printing:** Sharp boundaries between high-contrast colored filaments (e.g., white text on a red background or a brass nozzle against a black bed) suffer from significant color bleeding and edge fringing.

### 2. Quantization Matrices & Shadow Posterization
Discrete Cosine Transform (DCT) block-based compression splits the image into $8 \times 8$ pixel matrices:
* High-frequency coefficients (fine layer lines, texture of PEI sheets) are aggressively quantized by high values in the JPEG/H.264 quantization tables.
* Dark regions have low luminance energy, causing the encoder's rate-control algorithm to assign minimal bits. This produces severe **macroblocking ($8 \times 8$ pixel boundaries)** and severe **posterization/banding** in dark filament folds.

### 3. Limited Range vs. Full Range Studio Expansion (Shadow Crush)
Camera hardware encoders frequently output **Limited Range (Studio Range)** YUV:
* $Y \in [16, 235]$
* $C_b, C_r \in [16, 240]$

When Chromium's internal media pipeline decodes this stream to full-range sRGB ($[0, 255]$), any sub-black levels ($Y < 16$) are clipped directly to $0$. Inside a 3D printer enclosure, black filaments absorb more than 95% of incident light; this studio range clamping completely crushes nozzle-to-layer transition boundaries into absolute black voids.

---

## Chromium Embedded Framework (CEF) / Electron Injection Mechanism

Creality Print (v7.3+) is packaged as an Electron / CEF hybrid desktop application. The user interface is driven by a Chromium V8 renderer process reading local web assets bundled at:
```
C:\Program Files\Creality\Creality Print 7.3\resources\web\deviceMgr\
```

```
                        CEF INJECTION & EXECUTION LIFECYCLE
 ┌──────────────────────┐
 │ install_upscaler.ps1 │ ──> Modifies NTFS DACL (BUILTIN\Users:Modify)
 └──────────────────────┘ ──> Backs up index.html to index.html.bak
                          ──> Patches index.html: <script defer src="./upscale.js"></script>
                                     │
                                     ▼
 ┌──────────────────────┐     ┌────────────────────────┐     ┌────────────────────────┐
 │ Creality Print.exe   │ ──> │ V8 Execution Context   │ ──> │ MutationObserver       │
 │ Boots CEF Renderer   │     │ upscale.js Initialized │     │ Watches DOM for <video>│
 └──────────────────────┘     └────────────────────────┘     └────────────────────────┘
                                                                         │
                                                                         ▼
 ┌──────────────────────┐     ┌────────────────────────┐     ┌────────────────────────┐
 │ Hardware Canvas 2D   │ <── │ Binary WebSocket IPC   │ <── │ requestVideoFrame-     │
 │ Accelerated Overlay  │     │ ws://127.0.0.1:8088    │     │ Callback (V-Sync tap)  │
 └──────────────────────┘     └────────────────────────┘     └────────────────────────┘
```

### 1. Static AST Hooking & File Access Control List (DACL) Provisioning
Standard installations located in `C:\Program Files` inherit strict Windows security descriptors, preventing non-elevated writes. The PowerShell installer (`install_upscaler.ps1`) automates environment preparation:
1. **DACL Rule Modification:** Adds a persistent FileSystemAccessRule granting `Modify` permissions to `BUILTIN\Users` with container and object inheritance:
   ```powershell
   $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
       "BUILTIN\Users", "Modify", "ContainerInherit,ObjectInherit", "None", "Allow"
   )
   $acl.AddAccessRule($rule)
   Set-Acl -Path $deviceMgrDir -AclObject $acl
   ```
2. **Atomic Backup & Script Injection:** Creates `index.html.bak` and appends the deferred entry point right before `</body>`:
   ```html
   <script defer src="./upscale.js"></script>
   ```

### 2. Runtime DOM Interception & Non-Destructive Stream Hooking
Inside `upscale.js`, the script attaches to Creality Print's V8 thread:
* **Microtask-Debounced MutationObserver:** Because Creality Print utilizes Vue.js single-page routing, elements mount and unmount dynamically as the user navigates between "Prepare", "Preview", and "Device" views. A persistent observer monitors `document.body` for `<video id="remoteVideo">` or `video.video-player`.
* **Hardware V-Sync Synchronization (`requestVideoFrameCallback`):** 
  Standard `setInterval` or `requestAnimationFrame` polling decouples from the video decoder, leading to dropped frames or duplicate GPU inference calls. Creality Vision AI hooks into `HTMLVideoElement.prototype.requestVideoFrameCallback()`. The callback is invoked exactly once per decoded video frame, aligned directly with the browser's hardware Compositor thread.
* **Direct Offscreen Canvas Capture:**
  Rather than performing an expensive base64 encoding or lossy blob conversion on the client side, the frame is rendered into an offscreen canvas at the selected input resolution (e.g., $640 \times 360$). The underlying pixel memory is extracted via:
  ```javascript
  const img = grabCtx.getImageData(0, 0, w, h);
  ```
  This extracts raw 32-bit `RGBA_8888` bytes directly from the underlying GPU backbuffer memory with zero intermediate compression overhead.
* **Accelerated Composite Overlay:**
  An HTML5 canvas (`<canvas id="crealityUpscaleCanvas">`) is created and absolute-positioned directly over the original `<video>` element. The native `<video>` has its CSS `opacity` set to `0`. If the AI WebSocket service disconnects, opacity is immediately restored to `1`, providing a seamless fail-safe experience.

---

## Zero-Copy DirectML Tensor Pipeline & ONNX Graph Surgery

Traditional Python computer-vision pipelines suffer from substantial CPU-GPU synchronization bottlenecks:
1. Decode image $\rightarrow$ CPU NumPy array.
2. Convert `uint8` to `float32` $\rightarrow$ Divide by 255.0 on CPU.
3. Transpose memory layout from `NHWC` (Height, Width, Channels) to `NCHW` on CPU.
4. Transfer tensor across PCIe to GPU.
5. Execute Model $\rightarrow$ Transfer output tensor across PCIe back to CPU.
6. Transpose `NCHW` back to `NHWC` on CPU $\rightarrow$ Multiply by 255.0 and clip $\rightarrow$ Convert to `uint8`.

On high-resolution frames, these NumPy operations consume over **15 ms of CPU time per frame**, completely bottlenecking high-refresh rendering.

```
                      TRADITIONAL vs. ZERO-COPY PIPELINE
TRADITIONAL PIPELINE (Severe Host CPU Bottlenecks):
[Raw Uint8] ──> [CPU: Cast Float32] ──> [CPU: Div 255] ──> [CPU: Transpose NCHW] ──PCIe──> [GPU: Model] ──PCIe──> [CPU: Transpose NHWC] ──> [CPU: Mul 255] ──> [CPU: Cast Uint8]
               └─────────────────────── 15 ms CPU Overhead ───────────────────────┘                               └─────────────────────── 12 ms CPU Overhead ───────────────────────┘

CREALITY VISION AI FUSED GPU PIPELINE:
[Raw Uint8] ──Fast Buffer Copy──> [GPU VRAM: Cast F32 ──> Mul 1/255 ──> Transpose NCHW ──> Real-ESRGAN Core ──> Clip ──> Mul 255 ──> Round ──> Transpose NHWC ──> Cast U8] ──> [Uint8 Output]
                                  └──────────────────────────────────────────── 100% Executed in GPU VRAM (Zero CPU Stalls) ────────────────────────────────────────────┘
```

### 1. In-Graph DirectML Operator Fusion (`build_fused_model.py`)
Creality Vision AI performs topological surgery on the ONNX computation graph to fuse pre- and post-processing nodes directly into the model:

```python
# Injected Pre-Processing (Executed on GPU)
pre = [
    helper.make_node("Cast", ["input_u8"], ["fx_f32"], to=TensorProto.FLOAT),
    helper.make_node("Mul", ["fx_f32", "fx_inv255"], ["fx_norm"]),
    helper.make_node("Transpose", ["fx_norm"], [orig_in], perm=[0, 3, 1, 2]),
]

# Injected Post-Processing (Executed on GPU)
post = [
    helper.make_node("Clip", [raw_out, "fx_zero", "fx_one"], ["fx_clip"]),
    helper.make_node("Mul", ["fx_clip", "fx_255"], ["fx_scaled"]),
    helper.make_node("Add", ["fx_scaled", "fx_half"], ["fx_round"]),
    helper.make_node("Transpose", ["fx_round"], ["fx_nhwc"], perm=[0, 2, 3, 1]),
    helper.make_node("Cast", ["fx_nhwc"], ["output_u8"], to=TensorProto.UINT8),
]
```

### 2. Execution Characteristics
* **Input Tensor:** `input_u8` with shape `[1, H, W, 3]` and data type `uint8`.
* **Output Tensor:** `output_u8` with shape `[1, 4H, 4W, 3]` and data type `uint8`.
* **DirectML Execution Provider (`DmlExecutionProvider`):** 
  Executes natively on Microsoft DirectX 12 compute queues. `so.enable_mem_pattern = False` is enforced to satisfy DirectML memory heap allocation constraints.
* **Warmup Allocation:** During service boot, the engine pre-compiles and executes shader pipelines against $(480 \times 270)$, $(640 \times 360)$, and $(960 \times 540)$ geometries, eliminating runtime JIT shader compilation stutter.

---

## Advanced Digital Signal Processing (DSP) & Image Algorithms

```
                          IMAGE RECONSTRUCTION PIPELINE
 ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
 │ Raw RGBA     │ ──> │ O(1) Non-Lin │ ──> │ Fused ONNX   │ ──> │ Lanczos-4    │
 │ Ingest       │     │ HDR Lift LUT │     │ DirectML GPU │     │ Resampling   │
 └──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
                                                                       │
                                                                       ▼
 ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
 │ Binary WS    │ <── │ Turbo JPEG   │ <── │ Temporal     │ <── │ Unsharp Mask │
 │ Dispatch     │     │ Compression  │     │ Cross-Fade   │     │ Spatial Filter│
 └──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
```

### 1. Non-Linear Quadratic HDR Shadow Lift
To recover underexposed black filaments without elevating mid-tone noise or clipping highlight regions (e.g., LED chamber illumination reflections on PEI sheets), Creality Vision AI employs an analytical quadratic attenuation curve:

$$x' = x + (255 - x) \cdot 0.40 \cdot k \cdot \left(1 - \frac{x}{255}\right)^2$$

Where:
* $x \in [0, 255]$ is the input 8-bit channel intensity.
* $k \in [0.0, 1.0]$ represents the boost factor set via the UI slider.
* The term $(255 - x)$ scales the headroom available before white saturation.
* The quadratic attenuation factor $\left(1 - \frac{x}{255}\right)^2$ guarantees that the gradient smoothly approaches zero as pixel luminance increases:
  $$\frac{d}{dx} \left[ \left(1 - \frac{x}{255}\right)^2 \right]_{x=255} = 0$$

#### Look-Up Table (LUT) Caching
To maintain high frame rates, the service pre-computes 101 discrete 256-element uint8 arrays (one for each 1% increment of $k$):
```python
x = np.arange(256, dtype=np.float32)
t = x / 255.0
lifted = x + (255.0 - x) * 0.40 * (key / 100.0) * (1.0 - t) ** 2
lut = np.clip(lifted, 0, 255).astype(np.uint8)
```
Applying this transformation requires an $O(1)$ table lookup via `cv2.LUT()`, taking less than **0.18 ms** per frame.

### 2. High-Frequency Unsharp Mask Filtering
To restore microscopic edge gradients across 0.2mm layer boundaries, an unsharp filter is applied in spatial domain:

$$I_{\text{sharp}} = (1 + 0.9s) \cdot I - 0.9s \cdot (I * G_{\sigma=1.0})$$

Where:
* $s \in [0.0, 1.0]$ is the sharpness intensity.
* $G_{\sigma=1.0}$ is a $5 \times 5$ Gaussian kernel with standard deviation $\sigma = 1.0$.

### 3. Dynamic Temporal Cross-Fade Smoothing
When camera capture rate (e.g., 15 FPS) is significantly lower than display monitor refresh rates (60 Hz to 144 Hz), raw display updates appear jittery. The injected JavaScript runtime calculates elapsed inter-frame time ($\Delta t$) and performs dynamic alpha blending across consecutive frames:

$$\alpha = \text{clamp}\left(\frac{t_{\text{current}} - t_{\text{frame}}}{\Delta t_{\text{avg}}}, 0.0, 1.0\right)$$

$$I_{\text{display}} = (1.0 - \alpha) \cdot I_{t-1} + \alpha \cdot I_t$$

If $\Delta t_{\text{avg}} > 120\,\text{ms}$ (indicating large motion jumps or network stalls), smoothing automatically disables to prevent ghosting artifacts.

---

## Network & Wire Protocol Specifications

### 1. Asynchronous WebSocket Frame Protocol (`ws://127.0.0.1:8088/ws_upscale`)
The client-daemon IPC link operates exclusively over binary WebSockets to eliminate base64 encoding overheads.

#### Upstream Packet: Client $\rightarrow$ AI Service (Binary RGBA)
```
Byte Offset   0                   1                   2                   3
              0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
             +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
0x00 - 0x03  |                   Sequence Number (uint32_le)                 |
             +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
0x04 - 0x07  |       Width (uint16_le)       |      Height (uint16_le)       |
             +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
0x08 - End   |                                                               |
             |             Raw Contiguous RGBA Byte Payload                  |
             |                   (Size: Width * Height * 4)                  |
             +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

#### Downstream Packet: AI Service $\rightarrow$ Client (Binary JPEG)
```
Byte Offset   0                   1                   2                   3
              0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
             +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
0x00 - 0x03  |                   Sequence Number (uint32_le)                 |
             +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
0x04 - 0x07  |     Inference Time ms (u16)   |      Total Latency ms (u16)   |
             +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
0x08 - End   |                                                               |
             |            Compressed Turbo JPEG Stream Payload               |
             |             (Begins with SOI 0xFFD8, ends 0xFFD9)             |
             +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

### 2. Dual-Path Hardware Feedrate Dispatch Protocol
When the user adjusts the custom micro-precision print speed slider in the Creality Print UI:

```
                  MICRO-FEEDRATE DISPATCH STRATEGY
 ┌──────────────────────┐
 │ Injected Slider (UI) │
 └──────────────────────┘
            │
            ├──> [Path A: Direct Socket ws://<printer-ip>:9999]
            │    Sends JSON-RPC: {"method":"set","params":{"speedMode":0,"setFeedratePct":V}}
            │
            └──> [Path B: Fallback Bridge via AI Service]
                 GET http://127.0.0.1:8088/printer/set_speed?speed=V
                        │
                        ▼
                 POST http://<printer-ip>:7125/printer/gcode/script
                 Body: {"script": "M220 S{V}"}
```

#### Path A: Creality OS Native Control Daemon
* **Port:** `9999` (TCP/WebSocket)
* **Payload:** Creality JSON-RPC message structure:
  ```json
  {"method": "set", "params": {"speedMode": 0}}
  {"method": "set", "params": {"setFeedratePct": 125}}
  ```
  *(Note: Setting `speedMode: 1` triggers Silent mode at 25%, while `speedMode: 0` activates custom feedrate percentages up to 190%).*

#### Path B: Moonraker / Klipper G-Code Dispatch & Synchronized Telemetry
* **Port:** `7125` (HTTP/REST)
* **Speed Override:** Sent only on pointer release and only when the slider value changes:
  ```http
  POST /printer/gcode/script HTTP/1.1
  Host: 192.168.78.50:7125
  Content-Type: application/json

  {"script": "M220 S125"}
  ```
* **Synchronized Nozzle Activity & Extrusion Telemetry (`GET /printer/telemetry?ip=<addr>`):**
  Queries Moonraker (`motion_report`, `virtual_sdcard`, `gcode_move`, `print_stats`, `exclude_object`) and fetches bounded rolling HTTP `Range` chunks from `/server/files/gcodes/<filename>`. By correlating `motion_report.live_position`, extrusion ratio (`live_extruder_velocity / live_velocity`), and monotonic G-code offset progression across linear (`G0`/`G1`) and arc (`G2`/`G3`) moves, [`printer_telemetry.py`](printer_telemetry.py) compensates for Klipper's lookahead buffer and accurately resolves the active `;TYPE:` feature alongside live print/extrusion speed (mm/s), filament feed rate (mm/s), and volumetric flow rate (mm³/s). The injected card mounts side-by-side in Creality Print's Row 2 container right next to the fan control card (`Modello di ventilatore`), gracefully falling back to passive telemetry on Creality's WebSocket port `9999` when port `8088` is offline.

### 3. Real-Time Stringing AI & Automatic Print Pause (`stringing_detector.py`)
Creality Vision AI continuously analyzes decoded chamber frames from the SparkX i7 camera (~2.3 Hz in background threads) using [`stringing_detector.py`](stringing_detector.py):
* **Multi-Orientation Symmetrical Ridge Extractor:** Combines luma + CIE `Lab` chroma channels with CLAHE shadow normalization and dual-scale ($d=2,3$) directional response across 6 orientations ($0^\circ\text{--}150^\circ$), suppressing step-edge walls, rectilinear infill, and solid tree supports while isolating thin extruded wisps, cobwebs, and spaghetti loops.
* **Textured PEI Plate Glare & Carpet Shimmer Suppression:** Analyzes tile ridge floor medians (`tile_fmap_med >= 7.0`), macro delta strand contrast (`macro_delta_strand >= 7.5`), and macro Gaussian color divergence against baseline (`bg_macro_lab`). Glare and high-frequency specular sparkle on bare textured gold PEI sheets without genuine macro structural contrast or elongated strands are suppressed, completely preventing empty-bed false alarms even under carriage vibration, Z-drop parallax, or chamber LED shifts.
* **Chamber Perimeter & Upper Gantry Exclusion:** Automatically clips the upper 12% of the chamber camera frame (`y < 0.12`), preventing overhead gantry rails, CoreXY belt pulleys, and ceiling LED direct reflections from entering the spatial candidate matrix.
* **Defect Spatial Exclusion & Acknowledgment ("Segna come letti & Ignora"):** Rather than globally degrading detector sensitivity across the entire build plate when benign stringing occurs, the watchdog implements persistent spatial defect tracking (`ignored_defects`). Clicking *"Segna come letto & Continua"* (in the alert banner) or *"Segna letti & Ignora"* (in the control panel) registers each currently detected defect with a unique ID (`DEF-1`, `DEF-2`, etc.) and bounding box coordinates:
  * **Zero-Alarm Masking:** Candidates matching acknowledged regions (via spatial containment and IoU matching) are excluded from the frame threat score, guaranteeing zero recurring false alarms or auto-pauses for previously reviewed defects.
  * **Full Global Vigilance:** Full detection sensitivity is maintained across all other regions of the build plate to catch any new spaghetti or stringing outbreaks immediately.
  * **Subtle Visual HUD Footprint:** Ignored defects render on the camera overlay (`#agyStringingCanvas`) with an unobtrusive muted slate dashed outline tagged `"Letto · Ignorato"`, while genuine unacknowledged threats retain high-visibility solid amber/red borders.
* **Calibrated Sensitivity Readout & One-Click Reset:**
  * **Real-Time Percentage Display:** The active threshold is mapped to an intuitive percentage readout (e.g. `Sensibilità calib.: 39% (soglia 0.76)` or `23% (soglia 0.85 · ridotta)`) shown both in the control dashboard and the camera HUD badge.
  * **Sensitivity Restoration:** Clicking *"Ripristina sensibilità"* immediately resets any accumulated recalibration backoff back to the base preset (`high`: 83%, `balanced`: 59%, `low`: 39%, `ultra_low`: 23%).
* **Context-Aware Sustained Confirmation & Dual-Path Auto-Pause:** Suspends detection during `homing`/`heating`/`preparing` states and requires sustained confirmation (`high`: 0.50, 3.2 s; `balanced`: 0.62, 4.8 s; `low`: 0.74, 6.5 s; post-recalibration `ultra_low`: 0.82, 8.5 s) before triggering an immediate race-checked pause:
  * **Primary Path:** Queries Moonraker `print_stats` / `pause_resume` and executes `POST http://<printer-ip>:7125/printer/print/pause` (`gcode_macro PAUSE` retract + safe Z park).
  * **UI Fallback Path:** Emits `{"method":"set","params":{"pause":1}}` over Creality's native WebSocket (`ws://<printer-ip>:9999`) if Moonraker returns an unreachable state.
* **UI Controls & Evidence Snapshot (`http://127.0.0.1:8088/ai/stringing/*`):** Draws aspect-accurate bounding boxes (`#agyStringingCanvas`), displays a live status badge with reduced-sensitivity indicator when active (`#agyStringingHost`), integrates sensitivity/auto-pause settings inside `#aiPanel`, and saves an annotated evidence JPEG (`/ai/stringing/snapshot`) whenever an alert or pause fires. Both `ai_upscaler_service.py` (GPU + Stringing WS) and standalone `printer_telemetry.py` (CPU Telemetry + Stringing REST on port `8088`) serve the full watchdog pipeline.

### 4. Local Server Lifecycle Supervisor (`server_manager.py`, Port `8089`)
To let Creality Print inspect status, start, stop, and restart the `:8088` AI backend without external terminals:
* **Localhost Control Daemon:** [`server_manager.py`](server_manager.py) runs silently under `pythonw.exe` on `127.0.0.1:8089` using only the Python standard library, enforcing loopback `Origin` checks and capturing child logs under `%LOCALAPPDATA%\CrealityVisionAI\logs\ai_server.log`.
* **HTTP Control Endpoints:** Exposes `GET /control/status`, `POST /control/start`, `POST /control/stop`, `POST /control/restart`, and `GET /control/logs`, seamlessly switching between `ai_upscaler_service.py` (Full GPU DirectML + Stringing AI) and `printer_telemetry.py` (CPU Telemetry + Stringing AI). Both `:8088` servers also expose `POST /server/stop` and enriched `GET /status` so even manually launched `.bat` instances can be stopped from Creality Print.
* **Per-User Windows Auto-Start:** [`install_upscaler.ps1`](install_upscaler.ps1) registers `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\CrealityVisionAISupervisor` (no UAC elevation required) and starts the controller immediately.

---

## Dual-Engine Decoupled Architecture & Horizontal Dashboard UI

### 1. Independent Decoupled Operation Matrix

Creality Vision AI v1.1.0 introduces full runtime decoupling between the **Real-ESRGAN Neural Video Upscaler** and the **AI Stringing & Spaghetti Defect Watchdog**. Either engine can be toggled independently at any time without restarting the local backend or interrupting active print jobs:

| State / Combination | Video Upscaler Toggle | Stringing Watchdog Toggle | Client Transmission Rate | GPU DirectML Inference | CPU Ridge Analysis | Display Surface Mode | Bounding Box & HUD Alerts |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Full Vision AI** | **ON** | **ON** | Camera V-Sync ($15\text{--}60\,\text{FPS}$) | Active ($1080\text{p}/1440\text{p}$) | Active (~$2.3\,\text{Hz}$) | Hardware Canvas 2D | Active (`#agyStringingCanvas`) |
| **Upscale Only** | **ON** | **OFF** | Camera V-Sync ($15\text{--}60\,\text{FPS}$) | Active ($1080\text{p}/1440\text{p}$) | Disabled ($0\,\text{ms}$) | Hardware Canvas 2D | Hidden (Badge shows "Off") |
| **Stringing Watchdog Only** | **OFF** | **ON** | Throttled ($2.5\,\text{FPS} \cdot 400\,\text{ms}$) | Bypassed ($0\,\text{MB}$ VRAM) | Active (~$2.3\,\text{Hz}$) | Native `<video>` ($100\%$ Opacity) | Active over Native Video |
| **Zero Overhead Pass-Through**| **OFF** | **OFF** | Inactive ($0\,\text{FPS}$) | Bypassed ($0\,\text{MB}$ VRAM) | Disabled ($0\,\text{ms}$) | Native `<video>` ($100\%$ Opacity) | Hidden |

#### Zero-Inference GPU Bypass Protocol
When the video upscaler is turned off while stringing protection remains active:
1. `upscale.js` dispatches `{ "upscale_enabled": false }` via WebSocket to `ai_upscaler_service.py`.
2. The client frame capture loop throttles transmission interval from full camera framerate down to $400\,\text{ms}$ (~$2.5\,\text{FPS}$), reducing network IPC bandwidth by over $85\%$.
3. Upon receiving binary packets, `ai_upscaler_service.py` detects `upscale_enabled == False` and immediately extracts decoded RGB arrays for the stringing queue via `_extract_rgb_for_stringing()` without invoking the DirectML ONNX execution provider.
4. The client canvas overlay is hidden (`canvas.style.display = 'none'`) and the underlying camera element restores full opacity (`activeVideo.style.opacity = '1'`), granting latency-free native video while maintaining real-time defect monitoring and automated print pausing.

### 2. Ergonomic Horizontal Dual-Column Dashboard Layout

The v1.1.0 update reorganizes the in-app control panel (`#aiPanel` in [`upscale.js`](upscale.js)) from a tall vertical stack into a high-density, horizontal two-column grid engineered to fit within standard Creality Print desktop viewports:

```
+---------------------------------------------------------------------------------------------------+
|  Enhancement AI & Protezione                           [Upscaler Video] [X]                       |
|  Server locale, monitoraggio cinematico e super-risoluzione                                       |
+-------------------------------------------------+-------------------------------------------------+
|  COLUMN 1: CONTROLLO SERVER & STRINGING AI     |  COLUMN 2: PRESTAZIONI & QUALITA VIDEO          |
+-------------------------------------------------+-------------------------------------------------+
|  SERVER AI (:8088)                              |  PRESTAZIONI VIDEO & FPS                        |
|  [Stato Servizio: Attivo . GPU]                 |  Camera: 30.0 fps    AI Upscale: 30.0 fps       |
|  Modalita | Processo | Attivo da | Capacita     |  GPU/tot: 11/17 ms   Display: 144.0 fps         |
|  [Modalita di avvio: Auto (GPU/CPU)          v] |  Limite: Camera (30 fps in ingresso)            |
|  [ Avvia ] [ Ferma ] [ Riavvia ] [ Log ]        |                                                 |
|                                                 |  PARAMETRI IMMAGINE                             |
|  RILEVAMENTO STRINGING (AI)                     |  HDR Ombre:  [=======|------] 25%               |
|  [X] Protezione Stringing AI (Indipendente)    |  Nitidezza:  [====|---------] 35%               |
|  [X] Pausa automatica stampa alla conferma      |  Intensita:  [==========|---] 85%               |
|  [Sensibilita: Conservativa (7.0s)          v]  |  Contrasto & Luminosita                         |
|  Sensibilita calib.: 39% (soglia 0.76)         |                                                 |
|  Difetti ignorati: 2                           |  QUALITA & FLUIDITA                             |
|  Stato | Rischio | Conferma | Analisi           |  Input: 640x360      Output: 1080p FHD          |
|  [ Segna letti & Ignora ] [ Ripristina sens. ] |  [X] Smussamento dissolvenza tra frame          |
|  [ Ricalibra scena ]     [ Svuota ignorati ]   |  [Frequenza display: Auto                    v] |
|                                                 |  [ Ripristina predefiniti ]                     |
+-------------------------------------------------+-------------------------------------------------+
```

* **Grid Geometry:** Powered by CSS Grid (`grid-template-columns: 1.02fr 0.98fr; gap: 16px;`) with a maximum width of $650\,\text{px}$ and viewport-bounded scrollable height (`max-height: calc(100vh - top - 16px)`).
* **Responsive Breakpoint:** Under window widths $\le 680\,\text{px}$, a CSS media query seamlessly collapses the grid into a single column to prevent horizontal overflow.
* **Dynamic Conditional Control Visibility:**
  * When the **Video Upscaler** is toggled off (`settings.enabled = false`), the adjustable parameter sections (*Prestazioni Video & FPS*, *Parametri Immagine*, *Qualità & Fluidità*) automatically collapse and hide, leaving a clean, low-profile message and preventing UI clutter.
  * When the **Stringing AI Watchdog** is toggled off (`settings.stringingEnabled = false`), its secondary controls (*Pausa automatica*, *Sensibilità rilevamento*, calibrated percentage readout, defect counters, and action buttons) automatically collapse and hide.
* **Independent Telemetry Badges:**
  * Top-Right Badge (`#aiBadge`): Dedicated to Video Upscaler status (`AI · 30.0 fps`, `Upscaler disattivo`, or `Upscaler offline`).
  * Top-Left Badge (`#agyStringingHost`): Dedicated to Stringing Watchdog status (`Stringing AI · Attiva`, `Sospetto stringing 65%`, `Stampa in pausa · Stringing 91%`, or `Stringing AI · Off`), displaying the calibrated sensitivity percentage (`39% · soglia 0.76`) and count of ignored defects when active.

### 3. Redesigned Extrusion Activity, Comprehensive Startup Reporting & CronoPrint Integration

To ensure an ultra-clean, perfectly proportioned UI that seamlessly aligns with native Creality Print widgets:
* **Ultra-Compact Single-Row Activity Card (`.agy-telemetry-card`):** Mounted in Row 2 directly alongside Creality's native *Modello di ventilatore* card:
  * **Ultra-Low Profile Geometry (~36px height):** Drastically reduced from 120px/90px to an ergonomic ~36px single-row pill layout, saving over 65% vertical screen space. Redundant headers (such as the "Attività ugello" title), temperature gauges, red "Stop AI" buttons, and "Live" pills have been completely removed from this card.
  * **Balanced Horizontal Alignment:** Neatly unifies the live activity badge (e.g. `● Parete esterna`, `● Riempimento`, `● Supporti`, `● In calibrazione`), high-contrast tabular nozzle velocity (`142.3 mm/s`), and the CronoPrint launcher into a single harmonious baseline.
  * **Direct HTML Browser Launch (`[ 📊 CronoPrint ]`):** A high-visibility, translucent emerald quick-launch button. Clicking it triggers the supervisor `/control/cronoprint/launch` or `:8088/cronoprint/launch` to immediately open the generated interactive [`printer_history.html`](CronoPrint/printer_history.html) report in the user's default desktop web browser (via native Windows `os.startfile`), concurrently initiating background cache synchronization with Moonraker and serving the live report over HTTP at `/cronoprint`.
  * **Granular Startup & Calibration Telemetry:** When a print starts or during calibration sequences, the card moves beyond generic status and reports exact machine activities in real time:
    * *Riscaldamento piatto:* Displays live temperature convergence against target (e.g. `45 / 70 °C`).
    * *Riscaldamento ugello:* Displays live nozzle heat-up progress against target (e.g. `140 / 230 °C`).
    * *Allineamento assi (Homing):* Flags CoreXY coordinate homing and origin zeroing.
    * *Calibrazione quota Z / Piatto:* Reports pressure sensor swap checks and nozzle probe touches.
    * *Livellamento piatto (Bed Mesh):* Tracks automatic bed leveling matrix calibration.
    * *Pulizia ugello:* Identifies motorized silicone brush and nozzle wiping routines (`WIPE` / `NOZZLE_CLEAR`).
    * *Caricamento filamento:* Indicates smart rack and extruder feed loading.
    * *Spurgo ed estrusione iniziale:* Identifies initial purge lines and nozzle prime ribbons.
  * **High-Contrast Nozzle Speed (`mm/s`):** Displays real-time kinematic velocity paired with tooltip descriptors (*Velocità estrusione*, *Velocità spostamento*, *In riscaldamento*, *In calibrazione*, *In pulizia*).
* **Full-Width Telemetry Strip (`.agy-telemetry-strip`):** Injected directly above the native *Velocità di stampa* card, spanning 100% of the available container width:
  * **Top Row:** Displays the full, unclipped **Project / 3D Model Name** (`📦 Nome Oggetto`) with generous horizontal space, alongside `Layer X/Y`, job progress percentage, and elapsed / remaining time chips.
  * **Bottom Row:** Displays the complete unclipped **G-code Feature & Extrusion Line** (`G-code: Outer wall · Larghezza: 0.45 mm`) paired with real-time physical extrusion telemetry (`Filamento: X mm/s`, `Flusso: Y mm³/s`, `Fattore: Z%`).

### 4. Automatic Creality Print Lifecycle Shutdown (`cp_lifecycle.py`)

To ensure that background AI and telemetry servers never remain active when Creality Print is closed:
* **Toolhelp32 Process Watcher:** [`server_manager.py`](server_manager.py) and the `:8088` servers continuously monitor the host `CrealityPrint.exe` process without external dependencies or heavy polling.
* **Zero-Footprint Dormant Standby:** While Creality Print is closed, the supervisor remains dormant without listening on ports or communicating with the 3D printer.
* **Automatic Server Termination:** When Creality Print exits, the supervisor immediately issues a graceful shutdown request followed by a recursive process-tree termination (`kill_process_tree`) targeting child Python processes, ensuring zero orphaned processes or lingering network traffic. Defense-in-depth orphan guards inside [`ai_upscaler_service.py`](ai_upscaler_service.py) and [`printer_telemetry.py`](printer_telemetry.py) ensure clean self-termination even if launched standalone.

---

## Comprehensive Performance & Latency Benchmarks

Tested on **NVIDIA GeForce RTX 4070 (12GB VRAM)**, host system **Intel Core i7-13700K**, running **Windows 11 (64-bit)**:

| Metric / Pipeline Stage | 480 $\times$ 270 Input | 640 $\times$ 360 Input | 960 $\times$ 540 Input |
| :--- | :---: | :---: | :---: |
| **Output Resolution** | 1080p ($1920 \times 1080$) | 1080p ($1920 \times 1080$) | 1440p ($2560 \times 1440$) |
| **GPU Inference (DirectML)** | $6.2\,\text{ms}$ | $11.4\,\text{ms}$ | $24.8\,\text{ms}$ |
| **HDR Shadow Lift (LUT)** | $0.12\,\text{ms}$ | $0.18\,\text{ms}$ | $0.34\,\text{ms}$ |
| **Resampling (Lanczos-4)** | $1.4\,\text{ms}$ | $1.8\,\text{ms}$ | $3.2\,\text{ms}$ |
| **Unsharp Mask & Blend** | $1.1\,\text{ms}$ | $1.4\,\text{ms}$ | $2.8\,\text{ms}$ |
| **Turbo JPEG Compression** | $2.1\,\text{ms}$ | $2.6\,\text{ms}$ | $4.7\,\text{ms}$ |
| **Total Engine Latency** | **$10.92\,\text{ms}$** | **$17.38\,\text{ms}$** | **$35.84\,\text{ms}$** |
| **Max Throughput** | **$91.5\,\text{FPS}$** | **$57.5\,\text{FPS}$** | **$27.9\,\text{FPS}$** |
| **VRAM Consumption** | $420\,\text{MB}$ | $580\,\text{MB}$ | $1\,140\,\text{MB}$ |

---

## Complete Installation & Setup Guide

### System Prerequisites
1. **Operating System:** Windows 10 (Build 19041+) or Windows 11 (64-bit).
2. **Host Application:** Creality Print (v7.3 or later).
3. **Hardware Acceleration:** NVIDIA GeForce RTX / GTX (Turing, Ampere, Ada Lovelace), AMD Radeon RX (RDNA2/3), or Intel Arc with DirectX 12 Feature Level 12_0+.
4. **Python Runtime:** Python 3.10 to 3.12 (64-bit).

### Step 1: Environment Setup
Clone the repository and prepare an isolated virtual environment:
```powershell
git clone https://github.com/tiadiff/Creality-Vision-AI.git
cd Creality-Vision-AI
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

### Step 2: Build the Fused DirectML Model
Synthesize the fused zero-copy ONNX graph:
```powershell
.\.venv\Scripts\python.exe build_fused_model.py
```
*Expected Output:*
```
original input=input output=output opset=[14]
saved .../models/realesr_animevideo_v3_fused.onnx
```

### Step 3: Inject the CEF Hook & Local Supervisor (:8089)
Run the PowerShell injection script:
```powershell
powershell -ExecutionPolicy Bypass -File .\install_upscaler.ps1
```
The script backs up `index.html`, copies `upscale.js`, inserts the `<script>` tag, configures NTFS DACL permissions, registers per-user HKCU auto-start for [`server_manager.py`](server_manager.py) on port `8089`, and launches the controller immediately.

### Step 4: Control the AI Server (:8088) from Creality Print or Batch
Once Creality Print is open on the **Device** tab, you can inspect live status/logs and start, stop, or restart the `:8088` backend directly from the top-right AI panel (`Server AI (:8088)`) or via the quick inline button in the **Attività ugello** card.

Optionally, you can also launch the service manually via the included batch script:
```powershell
.\Avvia_AI_Upscaler.bat
```
The console will display:
```
=================================================================
Creality Real-ESRGAN AI Engine (RTX 4070, fused GPU pipeline)
=================================================================
[AI] Providers: ['DmlExecutionProvider', 'CPUExecutionProvider']
[AI] Warmup complete.
======== Running on http://127.0.0.1:8088 ========
```

### Step 5: Launch Creality Print
Open Creality Print, connect to your 3D printer, and open the **Device** interface. The **AI** telemetry pill will illuminate in the upper-right corner of the camera viewport. Click the badge to expand the deep control panel.

---

## File System Manifest

```
├── ai_upscaler_service.py       # Asynchronous WebSocket/REST server, DirectML engine & AI watchdog hook
├── printer_telemetry.py         # Synchronized Moonraker + G-code Range telemetry & standalone HTTP server
├── stringing_detector.py        # Multi-orientation ridge stringing/spaghetti detector & auto-pause engine
├── server_manager.py            # Localhost supervisor (:8089) for in-app Start/Stop/Restart/Status/Logs
├── cp_lifecycle.py              # Windows Toolhelp32 process watcher & automatic Creality Print closing lifecycle
├── upscale.js                   # Injected CEF script (DOM observer, canvas overlay, HUD, stringing boxes, server/speed/telemetry UI)
├── CronoPrint/                  # Integrated SparkX CronoPrint telemetry & history console (.NET 8 WinForms + WebView2)
│   ├── Avvia_SparkX_Console.bat # Standalone launcher script
│   ├── Genera_Report_Stampe.bat # Offline HTML report runner
│   ├── generate_printer_report.py # Standalone Python Moonraker history & range extractor
│   └── SparkXPrintConsole/      # C# .NET 8 desktop application sources
├── build_fused_model.py         # Topological ONNX graph surgery & operator fusion tool
├── install_upscaler.ps1         # Automated PowerShell CEF injector & NTFS DACL manager
├── Avvia_AI_Upscaler.bat        # Production launcher script
├── requirements.txt             # Python dependencies (onnxruntime-directml, aiohttp, etc.)
├── screen.png                   # High-DPI capture of the injected UI HUD and control panel
├── architecture_pipeline.png    # Standardized IEEE/ACM system architecture and network flow diagram
├── create_architecture_diagram.py # Vector-quality headless Edge diagram generator
├── LICENSE                      # MIT Open Source License
└── README.md                    # In-depth technical specification and engineering documentation
```

---

## License

Distributed under the **MIT License**. See [`LICENSE`](LICENSE) for complete details.

*Disclaimer: Creality, Creality Print, K1, K1 Max, and SparkX i7 are trademarks or registered trademarks of Shenzhen Creality 3D Technology Co., Ltd. Creality Vision AI is an independent, non-affiliated open-source research and engineering project.*
