"""
SparkX i7 / Creality Print History & Cost Console
Engineering master-detail console. Eliminates generic dashboard slop with horizontal data rows,
integrated telemetry strip, precision CAD vector placeholders, and a slide-over technical inspector.
"""

import os
import sys
import json
import re
import math
import time
import datetime
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

PRINTER_IP = "192.168.78.50"
PRINTER_PORT = 7125
DEFAULT_COST_PER_KG = 20.0  # Euro / kg

# Technical 3D Printer CAD Placeholder Vector
PRINTER_SVG_PLACEHOLDER = """<svg class="placeholder-svg" viewBox="0 0 80 80" fill="none" xmlns="http://www.w3.org/2000/svg">
  <path d="M16 66h48M20 66V16h40v50M16 32h48" stroke="#2d3340" stroke-width="2" stroke-linecap="round"/>
  <rect x="34" y="27" width="12" height="10" rx="1.5" fill="#14171f" stroke="#4b5563" stroke-width="1.5"/>
  <path d="M38 37l2 4.5h-4l2-4.5z" fill="#10b981"/>
  <path d="M40 21v6" stroke="#10b981" stroke-width="1.5" stroke-dasharray="2 2"/>
  <path d="M40 46l12 6.5-12 6.5-12-6.5L40 46z" fill="#181e28" stroke="#10b981" stroke-width="1.5"/>
  <path d="M28 52.5v7l12 6.5v-7l-12-6.5z" fill="#11141c" stroke="#374151" stroke-width="1.5"/>
  <path d="M52 52.5v7l-12 6.5v-7l12-6.5z" fill="#161b24" stroke="#374151" stroke-width="1.5"/>
  <line x1="28" y1="56" x2="40" y2="62.5" stroke="#252c38" stroke-width="1"/>
  <line x1="40" y1="62.5" x2="52" y2="56" stroke="#252c38" stroke-width="1"/>
</svg>"""

def clean_project_name(filename):
    """Extract clean project name from filename."""
    name = os.path.basename(filename)
    name = re.sub(r'_plate_\d+\.gcode$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\.gcode(?:\.3mf)?$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'_(?:PLA|PETG|ABS|TPU|ASA)(?:_[0-9hms]+)?$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\.stl$', '', name, flags=re.IGNORECASE)
    name = name.replace('_', ' ').replace('+', ' ').strip()
    return name if name else os.path.basename(filename)

def parse_time_str_to_seconds(time_str):
    """Parse strings like '15h 39m 16s', '20m 15s', '45s' to seconds."""
    if not time_str or time_str == "N/D":
        return None
    seconds = 0
    h_m = re.search(r'(\d+)\s*h', time_str)
    m_m = re.search(r'(\d+)\s*m', time_str)
    s_m = re.search(r'(\d+)\s*s', time_str)
    if h_m:
        seconds += int(h_m.group(1)) * 3600
    if m_m:
        seconds += int(m_m.group(1)) * 60
    if s_m:
        seconds += int(s_m.group(1))
    return seconds if seconds > 0 else None

def format_duration(seconds):
    """Format seconds into readable Xh Ym Zs string."""
    if not seconds or seconds < 0:
        return "0s"
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    rem_sec = seconds % 60
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes}m")
    if hours == 0 and minutes < 5:
        parts.append(f"{rem_sec}s")
    return " ".join(parts) if parts else "0s"

def extract_file_details(ip, port, rel_path, file_size=0):
    """Extract thumbnail, slicer, layers, and deep config metadata using HTTP Range requests."""
    url = f"http://{ip}:{port}/server/files/gcodes/" + urllib.parse.quote(rel_path)
    info = {
        "thumbnail": None,
        "slicer": None,
        "total_layers": None,
        "max_z": None,
        "filament_g_est": None,
        "filament_cost_est": None,
        "time_est": None,
        "filament_name": None,
        "filament_type": None,
        "nozzle_temp": None,
        "first_layer_nozzle_temp": None,
        "bed_temp": None,
        "first_layer_bed_temp": None,
        "density": 1.24,
        "print_settings_id": None,
        "printer_settings_id": None,
        "nozzle_diameter": None,
        "layer_height": None,
        "first_layer_height": None,
        "infill_density": None,
        "infill_pattern": None,
        "wall_loops": None,
        "enable_support": None,
        "support_type": None,
        "curr_bed_type": None,
        "ironing_type": None,
        "brim_type": None,
        "brim_width": None,
        "outer_wall_speed": None,
        "inner_wall_speed": None,
        "infill_speed": None,
        "travel_speed": None,
        "fan_max_speed": None,
        "seam_position": None,
        "file_size": file_size
    }

    try:
        req_head = urllib.request.Request(url, headers={"Range": "bytes=0-46080"})
        with urllib.request.urlopen(req_head, timeout=5) as r:
            head = r.read().decode("utf-8", errors="ignore")
            cr = r.headers.get("Content-Range", "")
            if "/" in cr:
                try:
                    file_size = int(cr.split("/")[-1])
                    info["file_size"] = file_size
                except Exception:
                    pass

        thumbs = re.findall(r';\s*thumbnail begin\s+(\d+x\d+)\s+(\d+)\s*\n((?:;[^\n]*\n)+?);\s*thumbnail end', head)
        if thumbs:
            thumbs_sorted = sorted(thumbs, key=lambda t: int(t[0].split("x")[0]), reverse=True)
            res, size, b64raw = thumbs_sorted[0]
            clean_b64 = re.sub(r'[\s;]', '', b64raw)
            info["thumbnail"] = f"data:image/png;base64,{clean_b64}"

        s_match = re.search(r';\s*generated by\s+([^\n]+)', head)
        if s_match:
            info["slicer"] = s_match.group(1).strip()

        l_match = re.search(r';\s*total layer number:\s*(\d+)', head)
        if l_match:
            info["total_layers"] = int(l_match.group(1))

        z_match = re.search(r';\s*max_z_height:\s*([\d\.]+)', head)
        if z_match:
            info["max_z"] = float(z_match.group(1))

        dens_match = re.search(r';\s*filament_density:\s*([\d\.]+)', head)
        if dens_match:
            try:
                info["density"] = float(dens_match.group(1))
            except Exception:
                pass

    except Exception:
        pass

    if file_size > 0:
        start_byte = max(0, file_size - 76800)
        try:
            req_tail = urllib.request.Request(url, headers={"Range": f"bytes={start_byte}-{file_size}"})
            with urllib.request.urlopen(req_tail, timeout=5) as r:
                tail = r.read().decode("utf-8", errors="ignore")

            for line in tail.splitlines():
                line = line.strip()
                if not line.startswith(";"):
                    continue
                
                if "=" in line:
                    parts = line.lstrip(";").split("=", 1)
                    k = parts[0].strip()
                    v = parts[1].strip().strip('\"')

                    if k in ["total filament used [g]", "filament used [g]"]:
                        val = v.split(",")[0].strip()
                        try:
                            info["filament_g_est"] = float(val)
                        except Exception:
                            pass
                    elif k in ["total filament cost", "filament cost"]:
                        val = v.split(",")[0].strip()
                        try:
                            info["filament_cost_est"] = float(val)
                        except Exception:
                            pass
                    elif "estimated printing time" in k:
                        if not info["time_est"]:
                            info["time_est"] = v
                    elif k == "filament_type":
                        info["filament_type"] = v.split(";")[0].strip()
                    elif k == "filament_settings_id":
                        info["filament_name"] = v.split(";")[0].strip()
                    elif k == "print_settings_id":
                        info["print_settings_id"] = v
                    elif k == "printer_settings_id":
                        info["printer_settings_id"] = v
                    elif k == "nozzle_diameter":
                        info["nozzle_diameter"] = v.split(",")[0].strip()
                    elif k == "layer_height":
                        info["layer_height"] = v.split(",")[0].strip()
                    elif k == "first_layer_height":
                        info["first_layer_height"] = v.split(",")[0].strip()
                    elif k == "sparse_infill_density":
                        info["infill_density"] = v
                    elif k == "sparse_infill_pattern":
                        info["infill_pattern"] = v
                    elif k == "wall_loops":
                        info["wall_loops"] = v
                    elif k == "enable_support":
                        info["enable_support"] = v
                    elif k == "support_type":
                        info["support_type"] = v
                    elif k == "curr_bed_type":
                        info["curr_bed_type"] = v
                    elif k == "ironing_type":
                        info["ironing_type"] = v
                    elif k == "brim_type":
                        info["brim_type"] = v
                    elif k == "brim_width":
                        info["brim_width"] = v
                    elif k == "outer_wall_speed":
                        info["outer_wall_speed"] = v
                    elif k == "inner_wall_speed":
                        info["inner_wall_speed"] = v
                    elif k == "sparse_infill_speed":
                        info["infill_speed"] = v
                    elif k == "travel_speed":
                        info["travel_speed"] = v
                    elif k == "fan_max_speed":
                        info["fan_max_speed"] = v
                    elif k == "seam_position":
                        info["seam_position"] = v
                    elif k == "nozzle_temperature":
                        info["nozzle_temp"] = v.split(",")[0].strip()
                    elif k in ["first_layer_temperature", "nozzle_temperature_initial_layer"]:
                        info["first_layer_nozzle_temp"] = v.split(",")[0].strip()
                    elif k in ["bed_temperature", "textured_plate_temp"]:
                        info["bed_temp"] = v.split(",")[0].strip()
                    elif k in ["first_layer_bed_temperature", "textured_plate_temp_initial_layer"]:
                        info["first_layer_bed_temp"] = v.split(",")[0].strip()
        except Exception:
            pass

    return info

