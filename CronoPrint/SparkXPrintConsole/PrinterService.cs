using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;

namespace SparkXPrintConsole
{
    public class PrinterService
    {
        private static readonly HttpClient Http = new HttpClient { Timeout = TimeSpan.FromSeconds(6) };
        private static readonly string CacheFile = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "history_cache.json");

        public static async Task<LivePrinterStatus> GetLiveStatusAsync(string ip, int port, Dictionary<string, string>? thumbCache = null)
        {
            var status = new LivePrinterStatus { IsConnected = false, State = "offline" };
            try
            {
                var url = $"http://{ip}:{port}/printer/objects/query?print_stats&extruder&heater_bed";
                var res = await Http.GetStringAsync(url);
                using var doc = JsonDocument.Parse(res);
                if (doc.RootElement.TryGetProperty("result", out var result) &&
                    result.TryGetProperty("status", out var st))
                {
                    status.IsConnected = true;

                    if (st.TryGetProperty("print_stats", out var ps))
                    {
                        status.State = ps.TryGetProperty("state", out var stateEl) ? stateEl.GetString() ?? "ready" : "ready";
                        status.IsPrinting = status.State == "printing" || status.State == "paused";
                        status.Filename = ps.TryGetProperty("filename", out var fEl) ? fEl.GetString() ?? "" : "";
                        status.ProjectName = CleanProjectName(status.Filename);

                        if (ps.TryGetProperty("print_duration", out var durEl))
                        {
                            status.PrintDurationSec = durEl.GetDouble();
                            status.PrintDurationStr = FormatDuration(status.PrintDurationSec);
                        }

                        if (ps.TryGetProperty("filament_used", out var filEl))
                        {
                            var mm = filEl.GetDouble();
                            status.FilamentUsedM = Math.Round(mm / 1000.0, 2);
                            status.FilamentUsedG = Math.Round((mm * Math.PI * Math.Pow(1.75 / 2.0, 2) / 1000.0) * 1.24, 1);
                        }

                        if (ps.TryGetProperty("z_pos", out var zEl))
                        {
                            status.CurrentZ = Math.Round(zEl.GetDouble(), 2);
                        }
                    }

                    if (st.TryGetProperty("extruder", out var ext))
                    {
                        if (ext.TryGetProperty("temperature", out var tEl)) status.NozzleTemp = Math.Round(tEl.GetDouble(), 1);
                        if (ext.TryGetProperty("target", out var tgtEl)) status.NozzleTarget = Math.Round(tgtEl.GetDouble(), 1);
                    }

                    if (st.TryGetProperty("heater_bed", out var bed))
                    {
                        if (bed.TryGetProperty("temperature", out var tEl)) status.BedTemp = Math.Round(tEl.GetDouble(), 1);
                        if (bed.TryGetProperty("target", out var tgtEl)) status.BedTarget = Math.Round(tgtEl.GetDouble(), 1);
                    }

                    if (!string.IsNullOrEmpty(status.Filename) && thumbCache != null && thumbCache.TryGetValue(status.Filename, out var t))
                    {
                        status.Thumbnail = t;
                    }
                }
            }
            catch
            {
                status.IsConnected = false;
                status.State = "offline";
            }
            return status;
        }

        public static List<PrintJob> LoadCachedHistory()
        {
            try
            {
                if (File.Exists(CacheFile))
                {
                    var json = File.ReadAllText(CacheFile);
                    var jobs = JsonSerializer.Deserialize<List<PrintJob>>(json);
                    if (jobs != null) return jobs;
                }
            }
            catch { }
            return new List<PrintJob>();
        }

        public static void SaveCachedHistory(List<PrintJob> jobs)
        {
            try
            {
                var json = JsonSerializer.Serialize(jobs, new JsonSerializerOptions { WriteIndented = true });
                File.WriteAllText(CacheFile, json);
            }
            catch { }
        }

