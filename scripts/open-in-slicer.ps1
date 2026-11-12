# Print Pantry "printpantry://" link handler (Windows).
#
# Bambu Studio's own bambustudio:// links only download from MakerWorld, so the
# web UI sends Bambu Studio files through this script instead:
#
#   printpantry://open?slicer=bambu&file=<url-encoded http link to the file>
#
# It downloads the file into %TEMP%\PrintPantry and starts the slicer with it.
# Only links to the Print Pantry servers listed in allowed-hosts.txt (next to
# this script) are followed. Installed by scripts\install-slicer-links.cmd.
param([string]$Link = "")

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Web, System.Windows.Forms

function Fail($msg) {
    [System.Windows.Forms.MessageBox]::Show($msg, "Print Pantry", "OK", "Warning") | Out-Null
    exit 1
}

try {
    $uri = [Uri]$Link
    if ($uri.Scheme -ne "printpantry") { Fail "Not a Print Pantry link: $Link" }
    $q = [System.Web.HttpUtility]::ParseQueryString($uri.Query)
    $slicer = $q["slicer"]
    $file = [Uri]$q["file"]
} catch { Fail "Could not read the link: $Link" }

if ($file.Scheme -notin @("http", "https")) { Fail "Only http and https downloads are allowed." }
if (-not $file.AbsolutePath.StartsWith("/api/dl/")) { Fail "Not a Print Pantry download link." }

$hostsFile = Join-Path $PSScriptRoot "allowed-hosts.txt"
$allowed = @()
if (Test-Path $hostsFile) { $allowed = Get-Content $hostsFile | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ } }
if ($file.Authority.ToLower() -notin $allowed) {
    Fail "Print Pantry at $($file.Authority) is not in $hostsFile. Add it on its own line if you trust it."
}

$protocols = @{ bambu = "bambustudio"; snapmaker = "snapmaker-orca" }
if (-not $protocols.ContainsKey($slicer)) { Fail "Unknown slicer '$slicer'." }
# Find the slicer from the link handler it registered when it was installed.
$exe = $null
foreach ($root in "HKCU:\Software\Classes", "Registry::HKEY_CLASSES_ROOT") {
    $key = "$root\$($protocols[$slicer])\shell\open\command"
    if (Test-Path $key) {
        $cmd = (Get-ItemProperty $key)."(default)"
        if ($cmd -match '^\s*"([^"]+)"') { $exe = $Matches[1] } else { $exe = ($cmd -split '\s+')[0] }
        break
    }
}
if (-not $exe -or -not (Test-Path $exe)) { Fail "Could not find the $slicer slicer. Is it installed?" }

$name = [Uri]::UnescapeDataString($file.Segments[-1])
$name = ($name -replace '[^A-Za-z0-9._ -]', '_')
if ($name -notmatch '\.(stl|3mf|step|stp|obj)$') { Fail "Unexpected file type: $name" }
$dir = Join-Path $env:TEMP "PrintPantry"
New-Item -ItemType Directory -Force $dir | Out-Null
$dest = Join-Path $dir $name

try {
    Invoke-WebRequest -Uri $file -OutFile $dest -UseBasicParsing
} catch { Fail "Download failed: $($_.Exception.Message)" }

Start-Process -FilePath $exe -ArgumentList "`"$dest`""