def fetch_printer_history(ip=PRINTER_IP, port=PRINTER_PORT, limit=200):
    print(f"Connecting to Moonraker at {ip}:{port}...")
    
    hist_url = f"http://{ip}:{port}/server/history/list?limit={limit}"
    with urllib.request.urlopen(hist_url, timeout=6) as r:
        hist_data = json.loads(r.read().decode("utf-8"))["result"]
    jobs = hist_data.get("jobs", [])
    print(f"Retrieved {len(jobs)} print records from history database.")

    files_url = f"http://{ip}:{port}/server/files/list?root=gcodes"
    with urllib.request.urlopen(files_url, timeout=6) as r:
        files_data = json.loads(r.read().decode("utf-8"))["result"]
    print(f"Found {len(files_data)} gcode files on storage.")

    file_map = {f["path"]: f for f in files_data}
    base_map = {}
    for f in files_data:
        base = f["path"].split("/")[-1]
        base_map[base] = f

    target_files = {}
    for j in jobs:
        fname = j.get("filename")
        if not fname:
            continue
        rel_path = None
        f_info = file_map.get(fname) or base_map.get(fname)
        if f_info:
            rel_path = f_info["path"]
            target_files[fname] = (rel_path, f_info.get("size", 0))

    print(f"Inspecting G-Code headers & footers for {len(target_files)} unique files...")
    
    metadata_cache = {}
    def worker(item):
        fname, (rel_path, fsize) = item
        details = extract_file_details(ip, port, rel_path, fsize)
        return fname, details

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        for fname, det in ex.map(worker, target_files.items()):
            metadata_cache[fname] = det

    print(f"Extraction completed in {time.time()-t0:.2f} seconds.")

    processed_jobs = []
    for j in jobs:
        fname = j.get("filename", "Unknown")
        status = j.get("status", "unknown")
        print_dur = j.get("print_duration", 0.0)
        total_dur = j.get("total_duration", 0.0)
        fil_used_mm = j.get("filament_used", 0.0)
        fil_used_m = fil_used_mm / 1000.0 if fil_used_mm else 0.0
        start_ts = j.get("start_time")
        end_ts = j.get("end_time")

        file_meta = metadata_cache.get(fname, {})
        clean_name = clean_project_name(fname)

        density = file_meta.get("density", 1.24)
        calc_grams = round((fil_used_mm * math.pi * ((1.75 / 2) ** 2) / 1000.0) * density, 2)
        
        sliced_grams = file_meta.get("filament_g_est")
        sliced_cost = file_meta.get("filament_cost_est")

        actual_grams = float(calc_grams) if (calc_grams and calc_grams > 0) else float(sliced_grams or 0.0)
        
        if sliced_cost and status == "completed":
            cost_eur = round(float(sliced_cost), 2)
        else:
            cost_eur = round(actual_grams * (DEFAULT_COST_PER_KG / 1000.0), 2)

        start_dt = datetime.datetime.fromtimestamp(start_ts) if start_ts else None
        end_dt = datetime.datetime.fromtimestamp(end_ts) if end_ts else None

        time_est_str = file_meta.get("time_est") or "N/D"
        est_seconds = parse_time_str_to_seconds(time_est_str)
        delta_str = ""
        delta_pct = None
        if status == "completed" and est_seconds and print_dur > 10:
            diff_sec = print_dur - est_seconds
            diff_pct = (diff_sec / est_seconds) * 100.0
            delta_pct = diff_pct
            if diff_pct < 0:
                delta_str = f"-{abs(diff_pct):.1f}%"
            else:
                delta_str = f"+{diff_pct:.1f}%"

        sup_enabled = file_meta.get("enable_support") == "1"
        sup_type = file_meta.get("support_type") or "Normale"
        if "tree" in sup_type.lower():
            sup_label = "Tree"
        elif sup_enabled:
            sup_label = "Standard"
        else:
            sup_label = "Nessuno"

        iron_type = file_meta.get("ironing_type") or "no ironing"
        has_ironing = iron_type not in ["no ironing", "none", ""]

        file_size_mb = round(file_meta.get("file_size", 0) / (1024 * 1024), 2) if file_meta.get("file_size") else None

        record = {
            "job_id": j.get("job_id", ""),
            "filename": fname,
            "project_name": clean_name,
            "status": status,
            "status_label": "Completata" if status == "completed" else ("Annullata" if status == "cancelled" else status.capitalize()),
            "start_time": start_dt.strftime("%Y-%m-%d %H:%M") if start_dt else "N/D",
            "end_time": end_dt.strftime("%Y-%m-%d %H:%M") if end_dt else "N/D",
            "timestamp": start_ts or 0,
            "print_duration": print_dur,
            "print_duration_str": format_duration(print_dur),
            "total_duration": total_dur,
            "total_duration_str": format_duration(total_dur),
            "time_est_str": time_est_str,
            "delta_str": delta_str,
            "delta_pct": delta_pct,
            "filament_mm": round(fil_used_mm, 1),
            "filament_m": round(fil_used_m, 2),
            "filament_g": actual_grams,
            "filament_cost_eur": cost_eur,
            "filament_type": file_meta.get("filament_type") or "PLA",
            "filament_name": file_meta.get("filament_name") or "Standard",
            "thumbnail": file_meta.get("thumbnail"),
            "slicer": file_meta.get("slicer") or "Creality Print",
            "total_layers": file_meta.get("total_layers") or "N/D",
            "max_z": file_meta.get("max_z") or "N/D",
            "nozzle_temp": file_meta.get("nozzle_temp") or "N/D",
            "first_layer_nozzle_temp": file_meta.get("first_layer_nozzle_temp") or "N/D",
            "bed_temp": file_meta.get("bed_temp") or "N/D",
            "first_layer_bed_temp": file_meta.get("first_layer_bed_temp") or "N/D",
            "print_profile": file_meta.get("print_settings_id") or "Standard",
            "printer_profile": file_meta.get("printer_settings_id") or "SparkX i7",
            "nozzle_diameter": file_meta.get("nozzle_diameter") or "0.4",
            "layer_height": file_meta.get("layer_height") or "N/D",
            "first_layer_height": file_meta.get("first_layer_height") or "N/D",
            "infill_density": file_meta.get("infill_density") or "N/D",
            "infill_pattern": file_meta.get("infill_pattern") or "N/D",
            "wall_loops": file_meta.get("wall_loops") or "N/D",
            "supports_enabled": sup_enabled,
            "support_label": sup_label,
            "support_type": sup_type,
            "bed_type": file_meta.get("curr_bed_type") or "PEI Plate",
            "has_ironing": has_ironing,
            "ironing_type": iron_type,
            "brim_type": file_meta.get("brim_type") or "N/D",
            "brim_width": file_meta.get("brim_width") or "N/D",
            "outer_wall_speed": file_meta.get("outer_wall_speed") or "N/D",
            "inner_wall_speed": file_meta.get("inner_wall_speed") or "N/D",
            "infill_speed": file_meta.get("infill_speed") or "N/D",
            "travel_speed": file_meta.get("travel_speed") or "N/D",
            "fan_speed": file_meta.get("fan_max_speed") or "N/D",
            "seam_position": file_meta.get("seam_position") or "N/D",
            "file_size_mb": file_size_mb or "N/D"
        }
        processed_jobs.append(record)

    return processed_jobs

