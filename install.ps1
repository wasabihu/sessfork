# One-line Windows installer for sessfork-mcp (zero Python, zero Node.js, SHA-256 verified).
#
# Usage:
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/wasabihu/sessfork/main/install.ps1 | iex"
#
# Optional environment variables:
#   $env:SESSFORK_VERSION = "v0.2.0"
#   $env:SESSFORK_INSTALL_DIR = "$env:USERPROFILE\.local\bin"

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo = "wasabihu/sessfork"
$InstallDir = if ($env:SESSFORK_INSTALL_DIR) { $env:SESSFORK_INSTALL_DIR.Trim() } else { Join-Path $env:USERPROFILE ".local\bin" }
$ExtDir = Join-Path $env:USERPROFILE ".sessfork\extension"

if ([Environment]::Is64BitOperatingSystem -eq $false) {
    throw "sessfork-mcp requires 64-bit Windows."
}
if ([string]::IsNullOrWhiteSpace($InstallDir) -or $InstallDir.Contains(";")) {
    throw "Invalid SESSFORK_INSTALL_DIR: must be a non-empty path without ';'."
}

$Version = $env:SESSFORK_VERSION
if (-not $Version) {
    $Release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ "User-Agent" = "sessfork-installer" }
    $Version = $Release.tag_name
}
if (-not $Version -or $Version -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.]+)?$') {
    throw "Invalid release tag '$Version' (expected semantic version tag like v0.2.0)."
}

$Asset = "sessfork-mcp-windows-x64.zip"
$ExtAsset = "sessfork-extension.zip"
$BaseUrl = "https://github.com/$Repo/releases/download/$Version"

function Get-ExpectedChecksum {
    param([string]$SumsFile, [string]$AssetName)
    foreach ($Line in Get-Content -Path $SumsFile) {
        if ($Line -match '^([0-9a-fA-F]{64})\s+\*?(.+)$' -and $Matches[2].Trim() -eq $AssetName) {
            return $Matches[1].ToLowerInvariant()
        }
    }
    return $null
}

$TmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("sessfork-install-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $TmpDir | Out-Null

try {
    Write-Host "Downloading sessfork-mcp $Version (windows-x64)..."
    $ZipPath = Join-Path $TmpDir $Asset
    $SumsPath = Join-Path $TmpDir "SHA256SUMS.txt"
    Invoke-WebRequest -Uri "$BaseUrl/$Asset" -OutFile $ZipPath -UseBasicParsing
    Invoke-WebRequest -Uri "$BaseUrl/SHA256SUMS.txt" -OutFile $SumsPath -UseBasicParsing

    $Expected = Get-ExpectedChecksum -SumsFile $SumsPath -AssetName $Asset
    if (-not $Expected) {
        throw "Checksum for $Asset not found in SHA256SUMS.txt"
    }
    $Actual = (Get-FileHash -Path $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -ne $Expected) {
        throw "SHA-256 mismatch for $Asset (expected $Expected, got $Actual)"
    }

    $ExtractDir = Join-Path $TmpDir "bin"
    Expand-Archive -Path $ZipPath -DestinationPath $ExtractDir -Force
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $DestBin = Join-Path $InstallDir "sessfork-mcp.exe"
    Copy-Item -Path (Join-Path $ExtractDir "sessfork-mcp.exe") -Destination $DestBin -Force

    # Download companion extension archive only if published in this release (HTTP 404 = not bundled).
    # Once downloaded, SHA-256 verification is strictly fail-closed outside the download catch!
    $ExtZip = Join-Path $TmpDir $ExtAsset
    $ExtDownloaded = $false
    try {
        Invoke-WebRequest -Uri "$BaseUrl/$ExtAsset" -OutFile $ExtZip -UseBasicParsing
        $ExtDownloaded = $true
    } catch {
        $StatusCode = $null
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            $StatusCode = [int]$_.Exception.Response.StatusCode
        }
        if ($StatusCode -eq 404) {
            Write-Host "Note: companion extension archive not bundled in $Version (clone extension/ from GitHub)."
        } else {
            throw
        }
    }

    if ($ExtDownloaded) {
        $ExtExpected = Get-ExpectedChecksum -SumsFile $SumsPath -AssetName $ExtAsset
        if (-not $ExtExpected) {
            throw "Checksum for $ExtAsset missing in SHA256SUMS.txt (refusing unverified extension install)"
        }
        $ExtActual = (Get-FileHash -Path $ExtZip -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($ExtActual -ne $ExtExpected) {
            throw "SHA-256 mismatch for $ExtAsset (expected $ExtExpected, got $ExtActual)"
        }
        if (Test-Path $ExtDir) { Remove-Item -Recurse -Force $ExtDir }
        New-Item -ItemType Directory -Force -Path $ExtDir | Out-Null
        Expand-Archive -Path $ExtZip -DestinationPath $ExtDir -Force
        Write-Host "Installed verified companion extension -> $ExtDir"
    }

    # Add $InstallDir to current user's PATH if missing.
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $PathParts = if ($UserPath) { $UserPath -split ";" | Where-Object { $_ } } else { @() }
    if ($PathParts -notcontains $InstallDir) {
        $NewPath = ($PathParts + $InstallDir) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
        $env:Path = "$InstallDir;$env:Path"
        Write-Host "Added $InstallDir to your User PATH."
    }

    $VerOut = & $DestBin --version
    Write-Host "Installed $VerOut -> $DestBin"
    Write-Host ""
    Write-Host "Next steps:"
    Write-Host "  1. Install Moli headless engine (if not installed):"
    Write-Host '     powershell -ExecutionPolicy ByPass -c "irm https://github.com/lexmount/moli/releases/latest/download/moli-installer.ps1 | iex"'
    Write-Host "  2. Load unpacked Chrome/Edge extension from: $ExtDir"
    Write-Host '  3. Click the sessfork extension icon and run: sessfork-mcp register-host --extension-id <ID>'
    Write-Host "  4. Wire into your AI client, e.g.:"
    Write-Host "     sessfork-mcp setup --mcp-file `"$env:USERPROFILE\.cursor\mcp.json`""
} finally {
    if (Test-Path $TmpDir) { Remove-Item -Recurse -Force $TmpDir }
}
