# Build the Windows folder-style release package for source1 only.
# Usage: powershell -ExecutionPolicy Bypass -File .\build_panel.ps1

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

# Prevent file-lock failures when rebuilding while panel is running.
Get-Process -Name "ChatGPTAssistantPanel" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 600

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

Write-Host "[build] Project: $ProjectRoot"
Write-Host "[build] Python : $Python"

$PythonBasePrefix = & $Python -c "import sys; print(sys.base_prefix)"
$env:TCL_LIBRARY = Join-Path $PythonBasePrefix "tcl\tcl8.6"
$env:TK_LIBRARY = Join-Path $PythonBasePrefix "tcl\tk8.6"
Write-Host "[build] Tcl    : $env:TCL_LIBRARY"
Write-Host "[build] Tk     : $env:TK_LIBRARY"

$ExistingDistCandidates = @()
$LegacyDistRoot = Join-Path $ProjectRoot "dist\ChatGPTAssistantPanel"
if (Test-Path $LegacyDistRoot) {
    $ExistingDistCandidates += $LegacyDistRoot
}
Get-ChildItem -LiteralPath $ProjectRoot -Directory -Filter "dist_build_*" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    ForEach-Object {
        $candidate = Join-Path $_.FullName "ChatGPTAssistantPanel"
        if (Test-Path $candidate) {
            $ExistingDistCandidates += $candidate
        }
    }

