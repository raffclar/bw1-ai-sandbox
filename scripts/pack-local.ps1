# Pack the local bw1-decomp clone (E:\bw1-decomp\bw1-decomp by default) for upload to the droplet:
#
#   repo.bundle      `git bundle --all`: every branch, tag and remote-tracking ref with full history.
#                    Only committed git objects go in, so build/, .ninja*, __pycache__, *.o, objdiff
#                    output and orig/ (gitignored) never do. Uncommitted changes are NOT included.
#   repo-head.txt    "<branch> <sha>" of the checked-out HEAD, which the droplet checks out.
#   orig-local.tar.gz  orig/<version>/ for each -Versions entry, plus orig/libs and orig/directx7.0.
#                    Single top folder orig/, which droplet/unpack-orig.py accepts. Other versions stay home.
#
# The game files are copyrighted: write them only to a temporary folder and the droplet. deploy.ps1
# calls this into a fresh %TEMP% folder and deletes it afterwards.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File E:\bw1-decomp\scripts\pack-local.ps1 -OutDir <empty folder>
#   ... -RepoPath E:\bw1-decomp\bw1-decomp -Versions BW1W120,BW1WCI -SkipRepo -SkipOrig

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OutDir,
    [string]$RepoPath = "E:\bw1-decomp\bw1-decomp",
    [string[]]$Versions = @("BW1W120"),
    [switch]$SkipRepo,
    [switch]$SkipOrig
)

$ErrorActionPreference = "Stop"
# "-Versions A,B" arrives as one string through powershell -File: split it.
$Versions = @($Versions | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$RepoPath = (Resolve-Path -LiteralPath $RepoPath).Path
if (-not (Test-Path -LiteralPath (Join-Path $RepoPath ".git"))) { throw "$RepoPath is not a git clone" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path

function Size([string]$p) { "{0:N1} MB" -f ((Get-Item -LiteralPath $p).Length / 1MB) }

# Windows PowerShell turns a native program's stderr into errors under "Stop"; judge by exit code instead.
function Invoke-Native([string]$what, [scriptblock]$cmd) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $out = & $cmd 2>&1 } finally { $ErrorActionPreference = $saved }
    if ($LASTEXITCODE -ne 0) { throw "$what failed: $out" }
    return $out
}

if (-not $SkipRepo) {
    $bundle = Join-Path $OutDir "repo.bundle"
    $branch = ([string](Invoke-Native "git rev-parse" { git -C $RepoPath rev-parse --abbrev-ref HEAD })).Trim()
    $sha = ([string](Invoke-Native "git rev-parse" { git -C $RepoPath rev-parse HEAD })).Trim()
    Invoke-Native "git bundle create" { git -C $RepoPath bundle create -q "$bundle" --all } | Out-Null
    Invoke-Native "git bundle verify" { git -C $RepoPath bundle verify -q "$bundle" } | Out-Null
    # LF, no BOM: read on the droplet.
    [IO.File]::WriteAllText((Join-Path $OutDir "repo-head.txt"), "$branch $sha`n")
    Write-Host "repo.bundle  $(Size $bundle)  (all refs; HEAD $branch @ $($sha.Substring(0, 8)))"
}

if (-not $SkipOrig) {
    foreach ($v in $Versions) {
        if ($v -notmatch '^BW1[A-Z0-9]+$') { throw "bad version name $v" }
    }
    $required = @()
    foreach ($v in $Versions) { $required += "orig/$v/runblack-decrypted.exe" }
    $required += @("orig/libs", "orig/directx7.0/include", "orig/directx7.0/lib")
    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $RepoPath $_)) })
    if ($missing.Count -gt 0) { throw ("missing in ${RepoPath}: " + ($missing -join ", ")) }
    $tar = Get-Command tar.exe -ErrorAction SilentlyContinue
    if ($null -eq $tar) { throw "tar.exe not found (it ships with Windows 10 1803 and later)" }
    $out = Join-Path $OutDir "orig-local.tar.gz"
    $members = @($Versions | ForEach-Object { "orig/$_" }) + @("orig/libs", "orig/directx7.0")
    # No gzip timestamp: the same files give the same archive, so Terraform only re-uploads on a change.
    Invoke-Native "tar" { tar.exe --options gzip:!timestamp -czf "$out" -C "$RepoPath" @members } | Out-Null
    Write-Host "orig-local.tar.gz  $(Size $out)  ($($members -join ', '))"
}
