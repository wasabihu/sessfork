# One-line Windows installer for sessfork-mcp + Companion Extension + Moli (zero Python, zero Node.js, SHA-256 verified).
#
# Usage:
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/wasabihu/sessfork/main/install.ps1 | iex"
#
# Optional environment variables:
#   $env:SESSFORK_VERSION        = "v0.2.1"
#   $env:SESSFORK_INSTALL_DIR    = "$env:USERPROFILE\.local\bin"
#   $env:SESSFORK_SKIP_MOLI      = "1"   # Skip auto-installing lexmount/moli if missing
#   $env:SESSFORK_SKIP_MCP_SETUP = "1"   # Skip auto-wiring detected AI client MCP configs

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { $null = $_ }

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
    throw "Invalid release tag '$Version' (expected semantic version tag like v0.2.1)."
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

function Add-UserPathDir {
    param([string]$Dir)
    if ([string]::IsNullOrWhiteSpace($Dir) -or $Dir.Contains(";") -or -not (Test-Path $Dir)) {
        return
    }
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $PathParts = if ($UserPath) { $UserPath -split ";" | Where-Object { $_ } } else { @() }
    if ($PathParts -notcontains $Dir) {
        $NewPath = ($PathParts + $Dir) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
        $env:Path = "$Dir;$env:Path"
        Write-Host "  + Added $Dir to User PATH"
    }
}

function Find-LocalMoli {
    $cmd = Get-Command "moli.exe" -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Path $cmd.Source)) { return $cmd.Source }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Moli\bin\moli.exe"),
        (Join-Path $env:USERPROFILE ".local\bin\moli.exe"),
        (Join-Path $env:USERPROFILE ".cargo\bin\moli.exe"),
        (Join-Path $env:LOCALAPPDATA "moli\moli.exe")
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    return $null
}

