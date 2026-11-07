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
        Additionally, the suite injects a micro-precision feedrate tuning slider directly into the native Creality Print interface, communicating bi-directionally with the printer via low-overhead WebSockets and Moonraker/Klipper G-code APIs.
      </p>
      <ul>
        <li><b>Zero-Copy DirectML Tensor Pipeline:</b> Normalization, layout transposition, clipping, and quantisation fused directly into the ONNX graph executed on GPU VRAM.</li>
        <li><b>Hardware V-Sync Frame Synchronization:</b> Native <code>requestVideoFrameCallback()</code> stream tap eliminating frame tearing, duplicated inference, and CPU polling.</li>
        <li><b>Specialized HDR Shadow Lift:</b> Non-linear quadratic attenuation LUT recovering underexposed nozzle and bed zones without highlight clipping.</li>
        <li><b>Dual-Path Micro-Feedrate Control:</b> Direct Creality OS socket communication (port 9999) with seamless Moonraker/Klipper G-code bridge (port 7125).</li>
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

#### Path B: Moonraker / Klipper G-Code Dispatch
* **Port:** `7125` (HTTP/REST)
* **Payload:** Direct dispatch to Klipper motion queue:
  ```http
  POST /printer/gcode/script HTTP/1.1
  Host: 192.168.78.50:7125
  Content-Type: application/json

  {"script": "M220 S125"}
  ```

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

### Step 3: Inject the CEF Hook into Creality Print
Run the PowerShell injection script:
```powershell
powershell -ExecutionPolicy Bypass -File .\install_upscaler.ps1
```
The script will back up `index.html`, copy `upscale.js`, insert the `<script>` tag, and update NTFS DACL permissions.

### Step 4: Start the Super-Resolution Engine
Launch the service via the included batch script:
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
├── ai_upscaler_service.py       # Asynchronous WebSocket/REST server & DirectML inference engine
├── upscale.js                   # Injected CEF script (DOM observer, canvas overlay, HUD, speed hook)
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
