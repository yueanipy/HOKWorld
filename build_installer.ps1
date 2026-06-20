# Build HOKWorldScript-<version>-Setup.exe: PyInstaller onedir -> Inno Setup -> SHA-256 sidecar.
# Single source of version = version.py (GUI / installer / update check stay aligned).
#
# Usage:   powershell -ExecutionPolicy Bypass -File build_installer.ps1
# Output:  installer\Output\HOKWorldScript-<version>-Setup.exe (+ .sha256)
# (ASCII-only on purpose: Windows PowerShell 5.1 parses .ps1 in the ANSI codepage.)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $root

# ---- 1) Read version (single source) ----
$m = Select-String -Path "version.py" -Pattern '__version__\s*=\s*"([^"]+)"'
if (-not $m) { throw "version.py: __version__ not found" }
$ver = $m.Matches[0].Groups[1].Value
Write-Host "==> Version $ver" -ForegroundColor Cyan

# ---- 2) Locate ISCC (Inno Setup compiler) ----
$isccCandidates = @(
  "$root\.tools\innosetup\ISCC.exe",
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
  $cmd = Get-Command iscc -ErrorAction SilentlyContinue
  if ($cmd) { $iscc = $cmd.Source }
}
if (-not $iscc) { throw "ISCC.exe not found. Install Inno Setup 6: https://jrsoftware.org/isdl.php" }
Write-Host "==> ISCC: $iscc"

# ---- 3) PyInstaller onedir ----
Write-Host "==> PyInstaller (onedir) ..." -ForegroundColor Cyan
python -m PyInstaller --noconfirm --clean HOKWorld.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
$dist = "$root\dist\HOKWorld"
if (-not (Test-Path "$dist\HOKWorld.exe")) { throw "missing $dist\HOKWorld.exe" }

# ---- 4) Inno Setup compile ----
Write-Host "==> Inno Setup (installer) ..." -ForegroundColor Cyan
& $iscc "/DMyAppVersion=$ver" "/DMyDistDir=$dist" "installer\HOKWorldScript.iss"
if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }

$setup = "$root\installer\Output\HOKWorldScript-$ver-Setup.exe"
if (-not (Test-Path $setup)) { throw "missing $setup" }

# ---- 5) SHA-256 sidecar (uploaded with the GitHub Release; client verifies on update) ----
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
"$hash  HOKWorldScript-$ver-Setup.exe" | Out-File "$setup.sha256" -Encoding ascii
$sizeMB = [math]::Round((Get-Item $setup).Length / 1MB, 1)

Write-Host ""
Write-Host "==================== DONE ====================" -ForegroundColor Green
Write-Host "Installer : $setup"
Write-Host "Size      : $sizeMB MB"
Write-Host "SHA-256   : $hash"
