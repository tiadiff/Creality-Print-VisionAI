# Uninstall Creality Print Video Upscaler & Restore Original Files
$ErrorActionPreference = "Stop"

$deviceMgrDir = "C:\Program Files\Creality\Creality Print 7.3\resources\web\deviceMgr"
$indexHtml = Join-Path $deviceMgrDir "index.html"
$backupHtml = Join-Path $deviceMgrDir "index.html.bak"
$targetJs = Join-Path $deviceMgrDir "upscale.js"

if (-not (Test-Path $deviceMgrDir)) {
    Write-Warning "Cartella Creality Print non trovata: $deviceMgrDir"
    exit 0
}

# 1. Ripristina index.html da index.html.bak se disponibile
if (Test-Path $backupHtml) {
    Copy-Item -Path $backupHtml -Destination $indexHtml -Force
    Write-Host "Ripristinato index.html originale dal backup: $backupHtml"
} else {
    # Rimuovi il tag script da index.html se non esiste il file di backup
    if (Test-Path $indexHtml) {
        $htmlContent = Get-Content -Path $indexHtml -Raw -Encoding UTF8
        $cleanContent = $htmlContent -replace '\s*<script\s+defer\s+src="\.\/upscale\.js"><\/script>', ''
        Set-Content -Path $indexHtml -Value $cleanContent -Encoding UTF8
        Write-Host "Rimosso tag upscale.js da index.html"
    }
}

# 2. Rimuovi upscale.js dalla cartella di Creality Print
if (Test-Path $targetJs) {
    Remove-Item -Path $targetJs -Force -ErrorAction SilentlyContinue
    Write-Host "Rimosso $targetJs"
}

# 3. Rimuovi avvio automatico HKCU per il supervisor se presente
try {
    $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    if (Get-ItemProperty -Path $runKey -Name "CrealityVisionAISupervisor" -ErrorAction SilentlyContinue) {
        Remove-ItemProperty -Path $runKey -Name "CrealityVisionAISupervisor" -Force
        Write-Host "Rimosso avvio automatico HKCU CrealityVisionAISupervisor."
    }
} catch {
    Write-Warning "Impossibile rimuovere voce di registro HKCU: $_"
}

Write-Host "`n=== DISINSTALLAZIONE COMPLETATA CON SUCCESSO! ==="
Write-Host "Creality Print e' stato ripristinato allo stato originale pulito."

