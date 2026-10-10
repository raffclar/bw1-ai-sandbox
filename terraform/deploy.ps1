# Switch the bw1-ai-sandbox droplet on: create (or update) the DigitalOcean droplet, upload the droplet
# wrapper, the settings and the secrets, and run the setup (users, Docker, worker firewall, the sandbox at
# its pinned commit, bw1-decomp + orig/ + host toolchain, worker image, publisher).
# No worker starts by itself: you spawn workers with the orchestrator, the portal or bw1-sandbox.
#
# The secrets are read at run time from a JSON tokens file into this process's environment only. They are
# passed to Terraform as DIGITALOCEAN_TOKEN and TF_VAR_* variables, never on the command line, and never
# printed or written anywhere by this script. The saved plan file (which Terraform fills with the variable
# values) is deleted when the script ends, whatever happens.
#
# Tokens file format (placeholders; scripts/new-github-credentials.ps1 explains each GitHub token):
#   {
#     "digitalocean":        "dop_v1_...",
#     "claude_oauth":        "sk-ant-oat01-...",   <- output of `claude setup-token` (your subscription)
#     "github":              "github_pat_...",     <- raffclar/bw1-decomp: push + PRs + comments
#     "github_openblack_pr": "github_pat_..."      <- openblack/bw1-decomp: PRs + comments only; pr_target = "openblack"
#   }
# An "anthropic" API key is not used: bw1-ai-sandbox runs its workers on the subscription token.
#
# Game files: orig_archive_path (default E:/bw1-original.zip) is uploaded to the droplet host, unpacked
# there and deleted; this script only checks that it exists and prints its size.
#
# Usage (PowerShell):
#   powershell -ExecutionPolicy Bypass -File E:\bw1-decomp\terraform\deploy.ps1
#   ... -TokensFile D:\other\tokens.json       # default E:\api_tokens.json
#   ... -ClaudeOAuthToken $o -GithubToken $t    # override "claude_oauth" / "github"
#   ... -OpenblackPrToken $p                    # override "github_openblack_pr"
#   ... -AllowSameTokenForOpenblack             # accept "github" as the openblack PR credential too (not recommended)
#   ... -AcceptUncommitted                      # don't ask when the local clone has uncommitted changes
#   ... -PlanOnly                               # every check, init and plan; stop before apply
#
# It refuses to go on unless the DigitalOcean token, the Claude subscription token and the GitHub token
# are present (plus the openblack PR credential when pr_target = "openblack"), and it asks for a typed
# confirmation before applying the plan it shows you.

