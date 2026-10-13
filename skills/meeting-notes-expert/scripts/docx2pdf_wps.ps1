param(
    [Parameter(Mandatory = $true)][string]$Src,
    [Parameter(Mandatory = $true)][string]$Dst
)
# Render docx -> pdf via WPS COM automation (KWPS.Application).
# ASCII-only script body: pass Chinese paths as arguments (see project lesson #12).
# Lesson #77: WPS COM dies in-process with "RPC server unavailable" (0x800706BA)
# when Open/Export get a RELATIVE path -- force both to absolute up front.
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Src)) { throw "SRC_NOT_FOUND $Src" }
$Src = (Resolve-Path -LiteralPath $Src).Path
$dstDir = Split-Path -Parent $Dst
if ($dstDir) {
    if (-not (Test-Path -LiteralPath $dstDir)) { New-Item -ItemType Directory -Force -Path $dstDir | Out-Null }
    $Dst = Join-Path ((Resolve-Path -LiteralPath $dstDir).Path) (Split-Path -Leaf $Dst)
} else {
    $Dst = [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $Dst))
}
$app = $null
$doc = $null
try {
    $app = New-Object -ComObject KWPS.Application
    $app.Visible = $false
    $app.DisplayAlerts = 0
    $doc = $app.Documents.Open($Src, $false, $true)
    $doc.ExportAsFixedFormat($Dst, 17)
    Write-Output ("PDF_OK " + $Dst)
    $doc.Close(0)
    $doc = $null
} finally {
    try { if ($doc) { $doc.Close(0) } } catch { }
    try { if ($app) { $app.Quit() } } catch { }
    try { if ($app) { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($app) | Out-Null } } catch { }
}
