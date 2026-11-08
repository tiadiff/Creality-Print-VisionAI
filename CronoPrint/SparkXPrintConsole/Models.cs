using System;
using System.Text.Json.Serialization;

namespace SparkXPrintConsole
{
    public class PrintJob
    {
        [JsonPropertyName("job_id")]
        public string JobId { get; set; } = "";

        [JsonPropertyName("filename")]
        public string Filename { get; set; } = "";

        [JsonPropertyName("project_name")]
        public string ProjectName { get; set; } = "";

        [JsonPropertyName("status")]
        public string Status { get; set; } = "";

        [JsonPropertyName("status_label")]
        public string StatusLabel { get; set; } = "";

        [JsonPropertyName("start_time")]
        public string StartTime { get; set; } = "N/D";

        [JsonPropertyName("end_time")]
        public string EndTime { get; set; } = "N/D";

        [JsonPropertyName("timestamp")]
        public double Timestamp { get; set; }

        [JsonPropertyName("print_duration")]
        public double PrintDuration { get; set; }

        [JsonPropertyName("print_duration_str")]
        public string PrintDurationStr { get; set; } = "0s";

        [JsonPropertyName("total_duration")]
        public double TotalDuration { get; set; }

        [JsonPropertyName("total_duration_str")]
        public string TotalDurationStr { get; set; } = "0s";

        [JsonPropertyName("time_est_str")]
        public string TimeEstStr { get; set; } = "N/D";

        [JsonPropertyName("delta_str")]
        public string DeltaStr { get; set; } = "";

        [JsonPropertyName("delta_pct")]
        public double? DeltaPct { get; set; }

        [JsonPropertyName("filament_mm")]
        public double FilamentMm { get; set; }

        [JsonPropertyName("filament_m")]
        public double FilamentM { get; set; }

        [JsonPropertyName("filament_g")]
        public double FilamentG { get; set; }

        [JsonPropertyName("filament_cost_eur")]
        public double FilamentCostEur { get; set; }

        [JsonPropertyName("filament_type")]
        public string FilamentType { get; set; } = "PLA";

        [JsonPropertyName("filament_name")]
        public string FilamentName { get; set; } = "Standard";

        [JsonPropertyName("thumbnail")]
        public string? Thumbnail { get; set; }

        [JsonPropertyName("slicer")]
        public string Slicer { get; set; } = "Creality Print";

        [JsonPropertyName("total_layers")]
        public string TotalLayers { get; set; } = "N/D";

        [JsonPropertyName("max_z")]
        public string MaxZ { get; set; } = "N/D";

        [JsonPropertyName("nozzle_temp")]
        public string NozzleTemp { get; set; } = "N/D";

        [JsonPropertyName("first_layer_nozzle_temp")]
        public string FirstLayerNozzleTemp { get; set; } = "N/D";

        [JsonPropertyName("bed_temp")]
        public string BedTemp { get; set; } = "N/D";

        [JsonPropertyName("first_layer_bed_temp")]
        public string FirstLayerBedTemp { get; set; } = "N/D";

        [JsonPropertyName("print_profile")]
        public string PrintProfile { get; set; } = "Standard";

        [JsonPropertyName("printer_profile")]
        public string PrinterProfile { get; set; } = "SparkX i7";

        [JsonPropertyName("nozzle_diameter")]
        public string NozzleDiameter { get; set; } = "0.4";

        [JsonPropertyName("layer_height")]
        public string LayerHeight { get; set; } = "N/D";

        [JsonPropertyName("first_layer_height")]
        public string FirstLayerHeight { get; set; } = "N/D";

        [JsonPropertyName("infill_density")]
        public string InfillDensity { get; set; } = "N/D";

        [JsonPropertyName("infill_pattern")]
        public string InfillPattern { get; set; } = "N/D";

        [JsonPropertyName("wall_loops")]
        public string WallLoops { get; set; } = "N/D";

        [JsonPropertyName("supports_enabled")]
        public bool SupportsEnabled { get; set; }

        [JsonPropertyName("support_label")]
        public string SupportLabel { get; set; } = "Nessuno";

        [JsonPropertyName("support_type")]
        public string SupportType { get; set; } = "N/D";

        [JsonPropertyName("bed_type")]
        public string BedType { get; set; } = "PEI Plate";

        [JsonPropertyName("has_ironing")]
        public bool HasIroning { get; set; }

        [JsonPropertyName("ironing_type")]
        public string IroningType { get; set; } = "no ironing";

        [JsonPropertyName("brim_type")]
        public string BrimType { get; set; } = "N/D";

        [JsonPropertyName("brim_width")]
        public string BrimWidth { get; set; } = "N/D";

        [JsonPropertyName("outer_wall_speed")]
        public string OuterWallSpeed { get; set; } = "N/D";

        [JsonPropertyName("inner_wall_speed")]
        public string InnerWallSpeed { get; set; } = "N/D";

        [JsonPropertyName("infill_speed")]
        public string InfillSpeed { get; set; } = "N/D";

        [JsonPropertyName("travel_speed")]
        public string TravelSpeed { get; set; } = "N/D";

        [JsonPropertyName("fan_speed")]
        public string FanSpeed { get; set; } = "N/D";

        [JsonPropertyName("seam_position")]
        public string SeamPosition { get; set; } = "N/D";

        [JsonPropertyName("file_size_mb")]
        public string FileSizeMb { get; set; } = "N/D";
    }

    public class LivePrinterStatus
    {
        [JsonPropertyName("is_connected")]
        public bool IsConnected { get; set; }

        [JsonPropertyName("is_printing")]
        public bool IsPrinting { get; set; }

        [JsonPropertyName("state")]
        public string State { get; set; } = "offline";

        [JsonPropertyName("filename")]
        public string Filename { get; set; } = "";

        [JsonPropertyName("project_name")]
        public string ProjectName { get; set; } = "";

        [JsonPropertyName("print_duration_sec")]
        public double PrintDurationSec { get; set; }

        [JsonPropertyName("print_duration_str")]
        public string PrintDurationStr { get; set; } = "0s";

        [JsonPropertyName("time_est_str")]
        public string TimeEstStr { get; set; } = "N/D";

        [JsonPropertyName("progress_pct")]
        public double ProgressPct { get; set; }

        [JsonPropertyName("filament_used_m")]
        public double FilamentUsedM { get; set; }

        [JsonPropertyName("filament_used_g")]
        public double FilamentUsedG { get; set; }

        [JsonPropertyName("current_z")]
        public double CurrentZ { get; set; }

        [JsonPropertyName("nozzle_temp")]
        public double NozzleTemp { get; set; }

        [JsonPropertyName("nozzle_target")]
        public double NozzleTarget { get; set; }

        [JsonPropertyName("bed_temp")]
        public double BedTemp { get; set; }

        [JsonPropertyName("bed_target")]
        public double BedTarget { get; set; }

        [JsonPropertyName("thumbnail")]
        public string? Thumbnail { get; set; }
    }
}

