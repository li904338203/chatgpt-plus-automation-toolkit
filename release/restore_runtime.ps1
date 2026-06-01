$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$zip = Join-Path $here "ChatGPTAssistantPanel-public-runtime.zip"
$parts = Get-ChildItem -LiteralPath $here -Filter "ChatGPTAssistantPanel-public-runtime.zip.part*" | Sort-Object Name
if (-not $parts) { throw "No runtime parts found." }
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
$out = [System.IO.File]::Open($zip, [System.IO.FileMode]::CreateNew)
try {
    foreach ($part in $parts) {
        Write-Host "Merging $($part.Name)"
        $in = [System.IO.File]::OpenRead($part.FullName)
        try { $in.CopyTo($out) } finally { $in.Dispose() }
    }
} finally { $out.Dispose() }
$dest = Join-Path $here "runtime"
if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Recurse -Force }
Expand-Archive -LiteralPath $zip -DestinationPath $dest -Force
Write-Host "Ready: $dest\ChatGPTAssistantPanel\ChatGPTAssistantPanel.exe"
Write-Host "Edit .env/config.yaml and data pools before running."