        public static async Task<List<PrintJob>> SyncHistoryAsync(string ip, int port, double costPerKg)
        {
            var histUrl = $"http://{ip}:{port}/server/history/list?limit=250";
            var histJson = await Http.GetStringAsync(histUrl);
            using var histDoc = JsonDocument.Parse(histJson);
            var jobsEl = histDoc.RootElement.GetProperty("result").GetProperty("jobs");

            var filesUrl = $"http://{ip}:{port}/server/files/list?root=gcodes";
            var filesJson = await Http.GetStringAsync(filesUrl);
            using var filesDoc = JsonDocument.Parse(filesJson);
            var filesEl = filesDoc.RootElement.GetProperty("result");

            var fileMap = new Dictionary<string, (string path, long size)>(StringComparer.OrdinalIgnoreCase);
            var baseMap = new Dictionary<string, (string path, long size)>(StringComparer.OrdinalIgnoreCase);

            foreach (var f in filesEl.EnumerateArray())
            {
                var p = f.GetProperty("path").GetString() ?? "";
                var sz = f.TryGetProperty("size", out var sEl) ? sEl.GetInt64() : 0L;
                fileMap[p] = (p, sz);
                var b = Path.GetFileName(p);
                if (!baseMap.ContainsKey(b)) baseMap[b] = (p, sz);
            }

            // Identify unique files to extract metadata for
            var targetFiles = new Dictionary<string, (string path, long size)>();
            foreach (var j in jobsEl.EnumerateArray())
            {
                var fname = j.TryGetProperty("filename", out var fEl) ? fEl.GetString() : null;
                if (string.IsNullOrEmpty(fname) || targetFiles.ContainsKey(fname)) continue;

                if (fileMap.TryGetValue(fname, out var info) || baseMap.TryGetValue(fname, out info))
                {
                    targetFiles[fname] = info;
                }
            }

            // Parallel extraction using Range requests
            var metaCache = new ConcurrentDictionary<string, FileMetadata>();
            var tasks = new List<Task>();
            using var sem = new System.Threading.SemaphoreSlim(8);

            foreach (var kvp in targetFiles)
            {
                tasks.Add(Task.Run(async () =>
                {
                    await sem.WaitAsync();
                    try
                    {
                        var meta = await ExtractFileMetadataAsync(ip, port, kvp.Value.path, kvp.Value.size);
                        metaCache[kvp.Key] = meta;
                    }
                    finally
                    {
                        sem.Release();
                    }
                }));
            }
            await Task.WhenAll(tasks);

            var list = new List<PrintJob>();
            foreach (var j in jobsEl.EnumerateArray())
            {
                var fname = j.TryGetProperty("filename", out var fEl) ? fEl.GetString() ?? "Unknown" : "Unknown";
                var status = j.TryGetProperty("status", out var sEl) ? sEl.GetString() ?? "unknown" : "unknown";
                var printDur = j.TryGetProperty("print_duration", out var pdEl) ? pdEl.GetDouble() : 0.0;
                var totalDur = j.TryGetProperty("total_duration", out var tdEl) ? tdEl.GetDouble() : 0.0;
                var filMm = j.TryGetProperty("filament_used", out var fuEl) ? fuEl.GetDouble() : 0.0;
                var startTs = j.TryGetProperty("start_time", out var stEl) && stEl.ValueKind == JsonValueKind.Number ? stEl.GetDouble() : 0.0;
                var endTs = j.TryGetProperty("end_time", out var etEl) && etEl.ValueKind == JsonValueKind.Number ? etEl.GetDouble() : 0.0;
                var jobId = j.TryGetProperty("job_id", out var idEl) ? idEl.GetString() ?? "" : "";

                metaCache.TryGetValue(fname, out var m);

                var filM = filMm / 1000.0;
                var density = m?.Density ?? 1.24;
                var calcGrams = (filMm * Math.PI * Math.Pow(1.75 / 2.0, 2) / 1000.0) * density;
                var actualGrams = calcGrams > 0 ? calcGrams : (m?.FilamentGEst ?? 0.0);

                var costEur = (m?.FilamentCostEst != null && status == "completed")
                    ? m.FilamentCostEst.Value
                    : actualGrams * (costPerKg / 1000.0);

                var startDt = startTs > 0 ? DateTimeOffset.FromUnixTimeSeconds((long)startTs).ToLocalTime() : (DateTimeOffset?)null;
                var endDt = endTs > 0 ? DateTimeOffset.FromUnixTimeSeconds((long)endTs).ToLocalTime() : (DateTimeOffset?)null;

                var estSec = ParseTimeStrToSeconds(m?.TimeEst);
                string deltaStr = "";
                double? deltaPct = null;
                if (status == "completed" && estSec.HasValue && printDur > 10)
                {
                    var diffSec = printDur - estSec.Value;
                    var pct = (diffSec / estSec.Value) * 100.0;
                    deltaPct = pct;
                    deltaStr = pct < 0 ? $"-{Math.Abs(pct):F1}%" : $"+{pct:F1}%";
                }

                var supEnabled = m?.EnableSupport == "1";
                var supType = m?.SupportType ?? "Normale";
                var supLabel = supType.Contains("tree", StringComparison.OrdinalIgnoreCase) ? "Tree" : (supEnabled ? "Standard" : "Nessuno");

                var item = new PrintJob
                {
                    JobId = jobId,
                    Filename = fname,
                    ProjectName = CleanProjectName(fname),
                    Status = status,
                    StatusLabel = status == "completed" ? "Completata" : (status == "cancelled" ? "Interrotta" : status),
                    StartTime = startDt?.ToString("yyyy-MM-dd HH:mm") ?? "N/D",
                    EndTime = endDt?.ToString("yyyy-MM-dd HH:mm") ?? "N/D",
                    Timestamp = startTs,
                    PrintDuration = printDur,
                    PrintDurationStr = FormatDuration(printDur),
                    TotalDuration = totalDur,
                    TotalDurationStr = FormatDuration(totalDur),
                    TimeEstStr = m?.TimeEst ?? "N/D",
                    DeltaStr = deltaStr,
                    DeltaPct = deltaPct,
                    FilamentMm = Math.Round(filMm, 1),
                    FilamentM = Math.Round(filM, 2),
                    FilamentG = Math.Round(actualGrams, 1),
                    FilamentCostEur = Math.Round(costEur, 2),
                    FilamentType = m?.FilamentType ?? "PLA",
                    FilamentName = m?.FilamentName ?? "Standard",
                    Thumbnail = m?.Thumbnail,
                    Slicer = m?.Slicer ?? "Creality Print",
                    TotalLayers = m?.TotalLayers?.ToString() ?? "N/D",
                    MaxZ = m?.MaxZ?.ToString("F1") ?? "N/D",
                    NozzleTemp = m?.NozzleTemp ?? "N/D",
                    FirstLayerNozzleTemp = m?.FirstLayerNozzleTemp ?? "N/D",
                    BedTemp = m?.BedTemp ?? "N/D",
                    FirstLayerBedTemp = m?.FirstLayerBedTemp ?? "N/D",
                    PrintProfile = m?.PrintSettingsId ?? "Standard",
                    PrinterProfile = m?.PrinterSettingsId ?? "SparkX i7",
                    NozzleDiameter = m?.NozzleDiameter ?? "0.4",
                    LayerHeight = m?.LayerHeight ?? "N/D",
                    FirstLayerHeight = m?.FirstLayerHeight ?? "N/D",
                    InfillDensity = m?.InfillDensity ?? "N/D",
                    InfillPattern = m?.InfillPattern ?? "N/D",
                    WallLoops = m?.WallLoops ?? "N/D",
                    SupportsEnabled = supEnabled,
                    SupportLabel = supLabel,
                    SupportType = supType,
                    BedType = m?.CurrBedType ?? "PEI Plate",
                    HasIroning = !string.IsNullOrEmpty(m?.IroningType) && m.IroningType != "no ironing" && m.IroningType != "none",
                    IroningType = m?.IroningType ?? "no ironing",
                    BrimType = m?.BrimType ?? "N/D",
                    BrimWidth = m?.BrimWidth ?? "N/D",
                    OuterWallSpeed = m?.OuterWallSpeed ?? "N/D",
                    InnerWallSpeed = m?.InnerWallSpeed ?? "N/D",
                    InfillSpeed = m?.InfillSpeed ?? "N/D",
                    TravelSpeed = m?.TravelSpeed ?? "N/D",
                    FanSpeed = m?.FanMaxSpeed ?? "N/D",
                    SeamPosition = m?.SeamPosition ?? "N/D",
                    FileSizeMb = m?.FileSize > 0 ? (m.FileSize / (1024.0 * 1024.0)).ToString("F2") : "N/D"
                };
                list.Add(item);
            }

            SaveCachedHistory(list);
            return list;
        }

