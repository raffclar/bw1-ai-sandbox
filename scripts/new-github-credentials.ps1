# Explain, for each GitHub remote the publisher uses, exactly which credential to create, on which page,
# with which permissions, and where it goes. Nothing secret is generated or read here: GitHub tokens can
# only be created on github.com. With -OutFile it writes a placeholder template of the tokens file
# (no secrets); -WhatIf shows what it would write.
#
#   raffclar/bw1-decomp   push ai-sandbox/* branches, PRs and comments there     tokens key "github"
#   openblack/bw1-decomp  PRs (head raffclar:<branch>) and comments only, never   tokens key "github_openblack_pr"
#                         a push; needed only with pr_target = "openblack"
#
# A fine-grained token is preferred for both: it is limited to one repository and to the permissions
# below. On openblack it has NO Contents write, so it cannot push, merge or delete branches there even
# though your account has write access. GitHub docs:
#   https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens
#   https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File E:\bw1-decomp\scripts\new-github-credentials.ps1
#   ... -Fork raffclar/bw1-decomp -Upstream openblack/bw1-decomp
#   ... -OutFile C:\somewhere\tokens.template.json [-WhatIf]

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Fork = "raffclar/bw1-decomp",
    [string]$Upstream = "openblack/bw1-decomp",
    [string]$OutFile = ""
)

$ErrorActionPreference = "Stop"
if ($Fork -notmatch '^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$' -or $Upstream -notmatch '^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$') {
    throw "-Fork and -Upstream must be owner/name"
}
if ($Fork.ToLower().StartsWith("openblack/")) { throw "-Fork must be your own fork, never openblack/*" }
$forkOwner, $forkName = $Fork.Split("/")
$upOwner, $upName = $Upstream.Split("/")
$newToken = "https://github.com/settings/personal-access-tokens/new"

# Exactly the permissions the publisher needs (README "Remotes and credentials"). Everything not listed: No access.
$forkPerms = @(
    @("Metadata", "Read-only", "mandatory; repository lookups"),
    @("Contents", "Read and write", "push new ai-sandbox/* branches (create only; the pre-push hook refuses the rest)"),
    @("Pull requests", "Read and write", "open draft PRs; read and post PR conversation comments, review comments and reviews"),
    @("Commit statuses", "Read-only", "CI status of PR heads (comments command)"),
    @("Actions", "Read-only", "workflow runs of PR heads, if CI runs on the fork")
)
$upPerms = @(
    @("Metadata", "Read-only", "mandatory; repository lookups"),
    @("Contents", "Read-only", "PR files and commits (the repo is public; NOT write: no push, merge or branch deletion)"),
    @("Pull requests", "Read and write", "open draft PRs from $forkOwner branches; read and post comments, review comments and reviews"),
    @("Commit statuses", "Read-only", "CI status of PR heads"),
    @("Actions", "Read-only", "workflow runs of PR heads (openblack's Build workflow)")
)
$never = "Issues (PR comments go through Pull requests), Workflows, Administration, Secrets, Variables, Environments, Webhooks, Deployments, Pages, Discussions, Codespaces, Custom properties, everything else: No access"

function Show-Credential([string]$title, [string]$key, [string]$owner, [string]$repo, $perms, [string[]]$notes) {
    Write-Host ""
    Write-Host "== $title ==" -ForegroundColor Cyan
    Write-Host "Create a fine-grained token: $newToken"
    Write-Host "  Token name:        bw1-sandbox $key"
    Write-Host "  Resource owner:    $owner"
    Write-Host "  Expiration:        90 days (renew by re-running deploy.ps1 with the new token)"
    Write-Host "  Repository access: Only select repositories -> $repo"
    Write-Host "  Repository permissions:"
    foreach ($p in $perms) { Write-Host ("    {0,-16} {1,-15} {2}" -f $p[0], $p[1], $p[2]) }
    Write-Host "    $never"
    Write-Host "  Account permissions: none"
    foreach ($n in $notes) { Write-Host "  $n" }
    Write-Host "  Put it in E:\api_tokens.json as ""$key"". It is copied only to the droplet's publisher user (0600)."
}

Show-Credential "1. Push, PRs and comments on $Fork" "github" $forkOwner $forkName $forkPerms @(
    "Pushes go only here: the publisher checks the push URL and its pre-push hook allows only creating new ai-sandbox/* branches.")
Show-Credential "2. PR-only on $Upstream (only for pr_target = ""openblack"")" "github_openblack_pr" $upOwner $upName $upPerms @(
    "Works because your account is a member of $upOwner. Fine-grained tokens cannot be used on repos where you are",
    "only an outside collaborator or not a member at all. If $upOwner requires approval of fine-grained tokens, the",
    "token stays read-only until an org owner approves it (Settings > Personal access tokens > Pending requests).",
    "The publisher uses it only through its gh wrapper (pr create/list/view, comments and comment-only reviews on",
    "harness PRs, a few read endpoints) and never for git. Do NOT use a classic token here: public_repo would let it",
    "push to $Upstream, because your account has write access there.")

Write-Host ""
Write-Host "Nothing else is needed. The droplet's DigitalOcean token and Claude token are described in the README."

if ($OutFile) {
    $template = [ordered]@{
        "digitalocean"        = "dop_v1_REPLACE_ME"
        "claude_oauth"        = "sk-ant-oat01-REPLACE_ME"
        "github"              = "github_pat_REPLACE_ME_fork"
        "github_openblack_pr" = "github_pat_REPLACE_ME_openblack_pr_only"
    }
    if (Test-Path -LiteralPath $OutFile) { throw "$OutFile exists; not overwriting it (it may hold real tokens)" }
    if ($PSCmdlet.ShouldProcess($OutFile, "write a placeholder tokens template (no secrets)")) {
        $template | ConvertTo-Json | Set-Content -LiteralPath $OutFile -Encoding ascii
        Write-Host "Wrote placeholder template $OutFile (fill it in, then merge it into E:\api_tokens.json)."
    }
}