[CmdletBinding()]
param(
    [string]$TokensFile = "E:\api_tokens.json",
    [string]$ClaudeOAuthToken = "",
    [string]$GithubToken = "",
    [string]$OpenblackPrToken = "",
    [switch]$AllowSameTokenForOpenblack,
    # Deploy even though the local clone has uncommitted changes (they are not uploaded).
    [switch]$AcceptUncommitted,
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

function Get-TfvarsString([string]$path, [string]$name, [string]$default) {
    $m = Select-String -LiteralPath $path -Pattern ('^\s*' + $name + '\s*=\s*"([^"]*)"') | Select-Object -Last 1
    if ($m) { return $m.Matches[0].Groups[1].Value }
    return $default
}

# --- settings file: must exist and must not hold secrets --------------------------------------
$varPath = if ([IO.Path]::IsPathRooted($VarFile)) { $VarFile } else { Join-Path $terraformDir $VarFile }
if (-not (Test-Path -LiteralPath $varPath)) {
    Fail "No $VarFile in $terraformDir. Copy terraform.tfvars.example to $VarFile and fill it in."
}
$secretLines = Select-String -LiteralPath $varPath -Pattern '^\s*(claude_oauth_token|anthropic_api_key|github_token|openblack_pr_token|digitalocean_token|do_token)\s*='
if ($secretLines) { Fail "$VarFile assigns a secret variable (line $($secretLines[0].LineNumber)). Remove it: secrets come only from the tokens file." }
$prTarget = Get-TfvarsString $varPath "pr_target" "fork"
if ($prTarget -notin @("fork", "openblack")) { Fail "pr_target must be ""fork"" or ""openblack"", not ""$prTarget""" }
$forkRepo = Get-TfvarsString $varPath "fork_repo" "raffclar/bw1-decomp"
$upstreamRepo = Get-TfvarsString $varPath "upstream_repo" "openblack/bw1-decomp"

# --- secrets ----------------------------------------------------------------------------------
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
if ([string]::IsNullOrWhiteSpace($OpenblackPrToken)) { $OpenblackPrToken = Get-TokenValue $tokens "github_openblack_pr" }
$hasApiKeyOnly = [string]::IsNullOrWhiteSpace($ClaudeOAuthToken) -and -not [string]::IsNullOrWhiteSpace((Get-TokenValue $tokens "anthropic"))

$missing = @()
if ([string]::IsNullOrWhiteSpace($doToken)) { $missing += "'digitalocean' (DigitalOcean API token)" }
if ([string]::IsNullOrWhiteSpace($ClaudeOAuthToken)) { $missing += "'claude_oauth' (output of ``claude setup-token``) or -ClaudeOAuthToken" }
if ([string]::IsNullOrWhiteSpace($GithubToken)) { $missing += "'github' (fork credential, see scripts\new-github-credentials.ps1) or -GithubToken" }
if ($prTarget -eq "openblack" -and [string]::IsNullOrWhiteSpace($OpenblackPrToken)) {
    if ($AllowSameTokenForOpenblack) { $OpenblackPrToken = $GithubToken }
    else { $missing += "'github_openblack_pr' (PR-only credential for $upstreamRepo, needed because pr_target = ""openblack"") or -OpenblackPrToken" }
}
if ($missing.Count -gt 0) {
    if ($hasApiKeyOnly) { Write-Host "An 'anthropic' API key is set, but bw1-ai-sandbox runs workers on the subscription token; it is not used." -ForegroundColor Yellow }
    Fail ("Refusing to deploy. Missing or empty: " + ($missing -join ", ") + ".")
}
if (-not [string]::IsNullOrWhiteSpace($OpenblackPrToken) -and $OpenblackPrToken -eq $GithubToken) {
    if (-not $AllowSameTokenForOpenblack) {
        Fail "'github_openblack_pr' is the same token as 'github'. Use a separate PR-only token (scripts\new-github-credentials.ps1), or pass -AllowSameTokenForOpenblack to accept it."
    }
    Write-Host "WARNING: one token for both remotes. If it is a classic token, it can also push to $upstreamRepo (you have write access there); only the publisher's guards stop that." -ForegroundColor Yellow
}
Write-Host "Claude auth: subscription token (claude_oauth -> the sandbox's token.env, CLAUDE_CODE_OAUTH_TOKEN)"

# --- game files ---------------------------------------------------------------------------------
$repoSource = Get-TfvarsString $varPath "repo_source" "local"
$origSource = Get-TfvarsString $varPath "orig_source" "local"
$gameVersion = Get-TfvarsString $varPath "game_version" "BW1W120"
$localRepo = Get-TfvarsString $varPath "local_repo_path" "E:/bw1-decomp/bw1-decomp"
if (-not [IO.Path]::IsPathRooted($localRepo)) { $localRepo = Join-Path $terraformDir $localRepo }
$uploadVersions = @($gameVersion)
$m = Select-String -LiteralPath $varPath -Pattern '^\s*orig_upload_versions\s*=\s*\[([^\]]*)\]' | Select-Object -Last 1
if ($m) {
    $listed = @([regex]::Matches($m.Matches[0].Groups[1].Value, '"([^"]+)"') | ForEach-Object { $_.Groups[1].Value })
    if ($listed.Count -gt 0) { $uploadVersions = $listed }
}
if ($repoSource -notin @("local", "upstream")) { Fail "repo_source must be ""local"" or ""upstream"", not ""$repoSource""" }
if ($origSource -notin @("local", "archive", "manual")) { Fail "orig_source must be ""local"", ""archive"" or ""manual"", not ""$origSource""" }

$archivePath = Get-TfvarsString $varPath "orig_archive_path" "E:/bw1-original.zip"
if (-not [IO.Path]::IsPathRooted($archivePath)) { $archivePath = Join-Path $terraformDir $archivePath }
$origOverride = ""
if ($origSource -eq "local") {
    # The local orig/ wins when it's present; otherwise fall back to the archive.
    $missingOrig = @($uploadVersions | Where-Object { -not (Test-Path -LiteralPath (Join-Path $localRepo "orig\$_\runblack-decrypted.exe")) })
    if (-not (Test-Path -LiteralPath (Join-Path $localRepo "orig\libs"))) { $missingOrig += "libs" }
    if ($missingOrig.Count -gt 0) {
        if (Test-Path -LiteralPath $archivePath -PathType Leaf) {
            Write-Host "Game files: $localRepo\orig lacks $($missingOrig -join ', '); using the archive $archivePath instead" -ForegroundColor Yellow
            $origSource = "archive"
            $origOverride = "archive"
        }
        else { Fail "Refusing to deploy: $localRepo\orig lacks $($missingOrig -join ', ') and there is no archive at $archivePath." }
    }
    else { Write-Host "Game files: $localRepo\orig ($($uploadVersions -join ', ') + libs + directx7.0), packed for upload" }
}
if ($origSource -eq "archive") {
    if ($archivePath -notmatch '(?i)\.(zip|tar\.gz|tgz)$') { Fail "orig_archive_path must be a .zip or .tar.gz: $archivePath" }
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
        Fail "Refusing to deploy: the game-files archive $archivePath does not exist (orig_source = ""archive"")."
    }
    # Size only: the archive is never opened or listed here.
    $sizeMb = [math]::Round((Get-Item -LiteralPath $archivePath).Length / 1MB, 1)
    Write-Host "Game files: archive $archivePath ($sizeMb MB), unpacked on the droplet host, then deleted there"
}
elseif ($origSource -eq "manual") {
    Write-Host "Game files: manual (copy orig/ to the droplet yourself, then re-run the setup)"
}