        private static async Task<FileMetadata> ExtractFileMetadataAsync(string ip, int port, string relPath, long knownSize)
        {
            var meta = new FileMetadata { FileSize = knownSize };
            var url = $"http://{ip}:{port}/server/files/gcodes/{Uri.EscapeDataString(relPath)}";

            try
            {
                using var reqHead = new HttpRequestMessage(HttpMethod.Get, url);
                reqHead.Headers.Range = new RangeHeaderValue(0, 46080);
                using var respHead = await Http.SendAsync(reqHead);
                var headStr = await respHead.Content.ReadAsStringAsync();

                if (respHead.Content.Headers.ContentRange?.Length.HasValue == true)
                {
                    meta.FileSize = respHead.Content.Headers.ContentRange.Length.Value;
                }

                // Thumbnail regex
                var thumbMatch = Regex.Matches(headStr, @";\s*thumbnail begin\s+(\d+x\d+)\s+(\d+)\s*\n((?:;[^\n]*\n)+?);\s*thumbnail end");
                if (thumbMatch.Count > 0)
                {
                    Match? best = null;
                    int maxRes = 0;
                    foreach (Match tm in thumbMatch)
                    {
                        var resStr = tm.Groups[1].Value.Split('x')[0];
                        if (int.TryParse(resStr, out var r) && r > maxRes)
                        {
                            maxRes = r;
                            best = tm;
                        }
                    }
                    if (best != null)
                    {
                        var raw = Regex.Replace(best.Groups[3].Value, @"[\s;]", "");
                        meta.Thumbnail = $"data:image/png;base64,{raw}";
                    }
                }

                var sm = Regex.Match(headStr, @";\s*generated by\s+([^\n]+)");
                if (sm.Success) meta.Slicer = sm.Groups[1].Value.Trim();

                var lm = Regex.Match(headStr, @";\s*total layer number:\s*(\d+)");
                if (lm.Success && int.TryParse(lm.Groups[1].Value, out var lCount)) meta.TotalLayers = lCount;

                var zm = Regex.Match(headStr, @";\s*max_z_height:\s*([\d\.]+)");
                if (zm.Success && double.TryParse(zm.Groups[1].Value, System.Globalization.CultureInfo.InvariantCulture, out var mz)) meta.MaxZ = mz;

                var dm = Regex.Match(headStr, @";\s*filament_density:\s*([\d\.]+)");
                if (dm.Success && double.TryParse(dm.Groups[1].Value, System.Globalization.CultureInfo.InvariantCulture, out var dens)) meta.Density = dens;
            }
            catch { }

            // Footer range
            if (meta.FileSize > 0)
            {
                try
                {
                    var startByte = Math.Max(0, meta.FileSize - 76800);
                    using var reqTail = new HttpRequestMessage(HttpMethod.Get, url);
                    reqTail.Headers.Range = new RangeHeaderValue(startByte, meta.FileSize);
                    using var respTail = await Http.SendAsync(reqTail);
                    var tailStr = await respTail.Content.ReadAsStringAsync();

                    using var reader = new StringReader(tailStr);
                    string? line;
                    while ((line = reader.ReadLine()) != null)
                    {
                        line = line.Trim();
                        if (!line.StartsWith(";") || !line.Contains('=')) continue;

                        var parts = line.TrimStart(';').Split('=', 2);
                        var k = parts[0].Trim();
                        var v = parts[1].Trim().Trim('\"');

                        if (k is "total filament used [g]" or "filament used [g]")
                        {
                            var s = v.Split(',')[0].Trim();
                            if (double.TryParse(s, System.Globalization.CultureInfo.InvariantCulture, out var g)) meta.FilamentGEst = g;
                        }
                        else if (k is "total filament cost" or "filament cost")
                        {
                            var s = v.Split(',')[0].Trim();
                            if (double.TryParse(s, System.Globalization.CultureInfo.InvariantCulture, out var c)) meta.FilamentCostEst = c;
                        }
                        else if (k.Contains("estimated printing time", StringComparison.OrdinalIgnoreCase) && string.IsNullOrEmpty(meta.TimeEst))
                        {
                            meta.TimeEst = v;
                        }
                        else if (k == "filament_type") meta.FilamentType = v.Split(';')[0].Trim();
                        else if (k == "filament_settings_id") meta.FilamentName = v.Split(';')[0].Trim();
                        else if (k == "print_settings_id") meta.PrintSettingsId = v;
                        else if (k == "printer_settings_id") meta.PrinterSettingsId = v;
                        else if (k == "nozzle_diameter") meta.NozzleDiameter = v.Split(',')[0].Trim();
                        else if (k == "layer_height") meta.LayerHeight = v.Split(',')[0].Trim();
                        else if (k == "first_layer_height") meta.FirstLayerHeight = v.Split(',')[0].Trim();
                        else if (k == "sparse_infill_density") meta.InfillDensity = v;
                        else if (k == "sparse_infill_pattern") meta.InfillPattern = v;
                        else if (k == "wall_loops") meta.WallLoops = v;
                        else if (k == "enable_support") meta.EnableSupport = v;
                        else if (k == "support_type") meta.SupportType = v;
                        else if (k == "curr_bed_type") meta.CurrBedType = v;
                        else if (k == "ironing_type") meta.IroningType = v;
                        else if (k == "brim_type") meta.BrimType = v;
                        else if (k == "brim_width") meta.BrimWidth = v;
                        else if (k == "outer_wall_speed") meta.OuterWallSpeed = v;
                        else if (k == "inner_wall_speed") meta.InnerWallSpeed = v;
                        else if (k == "sparse_infill_speed") meta.InfillSpeed = v;
                        else if (k == "travel_speed") meta.TravelSpeed = v;
                        else if (k == "fan_max_speed") meta.FanMaxSpeed = v;
                        else if (k == "seam_position") meta.SeamPosition = v;
                        else if (k == "nozzle_temperature") meta.NozzleTemp = v.Split(',')[0].Trim();
                        else if (k is "first_layer_temperature" or "nozzle_temperature_initial_layer") meta.FirstLayerNozzleTemp = v.Split(',')[0].Trim();
                        else if (k is "bed_temperature" or "textured_plate_temp") meta.BedTemp = v.Split(',')[0].Trim();
                        else if (k is "first_layer_bed_temperature" or "textured_plate_temp_initial_layer") meta.FirstLayerBedTemp = v.Split(',')[0].Trim();
                    }
                }
                catch { }
            }

            return meta;
        }

