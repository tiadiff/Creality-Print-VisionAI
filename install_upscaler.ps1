# Install Creality Print Video Upscaler
$ErrorActionPreference = "Stop"

$deviceMgrDir = "C:\Program Files\Creality\Creality Print 7.3\resources\web\deviceMgr"
$indexHtml = Join-Path $deviceMgrDir "index.html"
$backupHtml = Join-Path $deviceMgrDir "index.html.bak"
$targetJs = Join-Path $deviceMgrDir "upscale.js"
$sourceJs = if ($PSScriptRoot) { Join-Path $PSScriptRoot "upscale.js" } else { Join-Path (Get-Location) "upscale.js" }

if (-not (Test-Path $deviceMgrDir)) {
    Write-Error "Cartella Creality Print non trovata: $deviceMgrDir"
    exit 1
}

# 1. Backup originale di index.html se non esiste già
if (-not (Test-Path $backupHtml)) {
    Copy-Item -Path $indexHtml -Destination $backupHtml -Force
    Write-Host "Backup creato: $backupHtml"
}

# 2. Copia upscale.js
Copy-Item -Path $sourceJs -Destination $targetJs -Force
Write-Host "Copiato upscale.js in $targetJs"

# 3. Inietta upscale.js in index.html se non già presente
$htmlContent = Get-Content -Path $indexHtml -Raw -Encoding UTF8
if ($htmlContent -notmatch "upscale\.js") {
    $scriptTag = '<script defer src="./upscale.js"></script>'
    if ($htmlContent -match "(<\/body>)") {
        $htmlContent = $htmlContent -replace "(<\/body>)", "    $scriptTag`n`$1"
    } else {
        $htmlContent = $htmlContent + "`n" + $scriptTag
    }
    Set-Content -Path $indexHtml -Value $htmlContent -Encoding UTF8
    Write-Host "index.html aggiornato con successo con il tag upscale.js!"
} else {
    Write-Host "index.html contiene già il riferimento a upscale.js."
}

# 4. Assegna permessi di modifica alla cartella deviceMgr per gli utenti normali
try {
    $acl = Get-Acl $deviceMgrDir
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule("BUILTIN\Users", "Modify", "ContainerInherit,ObjectInherit", "None", "Allow")
    $acl.AddAccessRule($rule)
    Set-Acl -Path $deviceMgrDir -AclObject $acl
    Write-Host "Permessi di aggiornamento configurati."
} catch {
    Write-Warning "Impossibile aggiornare i permessi ACL (non critico): $_"
}

# 5. Inizializza impostazioni del supervisore con avvio automatico DISATTIVATO di default
$settingsDir = Join-Path $env:LOCALAPPDATA "CrealityVisionAI"
if (-not (Test-Path $settingsDir)) { New-Item -ItemType Directory -Path $settingsDir -Force | Out-Null }
$settingsJson = Join-Path $settingsDir "supervisor_settings.json"
if (-not (Test-Path $settingsJson)) {
    '{"autostart_with_creality": false, "autostart_mode": "stopped"}' | Set-Content -Path $settingsJson -Encoding UTF8
    Write-Host "Configurazione predefinita inizializzata con tutti i server e AI disattivati di default."
}

# Rimuovi eventuale avvio automatico forzato da registro
try {
    $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    if (Get-ItemProperty -Path $runKey -Name "CrealityVisionAISupervisor" -ErrorAction SilentlyContinue) {
        Remove-ItemProperty -Path $runKey -Name "CrealityVisionAISupervisor" -Force
        Write-Host "Avvio automatico del supervisore disattivato di default (avvio manuale)."
    }
} catch {}

Write-Host "`n=== INSTALLAZIONE COMPLETATA CON SUCCESSO! ==="

