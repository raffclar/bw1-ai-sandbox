# Switch the bw1-ai-sandbox droplet on: create (or update) the DigitalOcean droplet, upload the droplet
# wrapper, the settings and the two secrets, and run the setup (users, Docker, worker firewall, the
# sandbox at its pinned commit, bw1-decomp + orig/ + host toolchain, worker image, publisher).
# No worker starts by itself: you spawn workers with the orchestrator, the portal or bw1-sandbox.
#
# The secrets are read at run time from a JSON tokens file into this process's environment only.
# They are passed to Terraform as DIGITALOCEAN_TOKEN, TF_VAR_claude_oauth_token and
# TF_VAR_github_token, never on the command line, and never printed or written anywhere by this
# script. The saved plan file (which Terraform fills with the variable values) is deleted when the
# script ends, whatever happens.
#
# Tokens file format (placeholders):
#   {
#     "digitalocean": "dop_v1_...",
#     "claude_oauth": "sk-ant-oat01-...",   <- output of `claude setup-token` (your subscription)
#     "github":       "github_pat_...",     <- publisher: Contents + Pull requests read/write on your fork
#     "github_bw1_build": "github_pat_..."  <- optional, option (b) below
#   }
#
# Game files: by default (orig_source = "archive") orig_archive_path, E:/bw1-original.zip, is uploaded
# to the droplet host, unpacked there and deleted; the script only checks it exists and prints its size.
# With orig_source = "git" the droplet clones the private openblack/bw1-build repo for
# the game files, which needs ONE of:
#   (a) a read-only deploy key: scripts/new-bw1-build-key.ps1 writes it to keys/bw1-build-deploy
#       (gitignored) and tells you where to add the public half on GitHub (recommended);
#   (b) "github_bw1_build": a token with Contents: read on openblack/bw1-build only.
# It is kept apart from "github" and never reaches the workers.
# An "anthropic" API key is not used: bw1-ai-sandbox runs its workers on the subscription token
# (`sandbox.py token`), so the script refuses to deploy with an API key alone.
#
# Usage (PowerShell):
#   powershell -ExecutionPolicy Bypass -File E:\bw1-decomp\terraform\deploy.ps1
#   ... -TokensFile D:\other\tokens.json       # default E:\api_tokens.json
#   ... -ClaudeOAuthToken $o -GithubToken $t    # override "claude_oauth" / "github"
#   ... -Bw1BuildToken $b                       # override "github_bw1_build"
#   ... -PlanOnly                               # every check, init and plan; stop before apply
#
# It refuses to go on unless the DigitalOcean token, the Claude subscription token and the GitHub
# token are all present, and it asks for a typed confirmation before applying the plan it shows you.

[CmdletBinding()]
param(
    [string]$TokensFile = "E:\api_tokens.json",
    [string]$ClaudeOAuthToken = "",
    [string]$GithubToken = "",
    [string]$Bw1BuildToken = "",
    [string]$VarFile = "terraform.tfvars",
    [switch]$PlanOnly
)

$ErrorActionPreference = "Stop"
$terraformDir = $PSScriptRoot
$minTerraform = [version]"1.9.0"
$planFile = Join-Path $terraformDir ".deploy.tfplan"

function Fail([string]$message) {
    Write-Host "ERROR: $message" -ForegroundColor Red
    exit 1
}

function Get-TokenValue($tokens, [string]$name) {
    if ($null -eq $tokens) { return "" }
    $property = $tokens.PSObject.Properties[$name]
    if ($null -eq $property -or $null -eq $property.Value) { return "" }
    return [string]$property.Value
}

# --- secrets: DigitalOcean + Claude subscription + GitHub, or nothing -----------------------------
$tokens = $null
if (Test-Path -LiteralPath $TokensFile) {
    try { $tokens = Get-Content -Raw -LiteralPath $TokensFile | ConvertFrom-Json }
    catch { Fail "Could not parse $TokensFile as JSON" }
}
else {
    Fail "Tokens file not found: $TokensFile (it must hold at least the 'digitalocean' token)"
}