        public static async Task<bool> StartReprintAsync(string ip, int port, string filename)
        {
            try
            {
                var url = $"http://{ip}:{port}/printer/print/start";
                var payload = JsonSerializer.Serialize(new { filename });
                using var content = new StringContent(payload, Encoding.UTF8, "application/json");
                var res = await Http.PostAsync(url, content);
                return res.IsSuccessStatusCode;
            }
            catch
            {
                return false;
            }
        }

        public static async Task DownloadGcodeAsync(string ip, int port, string filename, string targetLocalPath)
        {
            var url = $"http://{ip}:{port}/server/files/gcodes/{Uri.EscapeDataString(filename)}";
            using var stream = await Http.GetStreamAsync(url);
            using var fileStream = File.Create(targetLocalPath);
            await stream.CopyToAsync(fileStream);
        }

        private static string CleanProjectName(string filename)
        {
            var name = Path.GetFileName(filename);
            name = Regex.Replace(name, @"_plate_\d+\.gcode$", "", RegexOptions.IgnoreCase);
            name = Regex.Replace(name, @"\.gcode(?:\.3mf)?$", "", RegexOptions.IgnoreCase);
            name = Regex.Replace(name, @"_(?:PLA|PETG|ABS|TPU|ASA)(?:_[0-9hms]+)?$", "", RegexOptions.IgnoreCase);
            name = Regex.Replace(name, @"\.stl$", "", RegexOptions.IgnoreCase);
            name = name.Replace('_', ' ').Replace('+', ' ').Trim();
            return string.IsNullOrEmpty(name) ? Path.GetFileName(filename) : name;
        }