function Wire-OpenCodeFallback {
    param(
        [string]$ConfigPath,
        [string]$BinPath
    )
    $StatePath = Join-Path $env:USERPROFILE ".sessfork\state.json"
    $OutputDir = Join-Path $env:USERPROFILE ".sessfork\playwright-output"
    $ParentDir = Split-Path -Parent $ConfigPath
    if ($ParentDir -and -not (Test-Path $ParentDir)) {
        New-Item -ItemType Directory -Force -Path $ParentDir | Out-Null
    }
    $Doc = [ordered]@{}
    if (Test-Path $ConfigPath) {
        $Raw = Get-Content -Path $ConfigPath -Raw -Encoding UTF8
        if (-not [string]::IsNullOrWhiteSpace($Raw)) {
            $Parsed = $Raw | ConvertFrom-Json
            if ($null -eq $Parsed -or -not ($Parsed -is [System.Management.Automation.PSCustomObject])) {
                throw "Expected JSON object in $ConfigPath"
            }
            foreach ($Prop in $Parsed.PSObject.Properties) {
                $Doc[$Prop.Name] = $Prop.Value
            }
        }
    }
    if (-not $Doc.Contains('$schema')) {
        $Doc['$schema'] = "https://opencode.ai/config.json"
    }
    $McpMap = [ordered]@{}
    if ($Doc.Contains("mcp")) {
        $ExistingMcp = $Doc["mcp"]
        if ($null -eq $ExistingMcp -or -not ($ExistingMcp -is [System.Management.Automation.PSCustomObject])) {
            throw "'mcp' is not a JSON object in $ConfigPath"
        }
        foreach ($Prop in $ExistingMcp.PSObject.Properties) {
            $McpMap[$Prop.Name] = $Prop.Value
        }
    }
    $McpMap["sessfork-moli"] = [ordered]@{
        type    = "local"
        command = @(
            $BinPath,
            "--storage-state=$StatePath",
            "--output-dir=$OutputDir"
        )
        enabled = $true
    }
    $Doc["mcp"] = $McpMap
    $JsonOut = ($Doc | ConvertTo-Json -Depth 10) + "`n"
    $TmpFile = "$ConfigPath.sessfork.tmp"
    [System.IO.File]::WriteAllText($TmpFile, $JsonOut, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -Force $TmpFile $ConfigPath
    Write-Host "Wired MCP server 'sessfork-moli' -> $ConfigPath"
}

$TmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("sessfork-install-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $TmpDir | Out-Null

try {
    Write-Host "[1/4] Downloading sessfork-mcp $Version (windows-x64)..."
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
    Add-UserPathDir -Dir $InstallDir
    $VerOut = & $DestBin --version
    Write-Host "  OK: $VerOut -> $DestBin"

    # Download companion extension archive (HTTP 404 = not bundled; any other failure or checksum mismatch aborts).
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
            Write-Host "  Note: companion extension archive not bundled in $Version."
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
        Write-Host "  OK: Companion extension -> $ExtDir"
    }

    Write-Host "[2/4] Checking Moli headless browser engine..."
    $MoliPath = Find-LocalMoli
    if (-not $MoliPath -and $env:SESSFORK_SKIP_MOLI -ne "1") {
        Write-Host "  Installing lexmount/moli via official installer..."
        $MoliInstaller = Join-Path $TmpDir "moli-installer.ps1"
        Invoke-WebRequest -Uri "https://github.com/lexmount/moli/releases/latest/download/moli-installer.ps1" -OutFile $MoliInstaller -UseBasicParsing
        & powershell -NoProfile -ExecutionPolicy ByPass -File $MoliInstaller
        $MoliPath = Find-LocalMoli
    }
    if ($MoliPath) {
        Add-UserPathDir -Dir (Split-Path -Parent $MoliPath)
        $MoliVer = & $MoliPath --version
        Write-Host "  OK: $MoliVer -> $MoliPath"
    } else {
        Write-Host "  Skipped Moli auto-install (run moli-installer.ps1 later if needed)."
    }

    Write-Host "[3/4] Registering Chrome/Edge/Brave Native Messaging host (fixed extension ID)..."
    & $DestBin register-host
    if ($LASTEXITCODE -ne 0) {
        throw "sessfork-mcp register-host exited with code $LASTEXITCODE"
    }

    Write-Host "[4/4] Auto-wiring detected AI coding clients..."
    $Wired = @()
    if ($env:SESSFORK_SKIP_MCP_SETUP -ne "1") {
        $ClientTargets = @(
            @{ Name = "Antigravity";        Path = (Join-Path $env:USERPROFILE ".gemini\config\mcp_config.json");      Dir = (Join-Path $env:USERPROFILE ".gemini\config");      Format = "standard"; Cmd = $null },
            @{ Name = "Antigravity Legacy"; Path = (Join-Path $env:USERPROFILE ".gemini\antigravity\mcp_config.json"); Dir = $null;                                             Format = "standard"; Cmd = $null },
            @{ Name = "Cursor";             Path = (Join-Path $env:USERPROFILE ".cursor\mcp.json");                    Dir = (Join-Path $env:USERPROFILE ".cursor");             Format = "standard"; Cmd = $null },
            @{ Name = "Claude Code";        Path = (Join-Path $env:USERPROFILE ".claude.json");                        Dir = $null;                                             Format = "standard"; Cmd = $null },
            @{ Name = "OpenCode";           Path = (Join-Path $env:USERPROFILE ".config\opencode\opencode.json");      Dir = (Join-Path $env:USERPROFILE ".config\opencode");    Format = "opencode"; Cmd = "opencode" }
        )
        foreach ($T in $ClientTargets) {
            $HasCmd = $T.Cmd -and (Get-Command $T.Cmd -ErrorAction SilentlyContinue)
            $Exists = (Test-Path $T.Path) -or ($T.Dir -and (Test-Path $T.Dir)) -or $HasCmd
            if ($Exists) {
                if ($T.Format -eq "opencode") {
                    # Prefer native Rust `--format opencode`; fall back cleanly on pre-0.2.2 binaries without triggering PS 5.1 NativeCommandError.
                    $PrevEAP = $ErrorActionPreference
                    $SetupExit = 2
                    try {
                        $ErrorActionPreference = "Continue"
                        & $DestBin setup --format opencode --mcp-file $T.Path 2>&1 | Out-Null
                        $SetupExit = $LASTEXITCODE
                    } finally {
                        $ErrorActionPreference = $PrevEAP
                    }
                    if ($SetupExit -eq 0) {
                        Write-Host "Wired MCP server 'sessfork-moli' -> $($T.Path)"
                        $Wired += "$($T.Name) ($($T.Path))"
                    } elseif ($SetupExit -eq 2) {
                        try {
                            Wire-OpenCodeFallback -ConfigPath $T.Path -BinPath $DestBin
                            $Wired += "$($T.Name) ($($T.Path))"
                        } catch {
                            Write-Host "  Warning: skipped $($T.Name) ($($T.Path)): $($_.Exception.Message)"
                        }
                    } else {
                        Write-Host "  Warning: skipped $($T.Name) ($($T.Path)) due to setup exit code $SetupExit"
                    }
                } else {
                    & $DestBin setup --mcp-file $T.Path
                    if ($LASTEXITCODE -eq 0) {
                        $Wired += "$($T.Name) ($($T.Path))"
                    } else {
                        Write-Host "  Warning: skipped $($T.Name) ($($T.Path)) due to setup exit code $LASTEXITCODE"
                    }
                }
            }
        }
    }
    if ($Wired.Count -eq 0) {
        Write-Host '  No pre-existing MCP config detected. Wire manually anytime via:'
        Write-Host '    sessfork-mcp setup --mcp-file "$env:USERPROFILE\.cursor\mcp.json"'
        Write-Host '    sessfork-mcp setup --mcp-file "$env:USERPROFILE\.config\opencode\opencode.json"'
    }

    $ClipboardNote = ""
    if ($ExtDownloaded) {
        try {
            Set-Clipboard -Value $ExtDir
            $ClipboardNote = " (path copied to clipboard - just press Ctrl+V + Enter!)"
        } catch {
            $null = $_
        }
    }

    Write-Host ""
    Write-Host "All set! Only 1 quick browser step left:"
    Write-Host "  1. Open chrome://extensions (or edge://extensions), enable Developer mode,"
    Write-Host "     click 'Load unpacked' and select: $ExtDir$ClipboardNote"
    Write-Host "  2. Click the sessfork icon on any logged-in site (already Connected!) -> Fork This Site."
} finally {
    if (Test-Path $TmpDir) { Remove-Item -Recurse -Force $TmpDir }
}