$doToken = Get-TokenValue $tokens "digitalocean"
if ([string]::IsNullOrWhiteSpace($ClaudeOAuthToken)) { $ClaudeOAuthToken = Get-TokenValue $tokens "claude_oauth" }
if ([string]::IsNullOrWhiteSpace($GithubToken)) { $GithubToken = Get-TokenValue $tokens "github" }
$hasApiKeyOnly = [string]::IsNullOrWhiteSpace($ClaudeOAuthToken) -and -not [string]::IsNullOrWhiteSpace((Get-TokenValue $tokens "anthropic"))

$missing = @()
if ([string]::IsNullOrWhiteSpace($doToken)) { $missing += "'digitalocean' (DigitalOcean API token)" }
if ([string]::IsNullOrWhiteSpace($ClaudeOAuthToken)) { $missing += "'claude_oauth' (output of ``claude setup-token``) or -ClaudeOAuthToken" }
if ([string]::IsNullOrWhiteSpace($GithubToken)) { $missing += "'github' (GitHub token) or -GithubToken" }
if ($missing.Count -gt 0) {
    if ($hasApiKeyOnly) { Write-Host "An 'anthropic' API key is set, but bw1-ai-sandbox runs workers on the subscription token; it is not used." -ForegroundColor Yellow }
    Fail ("Refusing to deploy. Missing or empty: " + ($missing -join ", ") + ". The sandbox is only switched on once all three are provided.")
}
Write-Host "Claude auth: subscription token (claude_oauth -> the sandbox's token.env, CLAUDE_CODE_OAUTH_TOKEN)"
if ([string]::IsNullOrWhiteSpace($Bw1BuildToken)) { $Bw1BuildToken = Get-TokenValue $tokens "github_bw1_build" }

# --- settings file: must exist and must not hold secrets --------------------------------------
$varPath = if ([IO.Path]::IsPathRooted($VarFile)) { $VarFile } else { Join-Path $terraformDir $VarFile }
if (-not (Test-Path -LiteralPath $varPath)) {
    Fail "No $VarFile in $terraformDir. Copy terraform.tfvars.example to $VarFile and fill it in."
}
$secretLines = Select-String -LiteralPath $varPath -Pattern '^\s*(claude_oauth_token|anthropic_api_key|github_token|bw1_build_token|digitalocean_token|do_token)\s*='
if ($secretLines) { Fail "$VarFile assigns a secret variable (line $($secretLines[0].LineNumber)). Remove it: secrets come only from the tokens file." }

