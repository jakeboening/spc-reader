# spc-reader installer for Windows.
#
#   irm https://raw.githubusercontent.com/jakeboening/spc-reader/main/install.ps1 | iex
#
# Installs into %USERPROFILE%\.spc-reader (source + private virtualenv), puts
# command shims in %USERPROFILE%\.spc-reader\bin, and adds that to the user
# PATH. Safe to re-run: it updates an existing install in place.

$ErrorActionPreference = "Stop"

$RepoUrl    = "https://github.com/jakeboening/spc-reader.git"
$ZipUrl     = "https://codeload.github.com/jakeboening/spc-reader/zip/refs/heads/main"
$InstallDir = Join-Path $env:USERPROFILE ".spc-reader"
$SrcDir     = Join-Path $InstallDir "src"
$VenvDir    = Join-Path $InstallDir "venv"
$BinDir     = Join-Path $InstallDir "bin"
$Tools      = @("spc-plot", "spc-plot-cycle", "spc-loadcell-probe", "spc-loadcell-cal")

function Info($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# -- Python 3.10+ --------------------------------------------------------------
$Py = $null
foreach ($candidate in @("py", "python3", "python")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    $pyArgs = if ($candidate -eq "py") { @("-3", "-c") } else { @("-c") }
    & $candidate @pyArgs "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $Py = $candidate
        break
    }
}
if (-not $Py) {
    Write-Host "ERROR: Python 3.10 or newer not found." -ForegroundColor Red
    Write-Host "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then re-run this installer."
    exit 1
}
Info "Using Python via '$Py'"

# -- Fetch the source ----------------------------------------------------------
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$git = Get-Command git -ErrorAction SilentlyContinue
if ($git) {
    if (Test-Path (Join-Path $SrcDir ".git")) {
        Info "Updating source in $SrcDir"
        git -C $SrcDir fetch --depth 1 --quiet origin main
        git -C $SrcDir reset --hard origin/main --quiet
    } else {
        if (Test-Path $SrcDir) { Remove-Item -Recurse -Force $SrcDir }
        Info "Cloning $RepoUrl"
        git clone --depth 1 --quiet $RepoUrl $SrcDir
    }
} else {
    Info "git not found - downloading a source snapshot instead"
    if (Test-Path $SrcDir) { Remove-Item -Recurse -Force $SrcDir }
    $zip = Join-Path $env:TEMP "spc-reader-main.zip"
    Invoke-WebRequest -Uri $ZipUrl -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $InstallDir -Force
    Move-Item (Join-Path $InstallDir "spc-reader-main") $SrcDir
    Remove-Item $zip
}

# -- Virtualenv + package ------------------------------------------------------
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Info "Creating virtual environment in $VenvDir"
    if ($Py -eq "py") { & py -3 -m venv $VenvDir } else { & $Py -m venv $VenvDir }
}
Info "Installing spc-reader and its dependencies (may take a few minutes on first run)"
& $VenvPython -m pip install --quiet --upgrade pip
& $VenvPython -m pip install --quiet --upgrade $SrcDir

# -- Command shims -------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
foreach ($tool in $Tools) {
    $exe = Join-Path $VenvDir "Scripts\$tool.exe"
    $shim = Join-Path $BinDir "$tool.cmd"
    "@echo off`r`n`"$exe`" %*" | Set-Content -Path $shim -Encoding ASCII
}
Info "Created shims in ${BinDir}: $($Tools -join ', ')"

# -- PATH ----------------------------------------------------------------------
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath -split ";") -notcontains $BinDir) {
    [Environment]::SetEnvironmentVariable("Path", "$BinDir;$userPath", "User")
    Info "Added $BinDir to your user PATH - open a NEW terminal for it to take effect"
}

Write-Host ""
Info "spc-reader installed. In a new terminal, try:"
Write-Host "        spc-plot --list-ports"
Write-Host "        spc-plot --mode temperature"
Write-Host "        (re-run this installer anytime to update)"
