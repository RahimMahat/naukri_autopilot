# Naukri Autopilot - one-command setup.
#
# Right-click this file -> "Run with PowerShell", or from a terminal:
#     powershell -ExecutionPolicy Bypass -File setup.ps1
#
# Safe to run again at any time; it repairs a half-finished install.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Step($n, $text) { Write-Host "`n[$n] $text" -ForegroundColor Cyan }
function Ok($text)       { Write-Host "    $text" -ForegroundColor Green }
function Die($text) {
    Write-Host "`n    $text" -ForegroundColor Red
    Write-Host "`nSetup stopped. Nothing was broken - fix the above and run it again.`n"
    exit 1
}

Write-Host "`nNaukri Autopilot setup" -ForegroundColor White
Write-Host "Everything installs into this folder. Nothing is sent anywhere."

# -- 1. Python --------------------------------------------------------------
Step 1 "Checking Python"
$py = $null
foreach ($c in @("py -3", "python")) {
    try {
        $exe, $arg = $c.Split(" ")
        $v = & $exe $arg --version 2>&1
        if ($LASTEXITCODE -eq 0) { $py = $c; break }
    } catch { }
}
if (-not $py) {
    Die "Python not found. Install it from https://www.python.org/downloads/ and tick 'Add python.exe to PATH', then run this again."
}
$exe, $arg = $py.Split(" ")
$version = (& $exe $arg --version) -replace "Python ", ""
$parts = $version.Split(".")
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 9)) {
    Die "Python $version is too old. 3.9 or newer is required."
}
Ok "Python $version"

# -- 2. Virtual environment -------------------------------------------------
Step 2 "Creating the virtual environment (.venv)"
if (Test-Path ".venv\Scripts\python.exe") {
    Ok "Already exists - reusing it"
} else {
    & $exe $arg -m venv .venv
    if (-not (Test-Path ".venv\Scripts\python.exe")) { Die "Could not create .venv" }
    Ok "Created"
}
$vpy = Resolve-Path ".venv\Scripts\python.exe"

# -- 3. Dependencies --------------------------------------------------------
Step 3 "Installing dependencies (this is the slow part)"
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install --quiet -e .
if ($LASTEXITCODE -ne 0) { Die "pip install failed - see the output above." }
Ok "Installed"

# -- 4. Browser -------------------------------------------------------------
Step 4 "Downloading the browser Playwright needs (~150 MB, once)"
& $vpy -m playwright install chromium
if ($LASTEXITCODE -ne 0) { Die "Browser download failed. Check your connection and run this again." }
Ok "Ready"

# -- 5. Health --------------------------------------------------------------
Step 5 "Checking what is left to do"
& $vpy -m naukri_autopilot.cli doctor

# -- next steps -------------------------------------------------------------
Write-Host "`n--------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host "Setup done. Anything still marked [FAIL] above is a next step," -ForegroundColor White
Write-Host "not a broken install." -ForegroundColor White
Write-Host "`nIn any new terminal, run this first:" -ForegroundColor White
Write-Host "`n    .venv\Scripts\activate" -ForegroundColor Yellow
Write-Host "`nThen the tool is just its name:" -ForegroundColor White
Write-Host "    naukri-autopilot login       " -NoNewline -ForegroundColor Yellow
Write-Host "sign in to Naukri (once)"
Write-Host "    naukri-autopilot dashboard   " -NoNewline -ForegroundColor Yellow
Write-Host "open the control panel"
Write-Host "    naukri-autopilot doctor      " -NoNewline -ForegroundColor Yellow
Write-Host "check what is wrong"
Write-Host "`nWithout activating, use the full path instead:" -ForegroundColor DarkGray
Write-Host "    .venv\Scripts\naukri-autopilot.exe dashboard" -ForegroundColor DarkGray
Write-Host "--------------------------------------------------------------`n" -ForegroundColor DarkGray
