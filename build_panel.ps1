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

$DistRoot = Join-Path $ProjectRoot "dist\ChatGPTAssistantPanel"
$RuntimeStateDirs = @("data", "output", "profiles", "logs")
$RuntimeStateFiles = @(".env", "config.yaml")
$RuntimeBackupRoot = Join-Path $ProjectRoot "build\_dist_runtime_backup"

# Preserve runtime state in existing dist so rebuild does not re-import consumed pools.
if (Test-Path $RuntimeBackupRoot) {
    Remove-Item -LiteralPath $RuntimeBackupRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $RuntimeBackupRoot -Force | Out-Null

if (Test-Path $DistRoot) {
    foreach ($dir in $RuntimeStateDirs) {
        $src = Join-Path $DistRoot $dir
        $dst = Join-Path $RuntimeBackupRoot $dir
        if (Test-Path $src) {
            Write-Host "[build] Backup runtime dir $dir"
            Copy-Item -LiteralPath $src -Destination $dst -Recurse -Force
        }
    }
    foreach ($file in $RuntimeStateFiles) {
        $src = Join-Path $DistRoot $file
        $dst = Join-Path $RuntimeBackupRoot $file
        if (Test-Path $src) {
            Write-Host "[build] Backup runtime file $file"
            Copy-Item -LiteralPath $src -Destination $dst -Force
        }
    }
}

& $Python -m py_compile control_panel_app.py panel_runner.py control_panel\file_registry.py control_panel\text_pool_service.py control_panel\env_service.py
& $Python -m PyInstaller ChatGPTAssistantPanel.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

if (-not (Test-Path $DistRoot)) {
    throw "PyInstaller output not found: $DistRoot"
}

$CopyDirs = @("data", "output", "profiles", "logs")
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
    } elseif ($dir -eq "logs") {
        New-Item -ItemType Directory -Path $dst -Force | Out-Null
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

Write-Host "[build] Done: $DistRoot"
