# Switch the bw1-decomp harness on: create (or update) the DigitalOcean droplet, upload the harness,
# the settings and the keys, and enable the timer.
#
# The three secrets are read at run time from a JSON tokens file into this process's environment
# only. They are passed to Terraform as DIGITALOCEAN_TOKEN, TF_VAR_anthropic_api_key and
# TF_VAR_github_token, never on the command line, and never printed or written anywhere by this
# script. The saved plan file (which Terraform fills with the variable values) is deleted when
# the script ends, whatever happens.
#
# Tokens file format (placeholders):
#   {
#     "digitalocean": "dop_v1_...",
#     "anthropic":    "sk-ant-...",
#     "github":       "github_pat_..."
#   }
#
# Usage (PowerShell):
#   powershell -ExecutionPolicy Bypass -File E:\bw1-decomp\terraform\deploy.ps1
#   ... -TokensFile D:\other\tokens.json       # default E:\api_tokens.json
#   ... -AnthropicKey $k -GithubToken $t        # override the file's "anthropic" / "github" keys
#
# It refuses to go on unless all three secrets are present, and it asks for a typed confirmation
# before applying the plan it shows you.

[CmdletBinding()]
param(
    [string]$TokensFile = "E:\api_tokens.json",
    [string]$AnthropicKey = "",
    [string]$GithubToken = "",
    [string]$VarFile = "terraform.tfvars",
    # Run every check, init and plan, then stop before apply.
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

# --- secrets: all three or nothing ----------------------------------------------------------
$tokens = $null
if (Test-Path -LiteralPath $TokensFile) {
    try { $tokens = Get-Content -Raw -LiteralPath $TokensFile | ConvertFrom-Json }
    catch { Fail "Could not parse $TokensFile as JSON" }
}
elseif ([string]::IsNullOrWhiteSpace($AnthropicKey) -or [string]::IsNullOrWhiteSpace($GithubToken)) {
    Fail "Tokens file not found: $TokensFile"
}

$doToken = Get-TokenValue $tokens "digitalocean"
if ([string]::IsNullOrWhiteSpace($AnthropicKey)) { $AnthropicKey = Get-TokenValue $tokens "anthropic" }
if ([string]::IsNullOrWhiteSpace($GithubToken)) { $GithubToken = Get-TokenValue $tokens "github" }

$missing = @()
if ([string]::IsNullOrWhiteSpace($doToken)) { $missing += "'digitalocean' (DigitalOcean API token)" }
if ([string]::IsNullOrWhiteSpace($AnthropicKey)) { $missing += "'anthropic' (Anthropic API key) or -AnthropicKey" }
if ([string]::IsNullOrWhiteSpace($GithubToken)) { $missing += "'github' (GitHub token) or -GithubToken" }
if ($missing.Count -gt 0) {
    Fail ("Refusing to deploy. Missing or empty: " + ($missing -join ", ") + ". The harness is only switched on once all three are provided.")
}

# --- settings file: must exist and must not hold secrets --------------------------------------
$varPath = if ([IO.Path]::IsPathRooted($VarFile)) { $VarFile } else { Join-Path $terraformDir $VarFile }
if (-not (Test-Path -LiteralPath $varPath)) {
    Fail "No $VarFile in $terraformDir. Copy terraform.tfvars.example to $VarFile and fill it in."
}
$secretLines = Select-String -LiteralPath $varPath -Pattern '^\s*(anthropic_api_key|github_token|digitalocean_token|do_token)\s*='
if ($secretLines) { Fail "$VarFile assigns a secret variable (line $($secretLines[0].LineNumber)). Remove it: secrets come only from the tokens file." }

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
    $env:TF_VAR_anthropic_api_key = $AnthropicKey
    $env:TF_VAR_github_token = $GithubToken

    Write-Host "== terraform init ==" -ForegroundColor Cyan
    & terraform init -input=false
    if ($LASTEXITCODE -ne 0) { Fail "terraform init failed" }

    Write-Host "`n== terraform plan ==" -ForegroundColor Cyan
    & terraform plan -input=false -var-file="$varPath" -out="$planFile"
    if ($LASTEXITCODE -ne 0) { Fail "terraform plan failed (nothing was changed)" }

    # Summary from the saved plan: only resource addresses and actions are printed.
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
    Write-Host ("  fork / PRs   {0} -> {1}" -f $vars.fork_repo.value, $(if ($vars.pr_repo.value) { $vars.pr_repo.value } else { $vars.fork_repo.value }))
    Write-Host ("  model        {0}, max `${1}/run, `${2}/day, {3} PRs/day, {4} open PRs" -f $vars.claude_model.value, $vars.max_usd_per_run.value, $vars.max_usd_per_day.value, $vars.max_prs_per_day.value, $vars.max_open_prs.value)
    Write-Host ("  timer        {0} (every {1})" -f $(if ($vars.harness_enabled.value) { "ENABLED" } else { "installed disabled" }), $vars.run_interval.value)

    if ($PlanOnly) {
        Write-Host "`n-PlanOnly: stopping before apply. Nothing was changed." -ForegroundColor Yellow
        exit 0
    }

    Write-Host ""
    Write-Host "Applying creates a billed droplet and, with the timer enabled, starts an AI agent that" -ForegroundColor Yellow
    Write-Host "spends Anthropic credit and opens pull requests with your GitHub token." -ForegroundColor Yellow
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
    Write-Host "Check the harness:"
    Write-Host "  ssh root@$ip systemctl list-timers bw1-harness.timer"
    Write-Host "  ssh root@$ip systemctl status bw1-harness.service --no-pager"
    Write-Host "  ssh root@$ip tail -n 50 /var/log/bw1-harness/harness.log"
    Write-Host "  ssh root@$ip tail -n 40 /var/log/bw1-harness-setup.log"
    Write-Host ""
    Write-Host "Stop it:   ssh root@$ip 'touch /etc/bw1-harness/STOP && systemctl disable --now bw1-harness.timer'"
    Write-Host "Remove it: powershell -ExecutionPolicy Bypass -File $terraformDir\teardown.ps1"
}
finally {
    Remove-Item -LiteralPath $planFile -ErrorAction SilentlyContinue
    Pop-Location
    Remove-Item Env:DIGITALOCEAN_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:TF_VAR_anthropic_api_key -ErrorAction SilentlyContinue
    Remove-Item Env:TF_VAR_github_token -ErrorAction SilentlyContinue
}
