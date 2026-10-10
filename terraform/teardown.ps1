# Tear down everything this Terraform config created for the bw1-decomp harness: the droplet (with
# its checkout, build, logs and keys), its firewall and its SSH key. It goes through
# `terraform destroy` using the existing state. Branches and PRs already on GitHub are left alone.
#
# Only the DigitalOcean token is needed. It is read from the same tokens file as deploy.ps1
# ({"digitalocean": "..."}) into this process's environment only, and never printed or written.
#
# Usage (PowerShell):
#   powershell -ExecutionPolicy Bypass -File E:\bw1-decomp\terraform\teardown.ps1
#   ... -TokensFile D:\other\tokens.json      # default E:\api_tokens.json
#   ... -AutoApprove                          # skip the typed confirmation

[CmdletBinding()]
param(
    [string]$TokensFile = "E:\api_tokens.json",
    [string]$VarFile = "terraform.tfvars",
    [switch]$AutoApprove
)

$ErrorActionPreference = "Stop"
$terraformDir = $PSScriptRoot
$minTerraform = [version]"1.9.0"

function Fail([string]$message) {
    Write-Host "ERROR: $message" -ForegroundColor Red
    exit 1
}

# --- token ----------------------------------------------------------------------------------
if (-not (Test-Path -LiteralPath $TokensFile)) { Fail "Tokens file not found: $TokensFile" }
try { $tokens = Get-Content -Raw -LiteralPath $TokensFile | ConvertFrom-Json }
catch { Fail "Could not parse $TokensFile as JSON" }
$property = $tokens.PSObject.Properties["digitalocean"]
$doToken = if ($null -ne $property) { [string]$property.Value } else { "" }
if ([string]::IsNullOrWhiteSpace($doToken)) { Fail "No 'digitalocean' key in $TokensFile" }

# --- terraform ------------------------------------------------------------------------------
$terraform = Get-Command terraform -ErrorAction SilentlyContinue
if ($null -eq $terraform) { Fail "terraform is not on PATH" }
$localVersion = [version]((& terraform version -json | ConvertFrom-Json).terraform_version)
if ($localVersion -lt $minTerraform) { Fail "Terraform $localVersion is too old; $minTerraform or newer is required." }
$stateFile = Join-Path $terraformDir "terraform.tfstate"
if (-not (Test-Path $stateFile)) { Fail "No terraform.tfstate in ${terraformDir}: nothing to destroy, or the state lives elsewhere (see README, Remote state)" }
$stateVersion = [version](Get-Content -Raw $stateFile | ConvertFrom-Json).terraform_version
if ($localVersion -lt $stateVersion) {
    Fail "The state was written by Terraform $stateVersion but this terraform is $localVersion. Install $stateVersion or newer first."
}
$varArgs = @()
$varPath = if ([IO.Path]::IsPathRooted($VarFile)) { $VarFile } else { Join-Path $terraformDir $VarFile }
if (Test-Path -LiteralPath $varPath) { $varArgs = @("-var-file=$varPath") }

Push-Location $terraformDir
$planFile = Join-Path $terraformDir ".destroy.tfplan"
try {
    $env:DIGITALOCEAN_TOKEN = $doToken

    Write-Host "== Resources in the state ==" -ForegroundColor Cyan
    & terraform state list
    if ($LASTEXITCODE -ne 0) { Fail "terraform state list failed" }

    Write-Host "`n== terraform init ==" -ForegroundColor Cyan
    & terraform init -input=false
    if ($LASTEXITCODE -ne 0) { Fail "terraform init failed" }

    Write-Host "`n== Destroy plan ==" -ForegroundColor Cyan
    & terraform plan -destroy -input=false @varArgs -out="$planFile"
    if ($LASTEXITCODE -ne 0) { Fail "terraform plan -destroy failed" }

    if (-not $AutoApprove) {
        Write-Host ""
        Write-Host "This permanently deletes the harness droplet (checkout, build, logs, keys), its firewall" -ForegroundColor Yellow
        Write-Host "and its SSH key. Pushed branches and open PRs on GitHub are not touched." -ForegroundColor Yellow
        $answer = Read-Host "Type 'destroy' to continue"
        if ($answer -ne "destroy") {
            Write-Host "Cancelled. Nothing was changed."
            exit 0
        }
    }

    Write-Host "`n== terraform apply (destroy) ==" -ForegroundColor Cyan
    & terraform apply -input=false "$planFile"
    if ($LASTEXITCODE -ne 0) { Fail "terraform apply failed; run the script again to retry what's left" }

    Write-Host "`n== Left in the state (should be empty) ==" -ForegroundColor Cyan
    & terraform state list

    Write-Host "`n== DigitalOcean check (tag 'bw1-harness') ==" -ForegroundColor Cyan
    $headers = @{ Authorization = "Bearer $doToken" }
    $droplets = (Invoke-RestMethod -Headers $headers -Uri "https://api.digitalocean.com/v2/droplets?tag_name=bw1-harness").droplets
    Write-Host ("Droplets tagged bw1-harness: {0}" -f @($droplets).Count)
    Write-Host "`nTeardown finished." -ForegroundColor Green
}
finally {
    Remove-Item -LiteralPath $planFile -ErrorAction SilentlyContinue
    Pop-Location
    Remove-Item Env:DIGITALOCEAN_TOKEN -ErrorAction SilentlyContinue
}