        private static int? ParseTimeStrToSeconds(string? timeStr)
        {
            if (string.IsNullOrEmpty(timeStr) || timeStr == "N/D") return null;
            int total = 0;
            var hm = Regex.Match(timeStr, @"(\d+)\s*h");
            var mm = Regex.Match(timeStr, @"(\d+)\s*m");
            var sm = Regex.Match(timeStr, @"(\d+)\s*s");
            if (hm.Success) total += int.Parse(hm.Groups[1].Value) * 3600;
            if (mm.Success) total += int.Parse(mm.Groups[1].Value) * 60;
            if (sm.Success) total += int.Parse(sm.Groups[1].Value);
            return total > 0 ? total : null;
        }

        private static string FormatDuration(double seconds)
        {
            if (seconds <= 0) return "0s";
            var sec = (int)seconds;
            var hours = sec / 3600;
            var minutes = (sec % 3600) / 60;
            var remSec = sec % 60;
            var list = new List<string>();
            if (hours > 0) list.Add($"{hours}h");
            if (minutes > 0 || hours > 0) list.Add($"{minutes}m");
            if (hours == 0 && minutes < 5) list.Add($"{remSec}s");
            return list.Count > 0 ? string.Join(" ", list) : "0s";
        }

        private class FileMetadata
        {
            public long FileSize { get; set; }
            public string? Thumbnail { get; set; }
            public string? Slicer { get; set; }
            public int? TotalLayers { get; set; }
            public double? MaxZ { get; set; }
            public double Density { get; set; } = 1.24;
            public double? FilamentGEst { get; set; }
            public double? FilamentCostEst { get; set; }
            public string? TimeEst { get; set; }
            public string? FilamentType { get; set; }
            public string? FilamentName { get; set; }
            public string? PrintSettingsId { get; set; }
            public string? PrinterSettingsId { get; set; }
            public string? NozzleDiameter { get; set; }
            public string? LayerHeight { get; set; }
            public string? FirstLayerHeight { get; set; }
            public string? InfillDensity { get; set; }
            public string? InfillPattern { get; set; }
            public string? WallLoops { get; set; }
            public string? EnableSupport { get; set; }
            public string? SupportType { get; set; }
            public string? CurrBedType { get; set; }
            public string? IroningType { get; set; }
            public string? BrimType { get; set; }
            public string? BrimWidth { get; set; }
            public string? OuterWallSpeed { get; set; }
            public string? InnerWallSpeed { get; set; }
            public string? InfillSpeed { get; set; }
            public string? TravelSpeed { get; set; }
            public string? FanMaxSpeed { get; set; }
            public string? SeamPosition { get; set; }
            public string? NozzleTemp { get; set; }
            public string? FirstLayerNozzleTemp { get; set; }
            public string? BedTemp { get; set; }
            public string? FirstLayerBedTemp { get; set; }
        }
    }
}