# --- game files: orig_source = "git" needs a bw1-build credential --------------------------------
function Get-TfvarsString([string]$path, [string]$name, [string]$default) {
    $m = Select-String -LiteralPath $path -Pattern ('^\s*' + $name + '\s*=\s*"([^"]*)"') | Select-Object -Last 1
    if ($m) { return $m.Matches[0].Groups[1].Value }
    return $default
}
$origSource = Get-TfvarsString $varPath "orig_source" "archive"
$keyPath = Get-TfvarsString $varPath "bw1_build_key_path" ""
if ([string]::IsNullOrWhiteSpace($keyPath)) { $keyPath = Join-Path (Split-Path $terraformDir -Parent) "keys\bw1-build-deploy" }
elseif (-not [IO.Path]::IsPathRooted($keyPath)) { $keyPath = Join-Path $terraformDir $keyPath }
if ($origSource -eq "git") {
    $hasKey = Test-Path -LiteralPath $keyPath
    $hasToken = -not [string]::IsNullOrWhiteSpace($Bw1BuildToken)
    if (-not $hasKey -and -not $hasToken) {
        Fail ("Refusing to deploy: orig_source = ""git"" needs a credential for the private openblack/bw1-build repo. " +
            "(a) recommended: run scripts\new-bw1-build-key.ps1 and add the printed key on GitHub as a READ-ONLY deploy key " +
            "(expected at $keyPath); or (b) put a token with Contents: read on openblack/bw1-build under ""github_bw1_build"" in the tokens file.")
    }
    if ($hasToken -and $Bw1BuildToken -eq $GithubToken) {
        Fail "'github_bw1_build' is the same token as 'github'. Keep them separate: the bw1-build credential must not be the publisher's token."
    }
    if ($hasKey) {
        Write-Host "Game files: git clone of openblack/bw1-build with the deploy key $keyPath"
        $Bw1BuildToken = ""
    }
    else {
        Write-Host "Game files: git clone of openblack/bw1-build with the 'github_bw1_build' token"
    }
}
elseif ($origSource -eq "archive") {
    $Bw1BuildToken = ""
    $archivePath = Get-TfvarsString $varPath "orig_archive_path" "E:/bw1-original.zip"
    if (-not [IO.Path]::IsPathRooted($archivePath)) { $archivePath = Join-Path $terraformDir $archivePath }
    if ($archivePath -notmatch '(?i)\.(zip|tar\.gz|tgz)$') { Fail "orig_archive_path must be a .zip or .tar.gz: $archivePath" }
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
        Fail "Refusing to deploy: the game-files archive $archivePath does not exist (orig_source = ""archive"")."
    }
    # Size only: the archive is never opened or listed here.
    $sizeMb = [math]::Round((Get-Item -LiteralPath $archivePath).Length / 1MB, 1)
    Write-Host "Game files: archive $archivePath ($sizeMb MB), unpacked on the droplet host, then deleted there"
}
else {
    $Bw1BuildToken = ""
    Write-Host "Game files: $origSource (copy orig/ to the droplet yourself)"
}

# --- terraform ------------------------------------------------------------------------------
$terraform = Get-Command terraform -ErrorAction SilentlyContinue
if ($null -eq $terraform) { Fail "terraform is not on PATH (https://developer.hashicorp.com/terraform/install)" }
$localVersion = [version]((& terraform version -json | ConvertFrom-Json).terraform_version)
if ($localVersion -lt $minTerraform) { Fail "Terraform $localVersion is too old; $minTerraform or newer is required." }
$stateFile = Join-Path $terraformDir "terraform.tfstate"
if (Test-Path $stateFile) {
    $stateVersion = [version](Get-Content -Raw $stateFile | ConvertFrom-Json).terraform_version
    if ($localVersion -lt $stateVersion) { Fail "The state was written by Terraform $stateVersion but this terraform is $localVersion. Install $stateVersion or newer first." }
}