def generate_html_report(jobs, output_path="printer_history.html"):
    total_jobs = len(jobs)
    completed_jobs = sum(1 for j in jobs if j["status"] == "completed")
    cancelled_jobs = sum(1 for j in jobs if j["status"] == "cancelled")

    total_print_seconds = sum(j["print_duration"] for j in jobs)
    total_filament_meters = sum(j["filament_m"] for j in jobs)
    total_filament_grams = sum(j["filament_g"] for j in jobs)
    total_cost = sum(j["filament_cost_eur"] for j in jobs)

    total_hours = total_print_seconds / 3600.0
    success_rate = (completed_jobs / total_jobs * 100.0) if total_jobs > 0 else 0

    json_jobs = json.dumps(jobs)
    json_svg = json.dumps(PRINTER_SVG_PLACEHOLDER)

    html_content = f"""<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SparkX i7 &bull; Console Telemetria Stampe</title>
    <style>
        :root {{
            --bg-base: #07090c;
            --bg-surface: #0f1217;
            --bg-elevated: #151921;
            --bg-item: #12151c;
            --bg-hover: #1a202a;
            --bg-viewport: #090b0e;

            --border-strong: #2c3342;
            --border: #1e232d;
            --border-faint: #151820;

            /* Strict Emerald Accent */
            --green: #10b981;
            --green-dim: #059669;
            --green-subtle: rgba(16, 185, 129, 0.08);
            --green-border: rgba(16, 185, 129, 0.28);
            --green-bright: #34d399;

            --fg-white: #ffffff;
            --fg-primary: #e2e8f0;
            --fg-muted: #8c97a8;
            --fg-faint: #555f6e;

            --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Monaco, Consolas, "Liberation Mono", monospace;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            background-color: var(--bg-base);
            color: var(--fg-primary);
            font-family: var(--font-sans);
            font-size: 13px;
            line-height: 1.45;
            -webkit-font-smoothing: antialiased;
            padding: 1.25rem 1.75rem 4rem;
        }}

        .console-root {{
            max-width: 1560px;
            margin: 0 auto;
        }}

        /* Integrated Header + Telemetry Strip */
        .console-header {{
            background: var(--bg-surface);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 0.85rem 1.25rem;
            margin-bottom: 1rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 1rem;
        }}

        .brand-cluster {{
            display: flex;
            align-items: center;
            gap: 0.85rem;
        }}

        .brand-name {{
            font-family: var(--font-mono);
            font-size: 13px;
            font-weight: 700;
            letter-spacing: 0.04em;
            color: var(--fg-white);
        }}

        .conn-pill {{
            font-family: var(--font-mono);
            font-size: 11px;
            color: var(--green-bright);
            background: var(--green-subtle);
            border: 1px solid var(--green-border);
            padding: 2px 7px;
            border-radius: 3px;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}

        .conn-dot {{
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background-color: var(--green);
        }}

        /* Compact Telemetry Pill Cluster */
        .telemetry-summary {{
            display: flex;
            align-items: center;
            gap: 0.75rem;
            flex-wrap: wrap;
        }}

        .stat-pill {{
            background: var(--bg-base);
            border: 1px solid var(--border);
            border-radius: 4px;
            padding: 4px 10px;
            display: inline-flex;
            align-items: baseline;
            gap: 6px;
        }}

        .stat-label {{
            font-size: 10px;
            font-weight: 600;
            color: var(--fg-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}

        .stat-num {{
            font-family: var(--font-mono);
            font-size: 13px;
            font-weight: 600;
            color: var(--fg-white);
        }}

        .stat-num.green {{
            color: var(--green-bright);
        }}

        .header-actions {{
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}

        .btn {{
            font-family: var(--font-sans);
            font-size: 12px;
            font-weight: 500;
            padding: 5px 12px;
            border-radius: 4px;
            border: 1px solid var(--border-strong);
            background: var(--bg-elevated);
            color: var(--fg-primary);
            cursor: pointer;
            transition: all 0.15s ease;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 5px;
        }}

        .btn:hover {{
            background: var(--bg-hover);
            border-color: var(--fg-muted);
            color: var(--fg-white);
        }}

        .btn-green {{
            background: var(--green-subtle);
            color: var(--green-bright);
            border-color: var(--green-border);
        }}

        .btn-green:hover {{
            background: rgba(16, 185, 129, 0.18);
            border-color: var(--green);
            color: var(--fg-white);
        }}

        /* Operational Toolbar */
        .action-toolbar {{
            background: var(--bg-surface);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 0.55rem 0.85rem;
            margin-bottom: 1rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 0.65rem;
        }}

        .filter-tabs {{
            display: inline-flex;
            background: var(--bg-base);
            border: 1px solid var(--border);
            border-radius: 4px;
            padding: 2px;
        }}

        .tab-item {{
            font-family: var(--font-sans);
            font-size: 12px;
            font-weight: 500;
            padding: 4px 10px;
            border-radius: 3px;
            border: none;
            background: transparent;
            color: var(--fg-muted);
            cursor: pointer;
            transition: all 0.15s ease;
        }}

        .tab-item:hover {{
            color: var(--fg-primary);
        }}

        .tab-item.active {{
            background: var(--bg-elevated);
            color: var(--fg-white);
            font-weight: 600;
        }}

        .tool-cluster {{
            display: flex;
            align-items: center;
            gap: 0.5rem;
            flex-wrap: wrap;
        }}

        .search-input {{
            background: var(--bg-base);
            border: 1px solid var(--border);
            border-radius: 4px;
            color: var(--fg-white);
            font-family: var(--font-sans);
            font-size: 12px;
            padding: 4px 9px;
            width: 220px;
            outline: none;
            transition: border-color 0.15s;
        }}

        .search-input:focus {{
            border-color: var(--green);
        }}

        .select-dark {{
            background: var(--bg-base);
            border: 1px solid var(--border);
            border-radius: 4px;
            color: var(--fg-primary);
            font-family: var(--font-sans);
            font-size: 12px;
            padding: 4px 8px;
            outline: none;
            cursor: pointer;
        }}

        .select-dark:focus {{
            border-color: var(--green);
        }}

        .rate-wrapper {{
            display: inline-flex;
            align-items: center;
            gap: 4px;
            background: var(--bg-base);
            border: 1px solid var(--border);
            border-radius: 4px;
            padding: 3px 8px;
            font-size: 11px;
            color: var(--fg-muted);
        }}

        .rate-input {{
            width: 36px;
            background: transparent;
            border: none;
            color: var(--green-bright);
            font-family: var(--font-mono);
            font-size: 11px;
            font-weight: 600;
            text-align: right;
            outline: none;
        }}

        .view-group {{
            display: inline-flex;
            background: var(--bg-base);
            border: 1px solid var(--border);
            border-radius: 4px;
            padding: 2px;
        }}

        .view-pill {{
            border: none;
            background: transparent;
            color: var(--fg-faint);
            padding: 4px 7px;
            border-radius: 3px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 11px;
            font-weight: 500;
            gap: 4px;
        }}

        .view-pill.active {{
            background: var(--bg-elevated);
            color: var(--fg-white);
        }}

        /* ============================================================
           VIEW 1: HORIZONTAL ROW FEED (Engineering Job Feed - Default)
           ============================================================ */
        .feed-container {{
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }}

        .feed-row {{
            background: var(--bg-surface);
            border: 1px solid var(--border);
            border-radius: 5px;
            padding: 0.75rem 1rem;
            display: grid;
            grid-template-columns: 88px 1fr 220px 180px 110px;
            align-items: center;
            gap: 1.25rem;
            cursor: pointer;
            transition: all 0.15s ease;
        }}

        .feed-row:hover {{
            background: var(--bg-elevated);
            border-color: var(--border-strong);
        }}

        .feed-thumb-box {{
            width: 88px;
            height: 88px;
            background: var(--bg-viewport);
            border: 1px solid var(--border);
            border-radius: 4px;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            flex-shrink: 0;
            position: relative;
        }}

        .feed-thumb-img {{
            max-width: 90%;
            max-height: 90%;
            object-fit: contain;
            filter: drop-shadow(0 2px 5px rgba(0,0,0,0.6));
        }}

        .placeholder-svg {{
            width: 58px;
            height: 58px;
            opacity: 0.85;
        }}

        .feed-col-primary {{
            min-width: 0;
        }}

        .feed-title {{
            font-size: 14px;
            font-weight: 600;
            color: var(--fg-white);
            margin-bottom: 3px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}

        .feed-file {{
            font-family: var(--font-mono);
            font-size: 11px;
            color: var(--fg-muted);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            margin-bottom: 6px;
        }}

        .feed-tags {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            align-items: center;
        }}

        .tag-pill {{
            font-size: 13px;
            font-family: var(--font-mono);
            background: var(--bg-base);
            border: 1px solid var(--border);
            color: var(--fg-primary);
            padding: 3px 9px;
            border-radius: 4px;
            display: inline-flex;
            align-items: center;
            gap: 5px;
        }}

        .tag-pill strong {{
            color: var(--fg-muted);
            font-weight: 600;
            font-size: 12px;
        }}

        .tag-pill.green {{
            color: var(--green-bright);
            border-color: var(--green-border);
            background: var(--green-subtle);
        }}

        .tag-pill.highlight {{
            border-color: var(--border-strong);
            background: var(--bg-elevated);
            color: var(--fg-white);
        }}

        .feed-col-specs {{
            border-left: 1px solid var(--border-faint);
            border-right: 1px solid var(--border-faint);
            padding: 0 1.25rem;
            display: flex;
            flex-direction: column;
            gap: 5px;
            font-size: 13px;
        }}

        .spec-line {{
            display: flex;
            justify-content: space-between;
        }}

        .spec-label {{
            color: var(--fg-faint);
            font-weight: 500;
            font-size: 12px;
        }}

        .spec-val {{
            font-family: var(--font-mono);
            color: var(--fg-primary);
            font-weight: 600;
            font-size: 13px;
        }}

        .feed-col-metrics {{
            display: flex;
            flex-direction: column;
            gap: 4px;
        }}

        .metric-duration {{
            font-family: var(--font-mono);
            font-size: 16px;
            font-weight: 700;
            color: var(--fg-white);
            display: flex;
            align-items: baseline;
            gap: 6px;
        }}

        .metric-delta {{
            font-size: 12px;
            color: var(--green-bright);
            font-family: var(--font-sans);
            font-weight: 600;
        }}

        .metric-grams {{
            font-family: var(--font-mono);
            font-size: 13px;
            color: var(--fg-muted);
        }}

        .metric-cost {{
            font-family: var(--font-mono);
            font-size: 14px;
            font-weight: 700;
            color: var(--green-bright);
            margin-top: 2px;
        }}

        .feed-col-status {{
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            gap: 6px;
        }}

        .status-badge {{
            font-family: var(--font-mono);
            font-size: 12px;
            font-weight: 700;
            text-transform: uppercase;
            padding: 4px 9px;
            border-radius: 4px;
            display: inline-flex;
            align-items: center;
            gap: 5px;
        }}

        .status-badge.completed {{
            color: var(--green-bright);
            background: var(--green-subtle);
            border: 1px solid var(--green-border);
        }}

        .status-badge.cancelled {{
            color: var(--fg-faint);
            background: var(--bg-base);
            border: 1px solid var(--border);
        }}

        .status-badge.in_progress {{
            color: var(--green);
            border: 1px solid var(--green);
        }}

        .feed-date {{
            font-size: 13.5px;
            font-weight: 600;
            color: var(--fg-white);
            text-align: right;
            line-height: 1.35;
            letter-spacing: -0.01em;
        }}

        .feed-job-id {{
            font-family: var(--font-mono);
            font-size: 12px;
            color: var(--fg-faint);
        }}

        /* ============================================================
           VIEW 2: GALLERY GRID (Visual Plate Gallery)
           ============================================================ */
        .gallery-grid {{
            display: none;
            grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
            gap: 0.85rem;
        }}

        .gallery-item {{
            background: var(--bg-surface);
            border: 1px solid var(--border);
            border-radius: 6px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            cursor: pointer;
            transition: all 0.15s ease;
        }}

        .gallery-item:hover {{
            background: var(--bg-elevated);
            border-color: var(--border-strong);
        }}

        .gallery-viewport {{
            height: 160px;
            background: var(--bg-viewport);
            border-bottom: 1px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
        }}

        .gallery-viewport img {{
            max-width: 85%;
            max-height: 85%;
            object-fit: contain;
        }}

        .gallery-body {{
            padding: 0.75rem 0.85rem;
            display: flex;
            flex-direction: column;
            gap: 0.4rem;
        }}

        /* ============================================================
           VIEW 3: DATA TABLE
           ============================================================ */
        .table-container {{
            display: none;
            background: var(--bg-surface);
            border: 1px solid var(--border);
            border-radius: 6px;
            overflow-x: auto;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
            text-align: left;
        }}

        th {{
            background: var(--bg-base);
            padding: 7px 10px;
            font-size: 10px;
            font-weight: 600;
            color: var(--fg-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            border-bottom: 1px solid var(--border);
            white-space: nowrap;
        }}

        td {{
            padding: 6px 10px;
            border-bottom: 1px solid var(--border-faint);
            color: var(--fg-primary);
            vertical-align: middle;
        }}

        tr:hover td {{
            background: var(--bg-elevated);
        }}

        .table-thumb {{
            width: 38px;
            height: 38px;
            background: var(--bg-viewport);
            border: 1px solid var(--border);
            border-radius: 3px;
            object-fit: contain;
            display: block;
        }}

        /* ============================================================
           SIDE INSPECTOR DRAWER (Slide-over Detail Panel)
           ============================================================ */
        .drawer-overlay {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.65);
            backdrop-filter: blur(4px);
            z-index: 1000;
            justify-content: flex-end;
        }}

        .drawer-panel {{
            background: var(--bg-surface);
            border-left: 1px solid var(--border-strong);
            width: 100%;
            max-width: 620px;
            height: 100%;
            overflow-y: auto;
            padding: 1.5rem;
            display: flex;
            flex-direction: column;
            box-shadow: -15px 0 35px rgba(0,0,0,0.8);
            animation: slideIn 0.2s ease-out;
        }}

        @keyframes slideIn {{
            from {{ transform: translateX(100%); }}
            to {{ transform: translateX(0); }}
        }}

        .drawer-header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 1.25rem;
            padding-bottom: 0.85rem;
            border-bottom: 1px solid var(--border);
        }}

        .drawer-close {{
            background: var(--bg-base);
            border: 1px solid var(--border);
            color: var(--fg-muted);
            width: 28px;
            height: 28px;
            border-radius: 4px;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            font-family: var(--font-mono);
            font-size: 13px;
        }}

        .drawer-close:hover {{
            background: var(--bg-hover);
            color: var(--fg-white);
        }}

        .drawer-viewport {{
            height: 190px;
            background: var(--bg-viewport);
            border: 1px solid var(--border);
            border-radius: 5px;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-bottom: 1.25rem;
            overflow: hidden;
        }}

        .drawer-viewport img {{
            max-width: 90%;
            max-height: 90%;
            object-fit: contain;
        }}

        .spec-block-title {{
            font-size: 11px;
            font-weight: 600;
            color: var(--green-bright);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin: 1.1rem 0 0.4rem;
            padding-bottom: 3px;
            border-bottom: 1px solid var(--border-faint);
        }}

        .spec-grid-2 {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 0.2rem 1.25rem;
        }}

        .spec-row {{
            display: flex;
            justify-content: space-between;
            padding: 3px 0;
            border-bottom: 1px solid var(--border-faint);
            font-size: 12px;
        }}

        .spec-k {{
            color: var(--fg-faint);
        }}

        .spec-v {{
            font-family: var(--font-mono);
            color: var(--fg-white);
            text-align: right;
        }}

        /* Responsive Breakpoints */
        @media (max-width: 1024px) {{
            .feed-row {{
                grid-template-columns: 75px 1fr 160px 100px;
            }}
            .feed-col-specs {{
                display: none;
            }}
        }}

        @media (max-width: 768px) {{
            body {{
                padding: 0.75rem;
            }}
            .feed-row {{
                grid-template-columns: 60px 1fr;
                gap: 0.75rem;
            }}
            .feed-col-metrics, .feed-col-status {{
                grid-column: span 2;
                align-items: flex-start;
            }}
        }}
    </style>
</head>
<body>
    <div class="console-root">
        <!-- Integrated Header -->
        <header class="console-header">
            <div class="brand-cluster">
                <span class="brand-name">SPARKX i7 TELEMETRY</span>
                <span class="conn-pill"><span class="conn-dot"></span>192.168.78.50:7125</span>
            </div>

            <!-- Integrated Telemetry Strip -->
            <div class="telemetry-summary">
                <div class="stat-pill">
                    <span class="stat-label">Lavori:</span>
                    <span class="stat-num">{total_jobs}</span>
                </div>
                <div class="stat-pill">
                    <span class="stat-label">Completamento:</span>
                    <span class="stat-num green">{success_rate:.1f}%</span>
                </div>
                <div class="stat-pill">
                    <span class="stat-label">Runtime:</span>
                    <span class="stat-num">{int(total_hours)}h {int(total_print_seconds%3600)//60}m</span>
                </div>
                <div class="stat-pill">
                    <span class="stat-label">Filamento:</span>
                    <span class="stat-num">{(total_filament_grams/1000.0):.2f} kg</span>
                </div>
                <div class="stat-pill">
                    <span class="stat-label">Spesa:</span>
                    <span class="stat-num green" id="stat-total-cost">€{total_cost:.2f}</span>
                </div>
            </div>

            <div class="header-actions">
                <button class="btn" onclick="exportCSV()">Esporta .CSV</button>
                <button class="btn btn-green" onclick="window.print()">Stampa PDF</button>
            </div>
        </header>

        <!-- Operational Toolbar -->
        <div class="action-toolbar">
            <div class="filter-tabs">
                <button class="tab-item active" data-filter="all" onclick="setStatusFilter('all', this)">Tutti ({total_jobs})</button>
                <button class="tab-item" data-filter="completed" onclick="setStatusFilter('completed', this)">Completati ({completed_jobs})</button>
                <button class="tab-item" data-filter="cancelled" onclick="setStatusFilter('cancelled', this)">Interrotti ({cancelled_jobs})</button>
            </div>

            <div class="tool-cluster">
                <input type="text" class="search-input" id="searchInput" placeholder="Cerca progetto, nozzle, profilo..." oninput="filterJobs()">

                <select class="select-dark" id="nozzleFilter" onchange="filterJobs()">
                    <option value="all">Nozzle: Tutti</option>
                    <option value="0.4">Nozzle: 0.4 mm</option>
                    <option value="0.8">Nozzle: 0.8 mm</option>
                </select>

                <select class="select-dark" id="supportFilter" onchange="filterJobs()">
                    <option value="all">Supporti: Tutti</option>
                    <option value="with_support">Con Supporti</option>
                    <option value="no_support">Senza Supporti</option>
                </select>

                <select class="select-dark" id="sortSelect" onchange="filterJobs()">
                    <option value="date_desc">Data (recenti)</option>
                    <option value="date_asc">Data (meno recenti)</option>
                    <option value="dur_desc">Durata maggiore</option>
                    <option value="dur_asc">Durata minore</option>
                    <option value="fil_desc">Consumo filamento (g)</option>
                    <option value="cost_desc">Costo materiale (€)</option>
                    <option value="name_asc">Nome file (A-Z)</option>
                </select>

                <div class="rate-wrapper" title="Modifica tariffa filamento">
                    <span>Costo:</span>
                    <input type="number" class="rate-input" id="costPerKgInput" value="{DEFAULT_COST_PER_KG:.1f}" step="0.5" min="5" max="100" onchange="recalculateCost(this.value)">
                    <span>€/kg</span>
                </div>

                <div class="view-group">
                    <button class="view-pill active" id="btnList" onclick="setViewMode('list')">Elenco</button>
                    <button class="view-pill" id="btnGallery" onclick="setViewMode('gallery')">Galleria</button>
                    <button class="view-pill" id="btnTable" onclick="setViewMode('table')">Tabella</button>
                </div>
            </div>
        </div>

        <!-- VIEW 1: Feed Rows (Default) -->
        <div class="feed-container" id="feedContainer"></div>

        <!-- VIEW 2: Gallery Grid -->
        <div class="gallery-grid" id="galleryContainer"></div>

        <!-- VIEW 3: Data Table -->
        <div class="table-container" id="tableContainer">
            <table>
                <thead>
                    <tr>
                        <th>Anteprima</th>
                        <th>Progetto</th>
                        <th>Stato</th>
                        <th>Data</th>
                        <th>Nozzle</th>
                        <th>Layer</th>
                        <th>Profilo Slicer</th>
                        <th>Infill</th>
                        <th>Supporti</th>
                        <th>Durata</th>
                        <th>Stima</th>
                        <th>Massa</th>
                        <th>Costo</th>
                        <th>Materiale</th>
                    </tr>
                </thead>
                <tbody id="tableBody"></tbody>
            </table>
        </div>
    </div>

    <!-- Slide-over Technical Inspector Drawer -->
    <div class="drawer-overlay" id="inspectorDrawer" onclick="closeDrawer(event)">
        <div class="drawer-panel" onclick="event.stopPropagation()">
            <div class="drawer-header">
                <div>
                    <div style="font-size:15px; font-weight:700; color:var(--fg-white);" id="modalTitle"></div>
                    <div style="font-family:var(--font-mono); font-size:11px; color:var(--fg-muted); word-break:break-all; margin-top:2px;" id="modalFilename"></div>
                </div>
                <button class="drawer-close" onclick="document.getElementById('inspectorDrawer').style.display='none'">&times;</button>
            </div>

            <div class="drawer-viewport" id="modalImgWrap"></div>
            <div id="modalDetails"></div>
        </div>
    </div>

    <script>
        const allJobs = {json_jobs};
        const svgPlaceholder = {json_svg};
        let currentStatusFilter = 'all';
        let costPerKg = {DEFAULT_COST_PER_KG};

        function formatItalianDate(timestamp, fallbackStr = "") {{
            let d = null;
            if (timestamp && timestamp > 0) {{
                d = new Date(timestamp * 1000);
            }} else if (fallbackStr && fallbackStr !== 'N/D') {{
                d = new Date(fallbackStr.replace(' ', 'T'));
            }}
            if (!d || isNaN(d.getTime())) return fallbackStr || 'Data non disponibile';

            const giorni = ['Domenica', 'Lunedì', 'Martedì', 'Mercoledì', 'Giovedì', 'Venerdì', 'Sabato'];
            const mesi = ['Gennaio', 'Febbraio', 'Marzo', 'Aprile', 'Maggio', 'Giugno', 'Luglio', 'Agosto', 'Settembre', 'Ottobre', 'Novembre', 'Dicembre'];
            
            const giornoSettimana = giorni[d.getDay()];
            const giorno = d.getDate();
            const mese = mesi[d.getMonth()];
            const anno = d.getFullYear();
            const ore = String(d.getHours()).padStart(2, '0');
            const minuti = String(d.getMinutes()).padStart(2, '0');

            return `${{giornoSettimana}} ${{giorno}} ${{mese}} ${{anno}}, ${{ore}}:${{minuti}}`;
        }}

        function renderFeed(jobs) {{
            const c = document.getElementById('feedContainer');
            if (jobs.length === 0) {{
                c.innerHTML = '<div style="text-align: center; padding: 3rem; color: var(--fg-faint); font-family: var(--font-mono);">NESSUN RECORD CORRISPONDENTE</div>';
                return;
            }}

            c.innerHTML = jobs.map(j => {{
                const thumbHtml = j.thumbnail 
                    ? `<img class="feed-thumb-img" src="${{j.thumbnail}}" alt="${{j.project_name}}" loading="lazy">`
                    : svgPlaceholder;

                const cost = ((j.filament_g * costPerKg) / 1000.0).toFixed(2);
                const nozzleStr = j.nozzle_diameter !== 'N/D' ? `${{j.nozzle_diameter}}mm` : '0.4mm';
                const layerStr = j.layer_height !== 'N/D' ? `${{j.layer_height}}mm` : 'N/D';
                const italianDate = formatItalianDate(j.timestamp, j.start_time);

                const deltaSpan = j.delta_str 
                    ? `<span class="metric-delta">(${{j.delta_str}})</span>` 
                    : '';

                return `
                <div class="feed-row" onclick="openInspector('${{j.job_id}}')">
                    <div class="feed-thumb-box">${{thumbHtml}}</div>

                    <div class="feed-col-primary">
                        <div class="feed-title" title="${{j.project_name}}">${{j.project_name}}</div>
                        <div class="feed-file" title="${{j.filename}}">${{j.print_profile}}</div>
                        <div class="feed-tags">
                            <span class="tag-pill green">${{j.filament_name}}</span>
                            <span class="tag-pill highlight"><strong>Ugello:</strong> ${{nozzleStr}}</span>
                            <span class="tag-pill highlight"><strong>Layer:</strong> ${{layerStr}}</span>
                            <span class="tag-pill"><strong>Infill:</strong> ${{j.infill_density !== 'N/D' ? j.infill_density : ''}} ${{j.infill_pattern}}</span>
                            ${{j.supports_enabled ? `<span class="tag-pill green"><strong>Supporti:</strong> ${{j.support_label.toUpperCase()}}</span>` : ''}}
                            ${{j.has_ironing ? `<span class="tag-pill green">STIRATURA</span>` : ''}}
                        </div>
                    </div>

                    <div class="feed-col-specs">
                        <div class="spec-line"><span class="spec-label">Pareti</span><span class="spec-val">${{j.wall_loops !== 'N/D' ? j.wall_loops : 'N/D'}} loops</span></div>
                        <div class="spec-line"><span class="spec-label">Velocità</span><span class="spec-val">${{j.outer_wall_speed !== 'N/D' ? j.outer_wall_speed + ' mm/s' : 'N/D'}}</span></div>
                        <div class="spec-line"><span class="spec-label">Termica</span><span class="spec-val">${{j.nozzle_temp}}° / ${{j.bed_temp}}°</span></div>
                        <div class="spec-line"><span class="spec-label">Altezza Z</span><span class="spec-val">${{j.max_z !== 'N/D' ? j.max_z + ' mm' : 'N/D'}}</span></div>
                    </div>

                    <div class="feed-col-metrics">
                        <div class="metric-duration">${{j.print_duration_str}} ${{deltaSpan}}</div>
                        <div class="metric-grams">${{j.filament_g.toFixed(1)}}g &bull; ${{j.filament_m.toFixed(2)}}m</div>
                        <div class="metric-cost">€${{cost}}</div>
                    </div>

                    <div class="feed-col-status">
                        <span class="status-badge ${{j.status}}">${{j.status_label}}</span>
                        <div class="feed-date">${{italianDate}}</div>
                        <span class="feed-job-id">#${{j.job_id}}</span>
                    </div>
                </div>
                `;
            }}).join('');
        }}

        function renderGallery(jobs) {{
            const c = document.getElementById('galleryContainer');
            if (jobs.length === 0) {{
                c.innerHTML = '<div style="grid-column: 1/-1; text-align: center; padding: 3rem; color: var(--fg-faint);">NESSUN DATO</div>';
                return;
            }}

            c.innerHTML = jobs.map(j => {{
                const thumbHtml = j.thumbnail 
                    ? `<img src="${{j.thumbnail}}" alt="${{j.project_name}}" loading="lazy">`
                    : svgPlaceholder;

                const cost = ((j.filament_g * costPerKg) / 1000.0).toFixed(2);
                const italianDate = formatItalianDate(j.timestamp, j.start_time);
                const nozzleVal = j.nozzle_diameter !== 'N/D' ? `${{j.nozzle_diameter}}mm` : '0.4mm';
                const layerVal = j.layer_height !== 'N/D' ? `${{j.layer_height}}mm` : 'N/D';

                return `
                <div class="gallery-item" onclick="openInspector('${{j.job_id}}')">
                    <div class="gallery-viewport">
                        ${{thumbHtml}}
                        <span class="status-badge ${{j.status}}" style="position:absolute; top:8px; right:8px;">${{j.status_label}}</span>
                    </div>
                    <div class="gallery-body">
                        <div style="font-size:15px; font-weight:700; color:var(--fg-white); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${{j.project_name}}</div>
                        <div style="display:flex; justify-content:space-between; font-family:var(--font-mono); font-size:13px; margin: 3px 0;">
                            <span style="color:var(--fg-white); font-weight:700;">${{j.print_duration_str}}</span>
                            <span style="color:var(--green-bright); font-weight:700;">€${{cost}}</span>
                        </div>
                        <div style="display:flex; flex-wrap:wrap; gap:5px; margin: 4px 0;">
                            <span class="tag-pill highlight"><strong>Ugello:</strong> ${{nozzleVal}}</span>
                            <span class="tag-pill highlight"><strong>Layer:</strong> ${{layerVal}}</span>
                        </div>
                        <div style="font-size:13px; font-weight:600; color:var(--fg-white); margin-top:4px;">
                            ${{italianDate}}
                        </div>
                    </div>
                </div>
                `;
            }}).join('');
        }}

        function renderTable(jobs) {{
            const tbody = document.getElementById('tableBody');
            if (jobs.length === 0) {{
                tbody.innerHTML = '<tr><td colspan="14" style="text-align: center; padding: 2rem; color: var(--fg-faint);">NESSUN RECORD</td></tr>';
                return;
            }}

            tbody.innerHTML = jobs.map(j => {{
                const thumbHtml = j.thumbnail 
                    ? `<img class="table-thumb" src="${{j.thumbnail}}" alt="${{j.project_name}}">`
                    : `<div class="table-thumb" style="display:flex;align-items:center;justify-content:center;">${{svgPlaceholder}}</div>`;

                const cost = ((j.filament_g * costPerKg) / 1000.0).toFixed(2);
                const italianDate = formatItalianDate(j.timestamp, j.start_time);

                return `
                <tr onclick="openInspector('${{j.job_id}}')" style="cursor:pointer;">
                    <td>${{thumbHtml}}</td>
                    <td><strong style="color:var(--fg-white); font-size:14px;">${{j.project_name}}</strong></td>
                    <td><span class="status-badge ${{j.status}}">${{j.status_label}}</span></td>
                    <td style="font-size:13px; font-weight:600; color:var(--fg-white);">${{italianDate}}</td>
                    <td style="font-family:var(--font-mono); font-weight:600;"><span style="color:var(--fg-muted); font-size:11px;">Ugello:</span> ${{j.nozzle_diameter}} mm</td>
                    <td style="font-family:var(--font-mono); font-weight:600;"><span style="color:var(--fg-muted); font-size:11px;">Layer:</span> ${{j.layer_height}} mm</td>
                    <td style="max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-family:var(--font-mono); color:var(--fg-muted);" title="${{j.print_profile}}">${{j.print_profile}}</td>
                    <td style="font-family:var(--font-mono); font-size:13px;"><span style="color:var(--fg-muted); font-size:11px;">Infill:</span> ${{j.infill_density}} (${{j.infill_pattern}})</td>
                    <td style="font-size:13px;">${{j.supports_enabled ? j.support_label : 'No'}}</td>
                    <td style="font-family:var(--font-mono); font-weight:700; color:var(--fg-white); font-size:14px;">${{j.print_duration_str}}</td>
                    <td style="font-family:var(--font-mono); color:var(--fg-faint); font-size:13px;">${{j.time_est_str}}</td>
                    <td style="font-family:var(--font-mono); font-size:13px;">${{j.filament_g.toFixed(1)}}g</td>
                    <td style="font-family:var(--font-mono); font-weight:700; color:var(--green-bright); font-size:14px;">€${{cost}}</td>
                    <td><span class="tag-pill green">${{j.filament_type}}</span></td>
                </tr>
                `;
            }}).join('');
        }}

        function filterJobs() {{
            const query = document.getElementById('searchInput').value.toLowerCase().trim();
            const nozzleVal = document.getElementById('nozzleFilter').value;
            const supportVal = document.getElementById('supportFilter').value;
            const sortVal = document.getElementById('sortSelect').value;

            let filtered = allJobs.filter(j => {{
                const matchesStatus = (currentStatusFilter === 'all') || (j.status === currentStatusFilter);
                const matchesNozzle = (nozzleVal === 'all') || (j.nozzle_diameter.toString().startsWith(nozzleVal));
                const matchesSupport = (supportVal === 'all') || 
                    (supportVal === 'with_support' && j.supports_enabled) ||
                    (supportVal === 'no_support' && !j.supports_enabled);

                const matchesQuery = !query || 
                    j.project_name.toLowerCase().includes(query) ||
                    j.filename.toLowerCase().includes(query) ||
                    j.print_profile.toLowerCase().includes(query) ||
                    j.filament_name.toLowerCase().includes(query) ||
                    j.filament_type.toLowerCase().includes(query) ||
                    j.start_time.toLowerCase().includes(query) ||
                    j.infill_pattern.toLowerCase().includes(query) ||
                    j.nozzle_diameter.toString().includes(query);

                return matchesStatus && matchesNozzle && matchesSupport && matchesQuery;
            }});

            filtered.sort((a, b) => {{
                switch(sortVal) {{
                    case 'date_desc': return b.timestamp - a.timestamp;
                    case 'date_asc': return a.timestamp - b.timestamp;
                    case 'dur_desc': return b.print_duration - a.print_duration;
                    case 'dur_asc': return a.print_duration - b.print_duration;
                    case 'fil_desc': return b.filament_g - a.filament_g;
                    case 'cost_desc': return b.filament_cost_eur - a.filament_cost_eur;
                    case 'name_asc': return a.project_name.localeCompare(b.project_name);
                    default: return b.timestamp - a.timestamp;
                }}
            }});

            renderFeed(filtered);
            renderGallery(filtered);
            renderTable(filtered);
        }}

        function setStatusFilter(status, btn) {{
            currentStatusFilter = status;
            document.querySelectorAll('.tab-item').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            filterJobs();
        }}

        function setViewMode(mode) {{
            const feed = document.getElementById('feedContainer');
            const gallery = document.getElementById('galleryContainer');
            const table = document.getElementById('tableContainer');

            document.querySelectorAll('.view-pill').forEach(p => p.classList.remove('active'));

            if (mode === 'list') {{
                feed.style.display = 'flex';
                gallery.style.display = 'none';
                table.style.display = 'none';
                document.getElementById('btnList').classList.add('active');
            }} else if (mode === 'gallery') {{
                feed.style.display = 'none';
                gallery.style.display = 'grid';
                table.style.display = 'none';
                document.getElementById('btnGallery').classList.add('active');
            }} else {{
                feed.style.display = 'none';
                gallery.style.display = 'none';
                table.style.display = 'block';
                document.getElementById('btnTable').classList.add('active');
            }}
        }}

        function recalculateCost(newCost) {{
            costPerKg = parseFloat(newCost) || 20.0;
            const totalCostEl = document.getElementById('stat-total-cost');
            const totalGrams = allJobs.reduce((acc, j) => acc + j.filament_g, 0);
            const newTotalCost = (totalGrams * costPerKg / 1000.0).toFixed(2);
            totalCostEl.textContent = '€' + newTotalCost;
            filterJobs();
        }}

        function openInspector(jobId) {{
            const j = allJobs.find(item => item.job_id === jobId);
            if (!j) return;

            document.getElementById('modalTitle').textContent = j.project_name;
            document.getElementById('modalFilename').textContent = `${{j.filename}} &bull; Job #${{j.job_id}}`;

            const imgWrap = document.getElementById('modalImgWrap');
            if (j.thumbnail) {{
                imgWrap.innerHTML = `<img src="${{j.thumbnail}}">`;
            }} else {{
                imgWrap.innerHTML = svgPlaceholder;
            }}

            const cost = ((j.filament_g * costPerKg) / 1000.0).toFixed(2);

            const detailsHtml = `
                <div class="spec-block-title">Configurazione Slicer & Profilazione</div>
                <div class="spec-grid-2">
                    <div class="spec-row"><span class="spec-k">Profilo di processo</span><span class="spec-v" style="color:var(--green-bright);">${{j.print_profile}}</span></div>
                    <div class="spec-row"><span class="spec-k">Profilo stampante</span><span class="spec-v">${{j.printer_profile}}</span></div>
                    <div class="spec-row"><span class="spec-k">Diametro Ugello (Nozzle)</span><span class="spec-v">${{j.nozzle_diameter}} mm</span></div>
                    <div class="spec-row"><span class="spec-k">Software Slicer</span><span class="spec-v">${{j.slicer}}</span></div>
                    <div class="spec-row"><span class="spec-k">Piano di Stampa</span><span class="spec-v">${{j.bed_type}}</span></div>
                    <div class="spec-row"><span class="spec-k">Dimensione G-Code</span><span class="spec-v">${{j.file_size_mb !== 'N/D' ? j.file_size_mb + ' MB' : 'N/D'}}</span></div>
                </div>

                <div class="spec-block-title">Geometria, Struttura & Supporti</div>
                <div class="spec-grid-2">
                    <div class="spec-row"><span class="spec-k">Altezza Layer Standard / 1°</span><span class="spec-v">${{j.layer_height}} mm / ${{j.first_layer_height}} mm</span></div>
                    <div class="spec-row"><span class="spec-k">Totale Layer / Quota Z Max</span><span class="spec-v">${{j.total_layers}} / ${{j.max_z}} mm</span></div>
                    <div class="spec-row"><span class="spec-k">Pareti Perimetriche</span><span class="spec-v">${{j.wall_loops}} loops</span></div>
                    <div class="spec-row"><span class="spec-k">Riempimento (Infill)</span><span class="spec-v">${{j.infill_density}} (${{j.infill_pattern}})</span></div>
                    <div class="spec-row"><span class="spec-k">Supporti</span><span class="spec-v">${{j.support_label}} (${{j.support_type}})</span></div>
                    <div class="spec-row"><span class="spec-k">Stiratura Superfici (Ironing)</span><span class="spec-v">${{j.ironing_type}}</span></div>
                    <div class="spec-row"><span class="spec-k">Adesione Piatto (Brim)</span><span class="spec-v">${{j.brim_type}} (${{j.brim_width}} mm)</span></div>
                    <div class="spec-row"><span class="spec-k">Cucitura Z (Seam)</span><span class="spec-v">${{j.seam_position}}</span></div>
                </div>

                <div class="spec-block-title">Parametri Cinematica & Termica</div>
                <div class="spec-grid-2">
                    <div class="spec-row"><span class="spec-k">Temperatura Nozzle (Stnd / 1°)</span><span class="spec-v">${{j.nozzle_temp}}°C / ${{j.first_layer_nozzle_temp}}°C</span></div>
                    <div class="spec-row"><span class="spec-k">Temperatura Piatto (Stnd / 1°)</span><span class="spec-v">${{j.bed_temp}}°C / ${{j.first_layer_bed_temp}}°C</span></div>
                    <div class="spec-row"><span class="spec-k">Velocità Parete Esterna</span><span class="spec-v">${{j.outer_wall_speed}} mm/s</span></div>
                    <div class="spec-row"><span class="spec-k">Velocità Riempimento</span><span class="spec-v">${{j.infill_speed}} mm/s</span></div>
                    <div class="spec-row"><span class="spec-k">Velocità Travel (Rapidi)</span><span class="spec-v">${{j.travel_speed}} mm/s</span></div>
                    <div class="spec-row"><span class="spec-k">Ventola Raffreddamento</span><span class="spec-v">${{j.fan_speed}}%</span></div>
                </div>

                <div class="spec-block-title">Consumi & Telemetria Esecuzione</div>
                <div class="spec-grid-2">
                    <div class="spec-row"><span class="spec-k">Filamento Estruso (Massa)</span><span class="spec-v" style="color:var(--fg-white); font-weight:600;">${{j.filament_g.toFixed(1)}} g</span></div>
                    <div class="spec-row"><span class="spec-k">Filamento Estruso (Lunghezza)</span><span class="spec-v">${{j.filament_m.toFixed(2)}} m</span></div>
                    <div class="spec-row"><span class="spec-k">Costo Calcolato</span><span class="spec-v" style="color:var(--green-bright); font-weight:600;">€${{cost}} (@${{costPerKg.toFixed(1)}}/kg)</span></div>
                    <div class="spec-row"><span class="spec-k">Identificativo Filamento</span><span class="spec-v">${{j.filament_name}} (${{j.filament_type}})</span></div>
                    <div class="spec-row"><span class="spec-k">Durata Stampa Reale</span><span class="spec-v" style="color:var(--fg-white); font-weight:600;">${{j.print_duration_str}}</span></div>
                    <div class="spec-row"><span class="spec-k">Stima Slicer vs Reale</span><span class="spec-v">${{j.time_est_str}} ${{j.delta_str ? '(' + j.delta_str + ')' : ''}}</span></div>
                    <div class="spec-row"><span class="spec-k">Durata Totale (con warm-up)</span><span class="spec-v">${{j.total_duration_str}}</span></div>
                    <div class="spec-row"><span class="spec-k">Intervallo Esecuzione</span><span class="spec-v">${{j.start_time}} &bull; ${{j.end_time}}</span></div>
                </div>
            `;
            document.getElementById('modalDetails').innerHTML = detailsHtml;
            document.getElementById('inspectorDrawer').style.display = 'flex';
        }}

        function closeDrawer(e) {{
            if (e.target.id === 'inspectorDrawer') {{
                document.getElementById('inspectorDrawer').style.display = 'none';
            }}
        }}

        document.addEventListener('keydown', (e) => {{
            if (e.key === 'Escape') {{
                document.getElementById('inspectorDrawer').style.display = 'none';
            }}
        }});

        function exportCSV() {{
            const headers = [
                'ID', 'Nome Progetto', 'Nome File', 'Stato', 'Data Avvio', 'Data Fine', 
                'Ugello (mm)', 'Altezza Layer (mm)', 'Profilo Processo', 'Profilo Stampante',
                'Infill %', 'Trama Infill', 'Pareti Loops', 'Supporti', 'Stiratura',
                'Durata Secondi', 'Durata Formattata', 'Stima Slicer', 
                'Filamento Grammi', 'Filamento Metri', 'Costo EUR', 'Tipo Filamento', 'Profilo Filamento',
                'Temp Nozzle', 'Temp Piatto', 'Velocita Parete (mm/s)', 'Velocita Spostamento (mm/s)'
            ];

            const rows = allJobs.map(j => [
                j.job_id,
                `"${{j.project_name.replace(/"/g, '""')}}"`,
                `"${{j.filename.replace(/"/g, '""')}}"`,
                j.status,
                j.start_time,
                j.end_time,
                j.nozzle_diameter,
                j.layer_height,
                `"${{j.print_profile.replace(/"/g, '""')}}"`,
                `"${{j.printer_profile.replace(/"/g, '""')}}"`,
                `"${{j.infill_density}}"`,
                `"${{j.infill_pattern}}"`,
                j.wall_loops,
                `"${{j.support_label}}"`,
                `"${{j.ironing_type}}"`,
                j.print_duration,
                `"${{j.print_duration_str}}"`,
                `"${{j.time_est_str}}"`,
                j.filament_g,
                j.filament_m,
                ((j.filament_g * costPerKg) / 1000.0).toFixed(2),
                j.filament_type,
                `"${{j.filament_name.replace(/"/g, '""')}}"`,
                j.nozzle_temp,
                j.bed_temp,
                j.outer_wall_speed,
                j.travel_speed
            ]);

            const csvContent = "data:text/csv;charset=utf-8," + [headers.join(','), ...rows.map(e => e.join(','))].join('\\n');
            const encodedUri = encodeURI(csvContent);
            const link = document.createElement("a");
            link.setAttribute("href", encodedUri);
            link.setAttribute("download", `telemetria_sparkx_i7_${{new Date().toISOString().slice(0,10)}}.csv`);
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        }}

        // Render initial view
        renderFeed(allJobs);
        renderGallery(allJobs);
        renderTable(allJobs);
    </script>
</body>
</html>
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"HTML report successfully written to {output_path}")

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate authentic 3D printer telemetry console.")
    parser.add_argument("--ip", default=PRINTER_IP, help="Printer IP address")
    parser.add_argument("--port", type=int, default=PRINTER_PORT, help="Printer Moonraker port")
    parser.add_argument("--limit", type=int, default=200, help="Max history jobs to fetch")
    parser.add_argument("--output", default="printer_history.html", help="Output HTML file path")
    parser.add_argument("--open", action="store_true", help="Open generated HTML file in web browser")
    args = parser.parse_args()

    jobs = fetch_printer_history(ip=args.ip, port=args.port, limit=args.limit)
    generate_html_report(jobs, output_path=args.output)

    if args.open:
        import webbrowser
        abs_path = os.path.abspath(args.output)
        webbrowser.open(f"file://{abs_path}")

if __name__ == "__main__":
    main()
