# Make the READ-ONLY deploy key the droplet uses to clone the private openblack/bw1-build repo (the
# game files). Option (a) in the README, recommended over a token.
#
# It writes an ed25519 keypair to E:\bw1-decomp\keys\ (gitignored) and prints the public key and the
# GitHub page to add it on. Add it there with "Allow write access" UNCHECKED. Adding a deploy key needs
# admin rights on openblack/bw1-build; if you don't have them, send the public key to someone who does.
#
# The private key goes to the droplet only (mode 0600, used by root for the clone) and is deleted there
# after a verified clone unless keep_bw1_build_key = true. Delete the deploy key on GitHub when you no
# longer need it.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File E:\bw1-decomp\scripts\new-bw1-build-key.ps1
#   ... -Repo openblack/bw1-build -Out E:\bw1-decomp\keys\bw1-build-deploy

[CmdletBinding()]
param(
    [string]$Repo = "openblack/bw1-build",
    [string]$Out = (Join-Path (Split-Path $PSScriptRoot -Parent) "keys\bw1-build-deploy")
)

$ErrorActionPreference = "Stop"

if (Test-Path -LiteralPath $Out) {
    Write-Host "A key already exists at $Out; not overwriting it. Its public half:" -ForegroundColor Yellow
}
else {
    $sshKeygen = Get-Command ssh-keygen -ErrorAction SilentlyContinue
    if ($null -eq $sshKeygen) { throw "ssh-keygen not found (Windows: Settings > Optional features > OpenSSH Client)" }
    New-Item -ItemType Directory -Force -Path (Split-Path $Out -Parent) | Out-Null
    # Empty passphrase: the droplet uses it unattended. It is read-only and scoped to one repo.
    & ssh-keygen -q -t ed25519 -N '""' -C "bw1-sandbox droplet, read-only, $Repo" -f "$Out"
    if ($LASTEXITCODE -ne 0) { throw "ssh-keygen failed" }
    # Owner-only access to the private key.
    & icacls "$Out" /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
    Write-Host "Wrote $Out (private, keep it out of git) and $Out.pub" -ForegroundColor Green
}

Write-Host ""
Write-Host "Public key:"
Write-Host (Get-Content -Raw -LiteralPath "$Out.pub").Trim()
Write-Host ""
Write-Host "Add it as a deploy key here, with 'Allow write access' left UNCHECKED:"
Write-Host "  https://github.com/$Repo/settings/keys/new"
Write-Host "Title suggestion: bw1-sandbox droplet (read-only)"