Push-Location $terraformDir
try {
    $env:DIGITALOCEAN_TOKEN = $doToken
    $env:TF_VAR_claude_oauth_token = $ClaudeOAuthToken
    $env:TF_VAR_github_token = $GithubToken
    $env:TF_VAR_bw1_build_token = $Bw1BuildToken

    Write-Host "== terraform init ==" -ForegroundColor Cyan
    & terraform init -input=false
    if ($LASTEXITCODE -ne 0) { Fail "terraform init failed" }

    Write-Host "`n== terraform plan ==" -ForegroundColor Cyan
    & terraform plan -input=false -var-file="$varPath" -out="$planFile"
    if ($LASTEXITCODE -ne 0) { Fail "terraform plan failed (nothing was changed)" }

    # Summary from the saved plan: only resource addresses, actions and non-secret settings are printed.
    $plan = & terraform show -json "$planFile" | ConvertFrom-Json
    $changes = @($plan.resource_changes | Where-Object { -not ($_.change.actions.Count -eq 1 -and $_.change.actions[0] -eq "no-op") })
    Write-Host "`n== Summary ==" -ForegroundColor Cyan
    if ($changes.Count -eq 0) {
        Write-Host "No changes. Nothing to apply."
        exit 0
    }
    foreach ($c in $changes) { Write-Host ("  {0,-22} {1}" -f ($c.change.actions -join "+"), $c.address) }
    $vars = $plan.variables
    Write-Host ""
    Write-Host ("  droplet      {0} in {1}" -f $vars.droplet_size.value, $vars.region.value)
    Write-Host ("  DO project   {0} (existing; the droplet is assigned to it, the project is never created or deleted)" -f $vars.do_project_id.value)
    Write-Host ("  sandbox      {0} @ {1}" -f $vars.sandbox_repo.value, $vars.sandbox_ref.value)
    Write-Host ("  game files   {0} ({1}); keep bw1-build credential after clone: {2}" -f $vars.orig_source.value, $vars.game_version.value, $vars.keep_bw1_build_key.value)
    Write-Host ("  workers      {0} CPUs, {1} each; base {2} @ {3}" -f $vars.worker_cpus.value, $vars.worker_memory.value, $vars.upstream_repo.value, $vars.bw1_decomp_ref.value)
    Write-Host ("  publisher    {0}: draft PRs on {1} only, branches {2}*, {3}/day, {4} open max" -f $(if ($vars.publisher_enabled.value) { "ENABLED" } else { "off" }), $vars.fork_repo.value, $vars.branch_prefix.value, $vars.max_prs_per_day.value, $vars.max_open_prs.value)
    Write-Host ("  services     autoresume {0}, portal {1} (localhost only), orchestrator tools {2}" -f $vars.autoresume_enabled.value, $vars.portal_enabled.value, $vars.orchestrator_tools.value)

    if ($PlanOnly) {
        Write-Host "`n-PlanOnly: stopping before apply. Nothing was changed." -ForegroundColor Yellow
        exit 0
    }

    Write-Host ""
    Write-Host "Applying creates a billed droplet holding your Claude subscription token and GitHub token." -ForegroundColor Yellow
    Write-Host "Workers you spawn there use your subscription's limits; the publisher opens draft PRs on your fork." -ForegroundColor Yellow
    $answer = Read-Host "Type 'deploy' to apply this plan"
    if ($answer -ne "deploy") {
        Write-Host "Cancelled. Nothing was changed."
        exit 0
    }

    Write-Host "`n== terraform apply ==" -ForegroundColor Cyan
    & terraform apply -input=false "$planFile"
    if ($LASTEXITCODE -ne 0) { Fail "terraform apply failed; fix the error and run the script again" }

    $ip = (& terraform output -raw droplet_ip)
    Write-Host "`n== Deployed ==" -ForegroundColor Green
    Write-Host "Droplet: $ip"
    Write-Host ""
    Write-Host "Check it:"
    Write-Host "  ssh root@$ip tail -n 40 /var/log/bw1-sandbox-setup.log"
    Write-Host "  ssh root@$ip sudo -u sandbox -H bw1-sandbox check      # workers reach the Anthropic API and nothing else"
    Write-Host "  ssh root@$ip sudo -u sandbox -H bw1-sandbox status"
    Write-Host "  ssh root@$ip tail -n 50 /var/log/bw1-publisher/publisher.log"
    Write-Host ""
    Write-Host "Portal:    ssh -N -L 8765:127.0.0.1:8765 root@$ip   (with portal_enabled = true)"
    Write-Host "Stop it:   terraform output stop_command"
    Write-Host "Remove it: powershell -ExecutionPolicy Bypass -File $terraformDir\teardown.ps1"
}
finally {
    Remove-Item -LiteralPath $planFile -ErrorAction SilentlyContinue
    Pop-Location
    Remove-Item Env:DIGITALOCEAN_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:TF_VAR_claude_oauth_token -ErrorAction SilentlyContinue
    Remove-Item Env:TF_VAR_github_token -ErrorAction SilentlyContinue
    Remove-Item Env:TF_VAR_bw1_build_token -ErrorAction SilentlyContinue
}
