# Pack the original game files the BW1W120 build needs into one .tar.gz for Terraform to upload
# (orig_source = "archive"). The files are copyrighted: the archive is gitignored and only ever
# goes to your own droplet.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File E:\bw1-decomp\scripts\pack-orig.ps1 -OrigDir C:\path\to\bw1-decomp\orig
#   ... -Out E:\bw1-decomp\orig-BW1W120.tar.gz   # default
#
# It packs orig/BW1W120 (the decrypted exe and the four DLLs), orig/libs (all CRT / Intel libs)
# and orig/directx7.0, and refuses if any required file is missing.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OrigDir,
    [string]$Out = (Join-Path (Split-Path $PSScriptRoot -Parent) "orig-BW1W120.tar.gz"),
    [string]$Version = "BW1W120"
)

$ErrorActionPreference = "Stop"
$OrigDir = (Resolve-Path -LiteralPath $OrigDir).Path
if ((Split-Path $OrigDir -Leaf) -ne "orig") { throw "-OrigDir must be the folder named 'orig' (it holds $Version, libs and directx7.0)" }

$required = @(
    "$Version/runblack-decrypted.exe",
    "$Version/LHaudiodllR.dll",
    "$Version/LHLogR.dll",
    "$Version/LHMultiplayerR.dll",
    "$Version/LHDialogLib.dll",
    "libs/msvc6.5/libcmt.lib",
    "libs/msvc6.5/libcpmt.lib",
    "libs/msvc6.5/msvcrtd.lib",
    "libs/msvc6.4/libcmt.lib",
    "libs/msvc6.0/libcimt.lib",
    "libs/amaths-2.0/amaths.lib",
    "libs/icc-5.0.115/libircmt.lib",
    "directx7.0/include/ddraw.h",
    "directx7.0/lib/ddraw.lib"
)
$missing = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $OrigDir $_)) })
if ($missing.Count -gt 0) { throw ("Missing under ${OrigDir}: " + ($missing -join ", ")) }

$tar = Get-Command tar.exe -ErrorAction SilentlyContinue
if ($null -eq $tar) { throw "tar.exe not found (it ships with Windows 10 1803 and later)" }

$parent = Split-Path $OrigDir -Parent
& tar.exe -czf "$Out" -C "$parent" "orig/$Version" "orig/libs" "orig/directx7.0"
if ($LASTEXITCODE -ne 0) { throw "tar failed" }
$size = [math]::Round((Get-Item -LiteralPath $Out).Length / 1MB, 1)
Write-Host "Wrote $Out ($size MB). Set orig_archive_path to it in terraform.tfvars."
