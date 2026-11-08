using System;
using System.IO;
using System.Text.Json;

namespace SparkXPrintConsole
{
    public class AppSettings
    {
        public string PrinterIp { get; set; } = "192.168.78.50";
        public int PrinterPort { get; set; } = 7125;
        public double CostPerKg { get; set; } = 20.0;
        public int PollIntervalSeconds { get; set; } = 3;
        public string LastViewMode { get; set; } = "list";

        private static readonly string SettingsPath = Path.Combine(
            AppDomain.CurrentDomain.BaseDirectory, "appsettings.json");

        public static AppSettings Load()
        {
            try
            {
                if (File.Exists(SettingsPath))
                {
                    var json = File.ReadAllText(SettingsPath);
                    var settings = JsonSerializer.Deserialize<AppSettings>(json);
                    if (settings != null) return settings;
                }
            }
            catch { }
            return new AppSettings();
        }

        public void Save()
        {
            try
            {
                var json = JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true });
                File.WriteAllText(SettingsPath, json);
            }
            catch { }
        }
    }
}

