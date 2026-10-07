param(
    [Parameter(Mandatory = $true)][string]$Src,
    [Parameter(Mandatory = $true)][string]$Dst
)
# Render docx -> pdf via WPS COM automation (KWPS.Application).
# ASCII-only script body: pass Chinese paths as arguments (see project lesson #12).
$ErrorActionPreference = 'Stop'
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
} finally {
    if ($app) {
        $app.Quit()
        [System.Runtime.InteropServices.Marshal]::ReleaseComObject($app) | Out-Null
    }
}
