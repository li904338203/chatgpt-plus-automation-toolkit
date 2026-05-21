Param(
    [string]$SourceDist = ".\dist\ChatGPTAssistantPanel",
    [string]$OutRoot = ".\delivery",
    [switch]$ZipPackage,
    [switch]$IncludeEnv,
    [switch]$IncludeOutput,
    [switch]$IncludeProfiles,
    [switch]$IncludeLogs
)

$ErrorActionPreference = "Stop"

function Resolve-FullPath([string]$p) {
    return [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $p).Path)
}

if (-not (Test-Path -LiteralPath $SourceDist)) {
    throw "Source dist not found: $SourceDist"
}

$src = Resolve-FullPath $SourceDist
$outRootFull = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $OutRoot))
if (-not (Test-Path -LiteralPath $outRootFull)) {
    New-Item -ItemType Directory -Path $outRootFull | Out-Null
}

$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$releaseDir = Join-Path $outRootFull ("ChatGPTAssistantPanel_release_" + $ts)

New-Item -ItemType Directory -Path $releaseDir | Out-Null

$exeName = "ChatGPTAssistantPanel.exe"
$mustHave = @(
    $exeName,
    "_internal",
    "data",
    "config.yaml"
)

foreach ($item in $mustHave) {
    if (-not (Test-Path -LiteralPath (Join-Path $src $item))) {
        throw "Missing required item in dist: $item"
    }
}

Write-Host "[release] copying runtime files..."
robocopy $src $releaseDir /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) {
    throw "robocopy failed with code $LASTEXITCODE"
}

if (-not $IncludeEnv) {
    $envFile = Join-Path $releaseDir ".env"
    if (Test-Path -LiteralPath $envFile) {
        Remove-Item -LiteralPath $envFile -Force
    }
}

if (-not $IncludeOutput) {
    $p = Join-Path $releaseDir "output"
    if (Test-Path -LiteralPath $p) {
        Remove-Item -LiteralPath $p -Recurse -Force
        New-Item -ItemType Directory -Path $p | Out-Null
    }
}

if (-not $IncludeProfiles) {
    $p = Join-Path $releaseDir "profiles"
    if (Test-Path -LiteralPath $p) {
        Remove-Item -LiteralPath $p -Recurse -Force
        New-Item -ItemType Directory -Path $p | Out-Null
    }
}

if (-not $IncludeLogs) {
    $p = Join-Path $releaseDir "logs"
    if (Test-Path -LiteralPath $p) {
        Remove-Item -LiteralPath $p -Recurse -Force
        New-Item -ItemType Directory -Path $p | Out-Null
    }
}

$readmePath = Join-Path $releaseDir "START_HERE.txt"
$readmeTemplate = Join-Path (Split-Path -Parent $PSCommandPath) "START_HERE_CN.txt"
if (Test-Path -LiteralPath $readmeTemplate) {
    Copy-Item -LiteralPath $readmeTemplate -Destination $readmePath -Force
} else {
    $readmeFallback = @(
        "Quick Start"
        "1) Unzip to a short ASCII path, for example: D:\ChatGPTAssistantPanel"
        "2) Add this folder to antivirus allow-list"
        "3) Run as administrator once: ChatGPTAssistantPanel.exe"
    ) -join [Environment]::NewLine
    Set-Content -LiteralPath $readmePath -Value $readmeFallback -Encoding UTF8
}

if ($ZipPackage) {
    $zipPath = "$releaseDir.zip"
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Compress-Archive -Path (Join-Path $releaseDir "*") -DestinationPath $zipPath -Force
    Write-Host "[release] zip created: $zipPath"
}

Write-Host "[release] done: $releaseDir"
Write-Host "[release] include env: $IncludeEnv"
Write-Host "[release] include output: $IncludeOutput"
Write-Host "[release] include profiles: $IncludeProfiles"
Write-Host "[release] include logs: $IncludeLogs"
