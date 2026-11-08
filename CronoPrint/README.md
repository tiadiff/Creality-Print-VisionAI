# Creality SparkX CronoPrint: Telemetry Console & Print History Suite

### High-Performance Desktop Telemetry, Range-Based G-Code Inspection & Real-Time Monitoring for Creality 3D Printers

**Creality SparkX CronoPrint** is a high-performance desktop application (.NET 8 WinForms + embedded Chromium WebView2) and telemetry analytics suite engineered specifically for the **Creality SparkX i7** and **K1 / K1 Max** CoreXY ecosystem running Moonraker and Klipper firmware.

The suite provides real-time print telemetry, integrated live chamber video monitoring, cost accounting, and ultra-fast G-code metadata extraction directly from remote printer storage without downloading multi-megabyte print files.

---

## Architecture & Range-Based Data Flow

Instead of saturating Wi-Fi networks by transferring full G-code files, CronoPrint leverages parallel HTTP byte-range requests throttled by semaphores:

```
                             CRONOPRINT ARCHITECTURE
 ┌──────────────────────┐         HTTP GET (List)          ┌───────────────────────────┐
 │ Moonraker History DB │ ───────────────────────────────> │ C# .NET 8 Telemetry Core  │
 └──────────────────────┘                                  │ (Parallel HTTP Range Sem) │
 ┌──────────────────────┐    HTTP Range [0..45KB] Thumb    │                           │
 │ G-Code Storage       │ ───────────────────────────────> │  - Base64 PNG Extraction  │
 │ (Remote Filesystem)  │ ──> HTTP Range [EOF-76KB..EOF] ─>│  - Slicer Config Parsing  │
 └──────────────────────┘                                  └─────────────┬─────────────┘
 ┌──────────────────────┐         WebRTC Stream                  Bidirectional Bridge
 │ Chamber Live Camera  │ ───────────────────────────────>         (PostMessage)
 └──────────────────────┘                                                │
                                                           ┌─────────────▼─────────────┐
                                                           │ Chromium WebView2 UI View │
                                                           │ (Dark High-Contrast Theme)│
                                                           └───────────────────────────┘
```

---

## Core Capabilities

### 1. Ultra-Fast Range-Based G-Code Metadata Extraction
* **Embedded Thumbnail Extraction:** Inspects the first 45 KB of G-code headers to extract high-resolution (up to 260x260) embedded base64 PNG thumbnails in milliseconds.
* **Slicer Profile Parsing:** Reads the final 76 KB of G-code footers to parse exact slicing parameters from Creality Print, OrcaSlicer, and PrusaSlicer:
  * Nozzle diameter and layer height (first layer and normal layers)
  * Infill density and infill geometry pattern
  * Wall loops / perimeter count and maximum Z-height
  * Ironing toggle and surface ironing type
  * Support enable flag and structure classification (Standard / Tree)
  * Extruder and heated bed operational temperatures

### 2. Live Chamber Camera & Fullscreen Modal
* Directly embeds the printer's WebRTC video stream (port 8000) alongside active print metrics.
* Single-click full-screen modal viewport with backdrop blur for detailed first-layer and print progress monitoring.

### 3. Comprehensive Print Telemetry Views
* **Horizontal Feed View:** Production-oriented list format displaying thumbnails, project filenames, slicer profiles, execution dates, duration, filament consumption, and calculated cost.
* **Plate Gallery View:** Responsive visual card grid focusing on 3D rendered thumbnails and rapid visual status assessment.
* **Data Table View:** High-density tabular layout with sorting, text search filtering, and nozzle/support segmentation.
* **Slide-Over Inspector Drawer:** Detailed side panel exposing deep slicer parameters, mechanical kinematics, temperatures, and one-click remote reprint dispatch via Moonraker.

### 4. Extended Italian Localization & Typographic Contrast
* Italian extended date format (e.g., `Lunedi 7 Gennaio 2026, 17:32`).
* Distinct, unambiguous badge labels for `Ugello` (Nozzle), `Layer` (Height), `Infill` (Density/Pattern), and `Supporti` (Supports).

### 5. Standalone HTML Snapshot Generator
* Includes a lightweight Python script (`generate_printer_report.py`) and Windows batch runner (`Genera_Report_Stampe.bat`) to generate offline, self-contained HTML reports (`printer_history.html`) without compiling .NET binaries.

---

## Installation & Setup

### Prerequisites
* Windows 10 or Windows 11 (64-bit)
* [.NET 8.0 SDK](https://dotnet.microsoft.com/download/dotnet/8.0) or .NET 8.0 Desktop Runtime
* [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/) (pre-installed on Windows 11 and modern Windows 10)

### Quick Start (Desktop Console)
Launch the suite via the included batch script:
```powershell
.\Avvia_SparkX_Console.bat
```
*(This automatically runs `Genera_Report_Stampe.bat` first to extract print history, update and open `printer_history.html` in your default browser, and then seamlessly launches `SparkXPrintConsole.exe` once report generation completes.)*

Alternatively, build and run from source in Release configuration:
```powershell
dotnet build SparkXPrintConsole -c Release
.\SparkXPrintConsole\bin\Release\net8.0-windows\SparkXPrintConsole.exe
```

### Standalone Python Report Generator
If you prefer generating a static HTML snapshot without the desktop application:
```powershell
python generate_printer_report.py
```
This queries Moonraker, extracts G-code metadata, and produces `printer_history.html`.

---

## File System Manifest

```
├── SparkXPrintConsole/             # C# .NET 8 WinForms + WebView2 desktop application
│   ├── Program.cs                  # High-DPI application entry point
│   ├── MainForm.cs                 # WinForms host window with DWM dark titlebar & WebView2 bridge
│   ├── PrinterService.cs           # Concurrent Moonraker HTTP range parser & live telemetry engine
│   ├── Models.cs                   # Strongly-typed print job & telemetry entities
│   ├── AppSettings.cs              # JSON configuration persistence
│   └── wwwroot/index.html          # High-contrast dark-mode dashboard UI (emerald accents)
├── Avvia_SparkX_Console.bat        # Launcher script: runs report generator first, then opens console
├── generate_printer_report.py      # Standalone Python Moonraker history & range extractor
├── Genera_Report_Stampe.bat        # Batch script to generate standalone printer_history.html
├── printer_history.html            # Standalone telemetry report snapshot
├── .gitignore                      # Git exclusion rules for .NET and temp files
├── LICENSE                         # MIT Open Source License
└── README.md                       # Complete technical specification
```

---

## License

Distributed under the **MIT License**. See [`LICENSE`](LICENSE) for complete details.

*Disclaimer: Creality, Creality Print, K1, K1 Max, and SparkX i7 are trademarks or registered trademarks of Shenzhen Creality 3D Technology Co., Ltd. Creality SparkX CronoPrint is an independent, non-affiliated open-source software project.*
