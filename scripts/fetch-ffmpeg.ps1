<#
.SYNOPSIS
    Downloads the LGPL FFmpeg build the application bundles.

.DESCRIPTION
    The binary is ~110 MB, so it is not kept in version control. This fetches it
    into backend/resource/bin/ffmpeg.exe, where the Rust shell points the
    backend via IMAGEIO_FFMPEG_EXE.

    The build must be LGPL, not GPL. BtbN publishes both; the LGPL variant is
    configured with --disable-libx264 --disable-libx265, which is precisely the
    point: shipping libx264 would mean distributing an AVC encoder and taking on
    the patent licensing that comes with it. The application encodes H.264
    through h264_mf (Windows Media Foundation) instead, so the licence stays
    with the operating system.

    See LICENSES-third-party.md for the full reasoning.

    Also writes licenses/FFMPEG-BUILD.txt, the exact version and configuration
    of the binary that was fetched. That file is what the LGPL source offer in
    licenses/THIRD-PARTY-NOTICES.md points a user at, so it has to describe the
    build actually in the package.

.PARAMETER Release
    BtbN release tag. "latest" tracks their rolling build.
#>
[CmdletBinding()]
param(
    [string]$Release = "latest",
    [string]$Asset = "ffmpeg-n9.0-latest-win64-lgpl-9.0.zip",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path $PSScriptRoot -Parent
$destination = Join-Path $projectDir "backend\resource\bin\ffmpeg.exe"

if ((Test-Path $destination) -and -not $Force) {
    Write-Host "FFmpeg already present at $destination (use -Force to replace)."
    exit 0
}

$url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/$Release/$Asset"
$tempZip = Join-Path ([System.IO.Path]::GetTempPath()) $Asset

Write-Host "Downloading $Asset ..."
Invoke-WebRequest -Uri $url -OutFile $tempZip -UseBasicParsing

try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($tempZip)
    try {
        # The archive nests everything under a versioned directory, so match on
        # the leaf name rather than a fixed path.
        $entry = $archive.Entries |
            Where-Object { $_.Name -eq "ffmpeg.exe" } |
            Select-Object -First 1
        if (-not $entry) { throw "ffmpeg.exe not found inside $Asset." }

        $binDir = Split-Path $destination -Parent
        if (-not (Test-Path $binDir)) {
            New-Item -ItemType Directory -Path $binDir -Force | Out-Null
        }
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $destination, $true)
    } finally {
        $archive.Dispose()
    }
} finally {
    Remove-Item $tempZip -Force -ErrorAction SilentlyContinue
}

# A GPL build here would silently reintroduce the licensing problem this script
# exists to avoid, so refuse it rather than let it ship.
$configuration = & $destination -version 2>&1 | Select-String -Pattern "configuration:"
if ($configuration -match "--enable-gpl") {
    Remove-Item $destination -Force
    throw "Downloaded build is GPL (--enable-gpl). Expected an LGPL build; refusing to keep it."
}

# Record which build actually shipped. What the binary may be redistributed
# under follows from its configuration - --enable-version3 without --enable-gpl
# makes it LGPL v3 - and the source offer in licenses/THIRD-PARTY-NOTICES.md has
# to name a version precisely enough that someone can go and fetch it.
$buildNotes = Join-Path $projectDir "licenses/FFMPEG-BUILD.txt"
$notesHeader = @"
FFmpeg build shipped with Clip Architect
========================================

This is the exact identification of the ffmpeg.exe inside this application,
written by scripts/fetch-ffmpeg.ps1 when the binary was fetched. It records
which external libraries are compiled into it, since that is what determines
the terms the binary is distributed under.

The build is an unmodified LGPL build published by BtbN/FFmpeg-Builds. It is
configured with --enable-version3 and without --enable-gpl, so the binary as a
whole is under LGPL v3; see THIRD-PARTY-NOTICES.md for the source offer.

"@
$banner = & $destination -version 2>&1
($notesHeader + "`r`n" + ($banner -join "`r`n")) | Out-File $buildNotes -Encoding utf8

$sizeMb = [Math]::Round((Get-Item $destination).Length / 1MB, 1)
Write-Host "Installed LGPL FFmpeg at $destination ($sizeMb MB)."
& $destination -version 2>&1 | Select-Object -First 1