$ExistingDistRoot = $null
foreach ($candidate in $ExistingDistCandidates) {
    $dataDir = Join-Path $candidate "data"
    $hasDataFiles = (Test-Path $dataDir) -and (Get-ChildItem -LiteralPath $dataDir -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($hasDataFiles) {
        $ExistingDistRoot = $candidate
        break
    }
}
if (-not $ExistingDistRoot) {
    foreach ($candidate in $ExistingDistCandidates) {
        $profilesDir = Join-Path $candidate "profiles"
        $hasProfileFiles = (Test-Path $profilesDir) -and (Get-ChildItem -LiteralPath $profilesDir -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($hasProfileFiles) {
            $ExistingDistRoot = $candidate
            break
        }
    }
}
if (-not $ExistingDistRoot -and $ExistingDistCandidates.Count -gt 0) {
    $ExistingDistRoot = $ExistingDistCandidates[0]
}

$DistOutputRoot = Join-Path $ProjectRoot "dist"
$DistRoot = Join-Path $DistOutputRoot "ChatGPTAssistantPanel"
$RuntimeStateDirs = @("data")
$RuntimeStateFiles = @(".env", "config.yaml")
$RuntimeBackupRoot = Join-Path $ProjectRoot "build\_dist_runtime_backup"

# Preserve runtime state in existing dist so rebuild does not re-import consumed pools.
if (Test-Path $RuntimeBackupRoot) {
    try {
        Remove-Item -LiteralPath $RuntimeBackupRoot -Recurse -Force -ErrorAction Stop
    } catch {
        $suffix = Get-Date -Format "yyyyMMdd_HHmmss"
        $RuntimeBackupRoot = Join-Path $ProjectRoot ("build\_dist_runtime_backup_" + $suffix)
        Write-Warning "Old runtime backup is locked, switch to new backup path: $RuntimeBackupRoot"
    }
}
New-Item -ItemType Directory -Path $RuntimeBackupRoot -Force | Out-Null

if ($ExistingDistRoot -and (Test-Path $ExistingDistRoot)) {
    Write-Host "[build] Runtime backup source: $ExistingDistRoot"
    foreach ($dir in $RuntimeStateDirs) {
        $src = Join-Path $ExistingDistRoot $dir
        $dst = Join-Path $RuntimeBackupRoot $dir
        if (Test-Path $src) {
            Write-Host "[build] Backup runtime dir $dir"
            Copy-Item -LiteralPath $src -Destination $dst -Recurse -Force
        }
    }
    foreach ($file in $RuntimeStateFiles) {
        $src = Join-Path $ExistingDistRoot $file
        $dst = Join-Path $RuntimeBackupRoot $file
        if (Test-Path $src) {
            Write-Host "[build] Backup runtime file $file"
            Copy-Item -LiteralPath $src -Destination $dst -Force
        }
    }
}

& $Python -m py_compile control_panel_app.py panel_runner.py control_panel\file_registry.py control_panel\text_pool_service.py control_panel\env_service.py
& $Python -m PyInstaller ChatGPTAssistantPanel.spec --noconfirm --clean --distpath $DistOutputRoot
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

if (-not (Test-Path $DistRoot)) {
    throw "PyInstaller output not found: $DistRoot"
}

$CopyDirs = @("data")
foreach ($dir in $CopyDirs) {
    $src = Join-Path $ProjectRoot $dir
    $dst = Join-Path $DistRoot $dir
    $runtimeBackup = Join-Path $RuntimeBackupRoot $dir
    if (Test-Path $runtimeBackup) {
        Write-Host "[build] Skip source dir $dir (runtime backup exists)"
        continue
    }
    if (Test-Path $src) {
        Write-Host "[build] Copy dir $dir"
        Copy-Item -LiteralPath $src -Destination $dst -Recurse -Force
    }
}

$CopyFiles = @(".env", "config.yaml", "recaptcha_solver.py", "get_oauth_rt.py", "README.md")
foreach ($file in $CopyFiles) {
    $src = Join-Path $ProjectRoot $file
    $runtimeBackup = Join-Path $RuntimeBackupRoot $file
    if (($file -eq ".env" -or $file -eq "config.yaml") -and (Test-Path $runtimeBackup)) {
        Write-Host "[build] Skip source file $file (runtime backup exists)"
        continue
    }
    if (Test-Path $src) {
        Write-Host "[build] Copy file $file"
        Copy-Item -LiteralPath $src -Destination (Join-Path $DistRoot $file) -Force
    }
}

# Restore preserved runtime state with highest priority.
foreach ($dir in $RuntimeStateDirs) {
    $src = Join-Path $RuntimeBackupRoot $dir
    $dst = Join-Path $DistRoot $dir
    if (Test-Path $src) {
        Write-Host "[build] Restore runtime dir $dir"
        Copy-Item -LiteralPath $src -Destination $dst -Recurse -Force
    }
}
foreach ($file in $RuntimeStateFiles) {
    $src = Join-Path $RuntimeBackupRoot $file
    $dst = Join-Path $DistRoot $file
    if (Test-Path $src) {
        Write-Host "[build] Restore runtime file $file"
        Copy-Item -LiteralPath $src -Destination $dst -Force
    }
}

$buildIncludeEnv = [string]$env:BUILD_INCLUDE_PLAYWRIGHT_BROWSERS
if ([string]::IsNullOrWhiteSpace($buildIncludeEnv)) {
    $IncludePlaywrightBrowsers = $true
} else {
    $IncludePlaywrightBrowsers = ($buildIncludeEnv.Trim().ToLower() -in @("1", "true", "yes"))
}
if ($IncludePlaywrightBrowsers) {
    $PlaywrightSource = Join-Path $env:LOCALAPPDATA "ms-playwright"
    $PlaywrightTarget = Join-Path $DistRoot "_internal\playwright\driver\package\.local-browsers"
    if (Test-Path $PlaywrightSource) {
        Write-Host "[build] Copy Playwright browsers"
        New-Item -ItemType Directory -Path $PlaywrightTarget -Force | Out-Null
        Get-ChildItem -LiteralPath $PlaywrightSource -Directory | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $PlaywrightTarget $_.Name) -Recurse -Force
        }
    } else {
        Write-Warning "Playwright browsers not found at $PlaywrightSource. Run: .\.venv\Scripts\python -m playwright install chromium"
    }
} else {
    Write-Host "[build] Skip Playwright browser cache copy (minimal package mode)"
}

# Minimal package: do not bundle runtime artifacts that can grow very large.
foreach ($name in @("profiles", "output", "logs")) {
    $path = Join-Path $DistRoot $name
    if (Test-Path $path) {
        Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "[build] Remove runtime dir $name (minimal package mode)"
    }
}

Write-Host "[build] Done: $DistRoot"
