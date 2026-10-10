# bw1-harness: unattended matching runs for bw1-decomp

This project runs an AI agent (Claude Code, headless) on a DigitalOcean droplet. The agent works on
[openblack/bw1-decomp](https://github.com/openblack/bw1-decomp), a matching decompilation of Black & White.
Each run checks the result with the repository's own tools. When a run makes things better, it pushes a new
branch and opens a pull request. Terraform provisions the droplet.

**Nothing here is switched on.** You switch it on yourself with `terraform/deploy.ps1`. That step needs
three secrets: a DigitalOcean token, a GitHub token and a Claude credential. The Claude credential is
either a long-lived subscription token from `claude setup-token` (preferred) or an Anthropic API key.
The script refuses to run unless all three are present. Terraform refuses as well, and so does the
service on the droplet.

## Contents

```
terraform/
  versions.tf               Terraform >= 1.9, DigitalOcean provider pinned to 2.104.0, archive 2.8.1
  variables.tf              droplet, SSH, game files, GitHub, agent and limit settings; the keys (sensitive, empty)
  main.tf                   SSH key, droplet, SSH-only firewall, uploads over SSH, setup script
  outputs.tf                IP, ssh, status and stop commands, PR list link
  cloud-init.yaml           first-boot packages only
  terraform.tfvars.example  copy to terraform.tfvars (gitignored); no secrets
  backend-spaces.tf.example optional remote state in DigitalOcean Spaces
  deploy.ps1                the switch: tokens file -> env -> init -> plan -> typed confirmation -> apply
  teardown.ps1              terraform destroy with a typed confirmation
harness/
  bw1-harness.sh            one run (the loop body); --dry-run prints what it would do
  harness_lib.py            pick work, judge improvement, daily limits, cost, PR body (stdlib only)
  prompt.md.tmpl            the bounded instructions given to the agent
  setup-droplet.sh          provisioning run by Terraform: packages, gh, Claude Code, clone, game files, systemd
  check-ready.sh            ExecStartPre: refuses to start without a Claude credential, GH_TOKEN, the game files and claude
  git-hooks/pre-push        only lets a push create a new branch under harness/
  systemd/                  bw1-harness.service (oneshot), bw1-harness.timer, logrotate
  config.env.example        every setting the run reads (Terraform writes the real one)
scripts/
  pack-orig.ps1             packs your local orig/ files for upload
```

## Architecture

```
your PC                                    DigitalOcean droplet (Ubuntu 24.04)
deploy.ps1                                  /etc/bw1-harness/config.env    settings (0640 root:harness)
  reads tokens file -> env only             /etc/bw1-harness/secrets.env   CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY, GH_TOKEN (0640)
  terraform plan/apply ----- SSH -------->  /opt/bw1-harness/harness       these scripts
                                            /srv/bw1-harness/bw1-decomp    clone (remote "upstream" fetch-only,
                                                                           remote "fork" for pushes) + orig/
                                            bw1-harness.timer -> bw1-harness.service (user "harness")
                                              -> bw1-harness.sh, one run at a time
                                            /var/log/bw1-harness/harness.log, runs/<id>/
```

* **Build on Linux.** `configure.py` downloads `dtk`, `objdiff-cli`, `lld-link`, the MSVC 6.0 and ICC 5.0
  compilers and `wibo`. On Linux x86_64 it runs the compilers under `wibo` automatically, with no wine.
  The repository's CI does the same thing in the private `ghcr.io/openblack/bw1-build` container.
* **Game files.** They are copyrighted and never downloaded. The build needs the decrypted BW1W120
  executable and its four DLLs, the MSVC 6 CRT libs, AMaths, the Intel `libircmt.lib` and the
  DirectX 7 DDK, all under `orig/`. Pick one of three sources (`orig_source`):
  * `archive` (default): run `scripts/pack-orig.ps1 -OrigDir <your bw1-decomp>\orig` on your PC.
    Terraform uploads the `.tar.gz` over SSH and the setup unpacks it into the clone.
  * `ghcr`: the setup copies `/orig` out of the private CI image. The GitHub token must be able to
    pull it.
  * `manual`: you `scp` the files to `/srv/bw1-harness/bw1-decomp/orig/` yourself.

  The setup checks the files against `config/BW1W120/build.sha1`. No run starts while the executable
  is missing.
* **Secrets.** The secrets exist only in your tokens file, in the deploy process's environment and in
  `/etc/bw1-harness/secrets.env` on the droplet. Terraform writes that file with a file provisioner,
  which is not recorded in the state. Only the Claude credential in use is written there. The state
  does hold a SHA-256 hash of the keys, so that a new key re-runs the setup. The saved plan holds the keys, so `deploy.ps1` deletes it on exit. The
  agent process is started without `GH_TOKEN`. Only the harness script itself pushes and opens PRs.

## How a run works (`harness/bw1-harness.sh`)

1. **Guards.** The run stops if the stop file exists or if another run holds the lock. It refuses
   unless `secrets.env` has a Claude credential and `GH_TOKEN`, and it logs which auth mode it
   uses (`oauth` or `api-key`, never the value). It also stops if the day's PRs or runs are at their
   caps, if the dollar cap is reached (API-key mode only), if a usage/rate-limit back-off is still
   running, or if `MAX_OPEN_PRS` harness PRs are already open.
2. **Sync and baseline.** The run hard-resets the clone to `upstream/main`. Then it runs
   `configure.py`, a full `ninja` and `ninja baseline`. If upstream does not build, the run stops.
3. **Choose work** (`harness_lib.py pick`). From the baseline report it takes non-auto-generated
   units whose source file exists. It skips units that already have an open harness PR, and functions
   tried in the last 7 days. It keeps functions of 16-600 bytes at 50-99.5% fuzzy match. Functions
   at 99.9% are usually known compiler tie-breaks, and AGENTS.md says to leave those. The pick is the
   unit with the smallest remaining work, plus up to 4 functions from it.
4. **Agent.** The run executes `claude -p` as the unprivileged `harness` user with the prompt
   `prompt.md.tmpl`. The prompt points the agent to AGENTS.md (CLAUDE.md loads it automatically).
   It lets the agent edit only that unit's source and the headers it needs. The inner loop is
   `ninja <object>` followed by `decomp-diff.py`, with at most 12 cycles per function. Fakematches,
   git writes and `gh`/`curl` are forbidden, and so is flipping objects to `Matching`. The agent ends
   with a summary file. The run enforces `--max-turns`, a wall-clock timeout, deny rules for
   git/gh/network tools and, in API-key mode only, `--max-budget-usd`. Only the credential in use is
   in the agent's environment: with OAuth it gets `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_API_KEY`
   is unset, so the key cannot take precedence. `GH_TOKEN` is never in it. If the run stops on a
   usage or rate limit (found in the transcript's error result, error messages, or the stderr of a
   failed run), the run is discarded cleanly. No run then starts for `rate_limit_backoff_min`
   (180 min), or until the reset time Claude Code reports if that is later. The run is not retried
   in a loop.
5. **Verify.**
   * Changed files must match `ALLOWED_PATHS` (`src/*,include/*`).
   * `tools/decomp-verify.py --changed --run --baseline build/BW1W120/baseline.json` runs whole-file
     clang-format, compiler replay, the full build with SHA-1 checks, a report refresh and the
     regression audit.
   * `tools/decomp-regress.py --refresh --fail-on-regression` must pass.
   * `harness_lib.py compare` must show no function worse, matched code not lower, and either a newly
     matched function or a unit fuzzy gain of at least 0.5%.

   Anything else is discarded with `git reset --hard`.
6. **PR.** The run commits as the configured author, with a `Co-Authored-By: Claude` trailer. It
   pushes `HEAD:refs/heads/harness/<date>-<unit>-<id>` to the fork; the pre-push hook only allows
   creating a new `harness/` branch, so there are no force pushes, no updates and no `main`. Then it
   runs `gh pr create` (draft by default). The PR body lists the before/after numbers per function,
   the agent's notes and the verification that ran.

Each run keeps `prompt.md`, `work.json`, `transcript.jsonl` (the full agent stream), the build,
verify and regress logs, `compare.json` and `pr-body.md` in `/var/log/bw1-harness/runs/<id>/` for 30 days.

### Safety limits (Terraform variables, defaults)

| limit | default |
|---|---|
| PRs opened per UTC day (`max_prs_per_day`) | 3 |
| open harness PRs before runs pause (`max_open_prs`) | 5 |
| agent spend per run, USD (`max_usd_per_run`), API-key mode only | 5 |
| agent spend per UTC day, USD (`max_usd_per_day`), API-key mode only | 25 |
| pause after a usage or rate limit, minutes (`rate_limit_backoff_min`) | 180 |
| runs per UTC day (`max_runs_per_day`) | 12 |
| agent turns per run (`max_turns`) | 150 |
| agent wall clock per run, minutes (`run_timeout_min`) | 120 |
| whole run, systemd `TimeoutStartSec` | 6 h |
| gap between runs (`run_interval`) | 2 h |
| kill switch | `/etc/bw1-harness/STOP` |

The daily spend cap counts what Claude Code reports per run (`total_cost_usd`), so one run can go
over it by at most `max_usd_per_run`. If the installed Claude Code has no `--max-budget-usd`, the run
logs a warning and relies on the turn cap, the timeout and the daily cap. In subscription mode the
reported dollar figure is only an estimate. It is logged but not enforced; the turn, timeout, run and
PR caps are the limits.

## Dry run (no API, no build, no changes)

```bash
# on any machine with bash and python, against an existing local checkout that has a report.json
sed -e 's|^REPO_DIR=.*|REPO_DIR=/path/to/bw1-decomp|' -e 's|^STATE_DIR=.*|STATE_DIR=/tmp/h/state|' \
    -e 's|^LOG_DIR=.*|LOG_DIR=/tmp/h/log|' -e 's|^STOP_FILE=.*|STOP_FILE=/tmp/h/STOP|' \
    harness/config.env.example > /tmp/h.env
PYTHON=python3 bash harness/bw1-harness.sh --dry-run --config /tmp/h.env --secrets /dev/null
```

It prints every git, ninja, claude and gh command it would run. It also renders the real prompt into
the run folder. On the droplet: `sudo -u harness bw1-harness --dry-run`.

## Switching it on

You will need:

1. **The tokens file**, `E:\api_tokens.json` by default (`-TokensFile` to use another). It never goes
   into this repository. Its format, with placeholder values:

   ```json
   {
     "digitalocean": "dop_v1_REPLACE_ME",
     "claude_oauth": "sk-ant-oat01-REPLACE_ME",
     "github": "github_pat_REPLACE_ME"
   }
   ```

   * `digitalocean`: a DigitalOcean API token with write scope (droplets, SSH keys, firewalls).
   * Exactly one Claude credential is needed:
     * `claude_oauth` (preferred): your Claude subscription, see [Claude auth](#claude-auth-subscription-or-api-key).
       `-ClaudeOAuthToken` overrides it.
     * `anthropic`: an Anthropic Console API key (`"anthropic": "sk-ant-api03-REPLACE_ME"`),
       billed per token. `-AnthropicKey` overrides it.
     * If both are set, the subscription token is used, the script says so, and the API key is not
       sent to the droplet.
   * `github`: a token for the account that owns the fork. `-GithubToken` overrides it.
     * A fine-grained PAT on the fork needs Contents read/write and Pull requests read/write.
     * Add Pull requests read/write on `openblack/bw1-decomp` if `pr_repo` points upstream.
     * For `orig_source = "ghcr"` you need a classic PAT with `read:packages` and access to the
       private `openblack/bw1-build` package.
2. **A fork** of openblack/bw1-decomp at `raffclar/bw1-decomp` (the default `fork_repo`; it does not exist yet, so create it
   first). Branches are only ever pushed to raffclar, never to openblack: Terraform and the harness both refuse an
   `openblack/*` fork_repo, and the clone's upstream remote has pushing disabled.
3. **The game files**: `scripts/pack-orig.ps1 -OrigDir C:\projects\bw1-decomp\orig`, or one of the
   other sources above.
4. **`terraform/terraform.tfvars`**: copy it from `terraform.tfvars.example`. Set at least
   `ssh_allowed_cidrs`, `fork_repo`, `git_author_*`, `orig_archive_path` and the SSH key paths.
   `deploy.ps1` refuses a tfvars file that assigns a secret.

Then:

```powershell
powershell -ExecutionPolicy Bypass -File E:\bw1-decomp\terraform\deploy.ps1
#   -PlanOnly          everything up to the plan, then stop
```

The script does these steps in order:

1. Checks that the DigitalOcean token, the GitHub token and a Claude credential are present, and
   prints which Claude auth mode it will use.
2. Checks the Terraform version, including against any existing state.
3. Runs `terraform init` and `terraform plan -out`.
4. Prints the resources it will create, the droplet size, the fork/PR target, the auth mode, the
   model and the limits.
5. Asks you to type `deploy`, then applies that exact plan.
6. Prints the droplet IP and the commands to check it.

The secrets go to Terraform only as `DIGITALOCEAN_TOKEN`, `TF_VAR_claude_oauth_token` or
`TF_VAR_anthropic_api_key`, and `TF_VAR_github_token`, all set in the script's own process.

## Claude auth: subscription or API key

**Making the subscription token.** On your own PC, log Claude Code in to the subscription you want
the harness to use (`claude`, then `/login` if needed). Then run:

```powershell
claude setup-token
```

It prints a long-lived OAuth token. Put that token in the tokens file as `"claude_oauth"`, and remove
or empty `"anthropic"` unless you want it as a fallback for later. On the droplet it becomes
`CLAUDE_CODE_OAUTH_TOKEN` in `/etc/bw1-harness/secrets.env`. Treat it like a password: it acts as your
subscription until you revoke it.

**Trade-offs.**

| | subscription (`claude_oauth`) | API key (`anthropic`) |
|---|---|---|
| billing | included in the plan, no per-token bill | per token, on the Console account |
| limits | the plan's usage and rate limits, **shared with your own interactive Claude use**: a busy harness can leave you rate-limited, and the other way round | Console rate limits and the spend limit you set there |
| dollar caps here | none (figures logged as estimates) | `max_usd_per_run` (`--max-budget-usd`) and `max_usd_per_day` |
| what bounds a run | turns, timeout, runs/day, PRs/day, back-off after a limit | the same, plus the dollar caps |

With a subscription, keep `max_runs_per_day` and `run_interval` modest so the harness leaves room for
your own use. When a limit is hit, the harness backs off instead of retrying.

**Checking the credential on the droplet.** This sends one tiny request:

```bash
ssh root@<ip>
set -a; . /etc/bw1-harness/secrets.env; set +a
# subscription mode: make sure the API key cannot take precedence
env -u ANTHROPIC_API_KEY -u GH_TOKEN sudo -E -H -u harness claude -p "say ok" --max-turns 1
# API-key mode instead:  env -u CLAUDE_CODE_OAUTH_TOKEN -u GH_TOKEN sudo -E -H -u harness claude -p "say ok" --max-turns 1
```

The answer should be `ok`. Each run also logs `claude auth mode: oauth` or `api-key` in
`/var/log/bw1-harness/harness.log`.

**Switching modes.** Edit the tokens file: set `claude_oauth` for subscription mode, or empty it and set
`anthropic` for API-key mode. Then run `deploy.ps1` again. The credential hash changes, so Terraform
re-runs the setup and rewrites `secrets.env` with only the credential now in use. The next run picks the
mode up from `secrets.env`; nothing else changes. For a quick switch on the droplet alone, edit
`/etc/bw1-harness/secrets.env` by hand. If both credentials are present there, the OAuth token wins. A
later `deploy.ps1` overwrites a hand edit.

To create the droplet without starting runs, set `harness_enabled = false`. You can enable it later
with `systemctl enable --now bw1-harness.timer`, or by applying again with `true`. The first run
starts about 10 minutes after the timer is enabled. It is slow because it does the first full build.

## Checking it works

```bash
ssh root@<ip> tail -n 40 /var/log/bw1-harness-setup.log         # provisioning result, orig hash check
ssh root@<ip> systemctl list-timers bw1-harness.timer             # next run
ssh root@<ip> systemctl status bw1-harness.service --no-pager     # last run's result
ssh root@<ip> tail -n 50 /var/log/bw1-harness/harness.log         # every step of every run
ssh root@<ip> ls -t /var/log/bw1-harness/runs | head              # per-run folders
ssh root@<ip> sudo -u harness /opt/bw1-harness/harness/check-ready.sh
```

`terraform output pull_requests` gives the link to the harness PRs.

## Stopping and tearing down

* **Pause:** `ssh root@<ip> touch /etc/bw1-harness/STOP`. No new run starts. A running one stops at
  its next checkpoint (before the agent, before verification, before the PR).
* **Stop:** `ssh root@<ip> 'touch /etc/bw1-harness/STOP && systemctl disable --now bw1-harness.timer && systemctl stop bw1-harness.service'`.
* **Resume:** remove the stop file and `systemctl enable --now bw1-harness.timer`.
* **Revoke:** delete the Claude credential and the GitHub token at their providers. That stops
  everything at once, wherever the keys are. A `setup-token` token is revoked from your Claude
  account settings; an API key from the Anthropic Console.
* **Tear down:** `powershell -ExecutionPolicy Bypass -File E:\bw1-decomp\terraform\teardown.ps1`.
  This runs `terraform destroy` on the droplet, the firewall and the SSH key, after you type
  `destroy`. It needs only the `digitalocean` token. Branches and PRs on GitHub are left alone.

## Remote state

The state is local (`terraform/terraform.tfstate`, gitignored) by default. It holds no keys, only
their hash. To share the state or keep it off this PC:

1. Copy `backend-spaces.tf.example` to `backend.tf` (gitignored).
2. Create the Spaces bucket.
3. Export a Spaces key as `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.
4. Run `terraform init -migrate-state`.

## Cost estimate (October 2026 list prices; check before applying)

| item | estimate |
|---|---|
| droplet `s-8vcpu-16gb` (default), always on | about $96 per month (about $0.14 per hour) |
| alternatives | `s-4vcpu-8gb` about $48 per month (slower builds); `c-8` dedicated CPU about $168 per month |
| Claude, subscription mode | no extra bill; uses the plan's usage limits, shared with your own use |
| Claude, API-key mode | capped at `max_usd_per_day` = $25 per day, so at most about $750 per month; actual cost depends on the model and how often runs find work |
| bandwidth, snapshots | negligible; no volumes or reserved IPs are created |

Destroy the droplet when you are not using it. Billing is hourly.

## Open questions for you

1. **Keys.** Subscription token (preferred) or API key? With a subscription, how much of its usage
   can the harness take from your own interactive use (`max_runs_per_day`, `run_interval`)? With an
   API key, what monthly limit is set on it in the Anthropic Console? That limit is a second cap,
   independent of this project. Which GitHub account owns the GitHub token?
2. **Where PRs go.** The default opens draft PRs inside your fork, for you to review and forward.
   PRs straight to `openblack/bw1-decomp` (`pr_repo`) need the maintainers' agreement.
3. **Game files.** Upload your own `orig/` (`archive`, the default)? Or get `read:packages` access to
   the private CI image (`ghcr`)? Note that `bw1-build` is private to the openblack organisation.
4. **Size and region.** `s-8vcpu-16gb` in `lon1`, always on, or something smaller or larger?
5. **Limits and model.** 3 PRs per day, 5 open, a 3 h pause after a usage limit, and in API-key mode
   $5 per run and $25 per day, model `claude-opus-5-5` (Opus 5.5), a run every
   2 h. Do these suit you?
6. **Commit identity.** Which `git_author_name` / `git_author_email` should go on harness commits?
7. **Not verified until the first run:**
   * that the full BW1W120 build passes on Ubuntu under `wibo`, including the one ICC unit. CI does
     this in a container whose contents are private.
   * which Claude Code flags the installed version supports (`--max-budget-usd`).
   * the exact text Claude Code prints when a subscription usage limit is hit. Detection matches the
     known forms ("usage limit reached|<reset>", rate_limit_error, 429). Check the first back-off
     in the log.
8. **No Ghidra.** The interactive workflow uses Ghidra, which is not on the droplet, so the agent
   works from objdiff assembly and the Mac symbols. Expect a lower hit rate than interactive work.
   Flipping a fully matched unit to `Matching` in `configure.py` is left to a human.
