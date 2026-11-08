using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace SparkXPrintConsole
{
    public class MainForm : Form
    {
        [DllImport("dwmapi.dll")]
        private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int attrValue, int attrSize);

        private const int DWMWA_USE_IMMERSIVE_DARK_MODE = 20;

        private WebView2? _webView;
        private AppSettings _settings = new();
        private List<PrintJob> _currentJobs = new();
        private readonly System.Windows.Forms.Timer _pollTimer = new();
        private bool _isSyncing = false;

        public MainForm()
        {
            InitializeComponent();
            ApplyDarkTitleBar();
            _settings = AppSettings.Load();

            _pollTimer.Interval = Math.Max(1000, _settings.PollIntervalSeconds * 1000);
            _pollTimer.Tick += async (s, e) => await PollLiveStatusAsync();
        }

        private void InitializeComponent()
        {
            this.Text = "SparkX i7 • Telemetria & Registro Stampe";
            this.Size = new Size(1400, 900);
            this.MinimumSize = new Size(1000, 650);
            this.StartPosition = FormStartPosition.CenterScreen;
            this.BackColor = Color.FromArgb(7, 9, 12);

            _webView = new WebView2
            {
                Dock = DockStyle.Fill
            };
            this.Controls.Add(_webView);

            this.Load += MainForm_Load;
        }

        private void ApplyDarkTitleBar()
        {
            try
            {
                int darkMode = 1;
                DwmSetWindowAttribute(this.Handle, DWMWA_USE_IMMERSIVE_DARK_MODE, ref darkMode, sizeof(int));
            }
            catch { }
        }

        private async void MainForm_Load(object? sender, EventArgs e)
        {
            if (_webView == null) return;

            try
            {
                var env = await CoreWebView2Environment.CreateAsync(null, Path.Combine(Path.GetTempPath(), "SparkXPrintConsole_UserData"));
                await _webView.EnsureCoreWebView2Async(env);

                var wwwroot = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "wwwroot");
                if (Directory.Exists(wwwroot))
                {
                    _webView.CoreWebView2.SetVirtualHostNameToFolderMapping(
                        "app.local", wwwroot, CoreWebView2HostResourceAccessKind.Allow);
                    _webView.CoreWebView2.Navigate("http://app.local/index.html");
                }
                else
                {
                    MessageBox.Show("Cartella wwwroot non trovata.", "Errore", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return;
                }

                _webView.CoreWebView2.WebMessageReceived += OnWebMessageReceived;
                _webView.NavigationCompleted += OnNavigationCompleted;
            }
            catch (Exception ex)
            {
                MessageBox.Show($"Inizializzazione WebView2 fallita: {ex.Message}", "Errore", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        private async void OnNavigationCompleted(object? sender, CoreWebView2NavigationCompletedEventArgs e)
        {
            // 1. First send cached history immediately so the user sees everything instantly
            _currentJobs = PrinterService.LoadCachedHistory();

            var initPayload = new
            {
                type = "init",
                jobs = _currentJobs,
                costPerKg = _settings.CostPerKg,
                printerIp = _settings.PrinterIp,
                printerPort = _settings.PrinterPort
            };
            PostJsonToWeb(initPayload);

            // 2. Start polling timer for live print telemetries
            _pollTimer.Start();
            await PollLiveStatusAsync();

            // 3. Trigger initial background sync
            await SyncWithPrinterAsync();
        }

        private async Task PollLiveStatusAsync()
        {
            try
            {
                var thumbMap = new Dictionary<string, string>();
                foreach (var j in _currentJobs)
                {
                    if (!string.IsNullOrEmpty(j.Filename) && !string.IsNullOrEmpty(j.Thumbnail))
                    {
                        thumbMap[j.Filename] = j.Thumbnail;
                    }
                }

                var status = await PrinterService.GetLiveStatusAsync(_settings.PrinterIp, _settings.PrinterPort, thumbMap);
                PostJsonToWeb(new
                {
                    type = "live_status",
                    status
                });
            }
            catch { }
        }

        private async Task SyncWithPrinterAsync()
        {
            if (_isSyncing) return;
            _isSyncing = true;
            try
            {
                var jobs = await PrinterService.SyncHistoryAsync(_settings.PrinterIp, _settings.PrinterPort, _settings.CostPerKg);
                _currentJobs = jobs;

                PostJsonToWeb(new
                {
                    type = "history_updated",
                    jobs = _currentJobs
                });
            }
            catch { }
            finally
            {
                _isSyncing = false;
            }
        }

        private async void OnWebMessageReceived(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
        {
            try
            {
                using var doc = JsonDocument.Parse(e.WebMessageAsJson);
                var root = doc.RootElement;
                var action = root.GetProperty("action").GetString();

                switch (action)
                {
                    case "sync":
                        await SyncWithPrinterAsync();
                        break;

                    case "reprint":
                        var fname = root.GetProperty("filename").GetString();
                        if (!string.IsNullOrEmpty(fname))
                        {
                            var ok = await PrinterService.StartReprintAsync(_settings.PrinterIp, _settings.PrinterPort, fname);
                            if (ok)
                            {
                                MessageBox.Show($"Comando di stampa inviato con successo a SparkX i7 per:\n{fname}", "Stampa Avviata", MessageBoxButtons.OK, MessageBoxIcon.Information);
                                await PollLiveStatusAsync();
                            }
                            else
                            {
                                MessageBox.Show($"Impossibile avviare la stampa di {fname}. Verifica lo stato della stampante.", "Errore Stampa", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                            }
                        }
                        break;

                    case "download_gcode":
                        var gFile = root.GetProperty("filename").GetString();
                        if (!string.IsNullOrEmpty(gFile))
                        {
                            using var sfd = new SaveFileDialog
                            {
                                FileName = Path.GetFileName(gFile),
                                Filter = "G-Code files (*.gcode)|*.gcode|All files (*.*)|*.*",
                                Title = "Salva file G-Code"
                            };
                            if (sfd.ShowDialog() == DialogResult.OK)
                            {
                                await PrinterService.DownloadGcodeAsync(_settings.PrinterIp, _settings.PrinterPort, gFile, sfd.FileName);
                                MessageBox.Show("File G-Code scaricato con successo!", "Download Completato", MessageBoxButtons.OK, MessageBoxIcon.Information);
                            }
                        }
                        break;

                    case "export_csv":
                        using (var sfd = new SaveFileDialog
                        {
                            FileName = $"registro_stampe_{DateTime.Now:yyyyMMdd_HHmm}.csv",
                            Filter = "CSV files (*.csv)|*.csv|All files (*.*)|*.*",
                            Title = "Esporta Registro in CSV"
                        })
                        {
                            if (sfd.ShowDialog() == DialogResult.OK)
                            {
                                ExportJobsToCsv(sfd.FileName);
                                MessageBox.Show("Registro esportato con successo in formato CSV!", "Esportazione Riuscita", MessageBoxButtons.OK, MessageBoxIcon.Information);
                            }
                        }
                        break;

                    case "test_connection":
                        var testIp = root.GetProperty("ip").GetString() ?? _settings.PrinterIp;
                        var testPort = root.GetProperty("port").GetInt32();
                        var st = await PrinterService.GetLiveStatusAsync(testIp, testPort);
                        PostJsonToWeb(new { type = "test_conn_result", success = st.IsConnected });
                        break;

                    case "save_settings":
                        _settings.PrinterIp = root.GetProperty("ip").GetString() ?? _settings.PrinterIp;
                        _settings.PrinterPort = root.GetProperty("port").GetInt32();
                        _settings.CostPerKg = root.GetProperty("costPerKg").GetDouble();
                        _settings.PollIntervalSeconds = root.GetProperty("pollInterval").GetInt32();
                        _settings.Save();

                        _pollTimer.Interval = Math.Max(1000, _settings.PollIntervalSeconds * 1000);
                        await SyncWithPrinterAsync();
                        break;

                    case "cost_changed":
                        _settings.CostPerKg = root.GetProperty("costPerKg").GetDouble();
                        _settings.Save();
                        break;
                }
            }
            catch (Exception ex)
            {
                System.Diagnostics.Debug.WriteLine($"WebMessage error: {ex}");
            }
        }

        private void ExportJobsToCsv(string targetPath)
        {
            var sb = new System.Text.StringBuilder();
            sb.AppendLine("ID,Progetto,NomeFile,Stato,DataAvvio,DataFine,NozzleMm,LayerMm,ProfiloProcesso,ProfiloStampante,InfillPct,TramaInfill,Pareti,Supporti,Stiratura,DurataSecondi,DurataFormattata,StimaSlicer,FilamentoGrammi,FilamentoMetri,CostoEUR,TipoFilamento,ProfiloFilamento,TempNozzle,TempPiatto,VelocitaPareteMmS,VelocitaTravelMmS");

            foreach (var j in _currentJobs)
            {
                var cost = ((j.FilamentG * _settings.CostPerKg) / 1000.0).ToString("F2", System.Globalization.CultureInfo.InvariantCulture);
                sb.AppendLine($"{j.JobId},\"{EscapeCsv(j.ProjectName)}\",\"{EscapeCsv(j.Filename)}\",{j.Status},{j.StartTime},{j.EndTime},{j.NozzleDiameter},{j.LayerHeight},\"{EscapeCsv(j.PrintProfile)}\",\"{EscapeCsv(j.PrinterProfile)}\",\"{j.InfillDensity}\",\"{j.InfillPattern}\",{j.WallLoops},\"{j.SupportLabel}\",\"{j.IroningType}\",{j.PrintDuration},\"{j.PrintDurationStr}\",\"{j.TimeEstStr}\",{j.FilamentG.ToString(System.Globalization.CultureInfo.InvariantCulture)},{j.FilamentM.ToString(System.Globalization.CultureInfo.InvariantCulture)},{cost},{j.FilamentType},\"{EscapeCsv(j.FilamentName)}\",{j.NozzleTemp},{j.BedTemp},{j.OuterWallSpeed},{j.TravelSpeed}");
            }

            File.WriteAllText(targetPath, sb.ToString(), System.Text.Encoding.UTF8);
        }

        private static string EscapeCsv(string val) => val.Replace("\"", "\"\"");

        private void PostJsonToWeb(object obj)
        {
            if (_webView?.CoreWebView2 == null) return;
            try
            {
                var json = JsonSerializer.Serialize(obj);
                _webView.CoreWebView2.PostWebMessageAsJson(json);
            }
            catch { }
        }
    }
}

