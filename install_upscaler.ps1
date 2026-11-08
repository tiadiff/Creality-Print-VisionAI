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

# 5. Configura avvio automatico del controller locale (:8089) per Avvio/Arresto da Creality Print
$repoRoot = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$mgrScript = Join-Path $repoRoot "server_manager.py"
if (Test-Path $mgrScript) {
    $pyCmd = $null
    $pywCmd = $null
    $venvPy = Join-Path $repoRoot ".venv\Scripts\python.exe"
    $venvPyw = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"
    if (Test-Path $venvPyw) {
        $pywCmd = $venvPyw
        $pyCmd = if (Test-Path $venvPy) { $venvPy } else { $venvPyw }
    } else {
        $foundPy = Get-Command python -ErrorAction SilentlyContinue
        $foundPyw = Get-Command pythonw -ErrorAction SilentlyContinue
        if ($foundPyw) { $pywCmd = $foundPyw.Source }
        if ($foundPy) { $pyCmd = $foundPy.Source }
    }

    if ($pywCmd) {
        try {
            $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
            $runVal = "`"$pywCmd`" `"$mgrScript`""
            Set-ItemProperty -Path $runKey -Name "CrealityVisionAISupervisor" -Value $runVal -Type String
            Write-Host "Avvio automatico controller locale configurato ($pywCmd)."
        } catch {
            Write-Warning "Impossibile registrare avvio automatico HKCU: $_"
        }
    }

    if ($pyCmd) {
        try {
            & $pyCmd $mgrScript --ensure-running | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Host "Controller locale (:8089) attivo in background."
            } else {
                Write-Warning "Controller locale (:8089) non ha risposto al check iniziale."
            }
        } catch {
            Write-Warning "Avvio immediato controller locale non riuscito: $_"
        }
    }
}

Write-Host "`n=== INSTALLAZIONE COMPLETATA CON SUCCESSO! ==="