# --- the local clone: show what will be uploaded --------------------------------------------------
if ($repoSource -eq "local" -or $origSource -eq "local") {
    if (-not (Test-Path -LiteralPath (Join-Path $localRepo ".git"))) { Fail "local_repo_path $localRepo is not a git clone" }
}
if ($repoSource -eq "local") {
    $saved = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $branch = (& git -C $localRepo rev-parse --abbrev-ref HEAD 2>$null)
    $head = (& git -C $localRepo log -1 --format="%h %s" 2>$null)
    $dirty = @(& git -C $localRepo status --porcelain 2>$null)
    $ErrorActionPreference = $saved
    Write-Host "Project checkout: $localRepo on $branch @ $head; uploaded as a git bundle of all refs (committed work only)"
    if ($dirty.Count -gt 0) {
        Write-Host "WARNING: $($dirty.Count) uncommitted change(s) in $localRepo will NOT be uploaded (workers start from the committed HEAD):" -ForegroundColor Yellow
        $dirty | Select-Object -First 10 | ForEach-Object { Write-Host "    $_" }
        if (-not $AcceptUncommitted) {
            $answer = Read-Host "Type 'continue' to deploy without them (or commit first)"
            if ($answer -ne "continue") { Write-Host "Cancelled. Nothing was changed."; exit 0 }
        }
    }
}
else {
    Write-Host "Project checkout: fresh clone of $upstreamRepo on the droplet"
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
# Packed uploads (git bundle, orig/ subset) live only in this temporary folder, deleted in `finally`.
$packDir = Join-Path ([IO.Path]::GetTempPath()) ("bw1-sandbox-upload-" + [guid]::NewGuid().ToString("N"))
try {
    if ($repoSource -eq "local" -or $origSource -eq "local") {
        Write-Host "== packing the local clone ==" -ForegroundColor Cyan
        $packArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path (Split-Path $terraformDir -Parent) "scripts\pack-local.ps1"),
            "-OutDir", $packDir, "-RepoPath", $localRepo, "-Versions", ($uploadVersions -join ","))
        if ($repoSource -ne "local") { $packArgs += "-SkipRepo" }
        if ($origSource -ne "local") { $packArgs += "-SkipOrig" }
        & powershell @packArgs
        if ($LASTEXITCODE -ne 0) { Fail "packing $localRepo failed" }
    }
    # Non-secret run-time settings go on the command line, so they win over terraform.tfvars.
    $runVars = @()
    if ($repoSource -eq "local") { $runVars += "-var=local_bundle_path=" + ((Join-Path $packDir "repo.bundle") -replace '\\', '/') }
    if ($origSource -eq "local") { $runVars += "-var=orig_upload_path=" + ((Join-Path $packDir "orig-local.tar.gz") -replace '\\', '/') }
    if ($origOverride) { $runVars += "-var=orig_source=$origOverride" }

    $env:DIGITALOCEAN_TOKEN = $doToken
    $env:TF_VAR_claude_oauth_token = $ClaudeOAuthToken
    $env:TF_VAR_github_token = $GithubToken
    $env:TF_VAR_openblack_pr_token = $OpenblackPrToken

    Write-Host "== terraform init ==" -ForegroundColor Cyan
    & terraform init -input=false
    if ($LASTEXITCODE -ne 0) { Fail "terraform init failed" }

    Write-Host "`n== terraform plan ==" -ForegroundColor Cyan
    & terraform plan -input=false -var-file="$varPath" @runVars -out="$planFile"
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
    $prRepo = if ($vars.pr_target.value -eq "openblack") { $vars.upstream_repo.value } else { $vars.fork_repo.value }
    Write-Host ""
    Write-Host ("  droplet      {0} in {1}" -f $vars.droplet_size.value, $vars.region.value)
    Write-Host ("  DO project   {0} (existing; the droplet is assigned to it, the project is never created or deleted)" -f $vars.do_project_id.value)
    Write-Host ("  sandbox      {0} @ {1}" -f $vars.sandbox_repo.value, $vars.sandbox_ref.value)
    $checkout = if ($vars.repo_source.value -eq "local") { "your clone $localRepo at $branch @ $head (git bundle, all refs)" } else { "fresh clone of $($vars.upstream_repo.value) @ $($vars.bw1_decomp_ref.value)" }
    Write-Host ("  checkout     {0}" -f $checkout)
    $gameFiles = if ($vars.orig_source.value -eq "local") { ($uploadVersions -join ", ") + " + libs + directx7.0 from your clone" } else { $vars.game_version.value }
    Write-Host ("  game files   {0} ({1})" -f $vars.orig_source.value, $gameFiles)
    Write-Host ("  workers      {0} CPUs, {1} each; base = the checkout's HEAD (sync)" -f $vars.worker_cpus.value, $vars.worker_memory.value)
    Write-Host "  remotes"
    Write-Host ("    {0,-24} push {1}* branches (create only), PRs + comments when it is the PR target   ['github']" -f $vars.fork_repo.value, $vars.branch_prefix.value)
    Write-Host ("    {0,-24} fetch only; {1}   ['github_openblack_pr']" -f $vars.upstream_repo.value, $(if ($vars.pr_target.value -eq "openblack") { "draft PRs from $($vars.fork_repo.value) branches + comments on them, never a push" } else { "no PRs (pr_target = fork)" }))
    Write-Host ("  publisher    {0}: draft PRs on {1}, {2}/day, {3} open max; comments checked; feedback to workers {4}" -f $(if ($vars.publisher_enabled.value) { "ENABLED" } else { "off" }), $prRepo, $vars.max_prs_per_day.value, $vars.max_open_prs.value, $vars.feedback_to_sandbox.value)
    Write-Host ("  services     autoresume {0}, portal {1} (localhost only), orchestrator tools {2}" -f $vars.autoresume_enabled.value, $vars.portal_enabled.value, $vars.orchestrator_tools.value)

    if ($PlanOnly) {
        Write-Host "`n-PlanOnly: stopping before apply. Nothing was changed." -ForegroundColor Yellow
        exit 0
    }

    Write-Host ""
    Write-Host "Applying creates a billed droplet holding your Claude subscription token and GitHub token(s)." -ForegroundColor Yellow
    Write-Host "Workers you spawn there use your subscription's limits; the publisher opens draft PRs on $prRepo." -ForegroundColor Yellow
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
    # The packed clone and game files never stay on this PC.
    if (Test-Path -LiteralPath $packDir) { Remove-Item -LiteralPath $packDir -Recurse -Force -ErrorAction SilentlyContinue }
    Pop-Location
    Remove-Item Env:DIGITALOCEAN_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:TF_VAR_claude_oauth_token -ErrorAction SilentlyContinue
    Remove-Item Env:TF_VAR_github_token -ErrorAction SilentlyContinue
    Remove-Item Env:TF_VAR_openblack_pr_token -ErrorAction SilentlyContinue
}
