# bw1-harness: unattended matching runs for bw1-decomp

This project runs an AI agent (Claude Code, headless) on a DigitalOcean droplet. The agent works on
[openblack/bw1-decomp](https://github.com/openblack/bw1-decomp), a matching decompilation of Black & White.
Each run checks the result with the repository's own tools. When a run makes things better, it pushes a new
branch and opens a pull request. Terraform provisions the droplet.

**Nothing here is switched on.** You switch it on yourself with `terraform/deploy.ps1`. That step needs
three secrets: a DigitalOcean token, an Anthropic API key and a GitHub token. The script refuses to run
unless all three are present. Terraform refuses as well, and so does the service on the droplet.

## Contents

```
terraform/
  versions.tf               Terraform >= 1.9, DigitalOcean provider pinned to 2.104.0, archive 2.8.1
  variables.tf              droplet, SSH, game files, GitHub, agent and limit settings; the two keys (sensitive, empty)
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
  check-ready.sh            ExecStartPre: refuses to start without both keys, the game files and claude
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
  reads tokens file -> env only             /etc/bw1-harness/secrets.env   ANTHROPIC_API_KEY, GH_TOKEN (0640)
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
  which is not recorded in the state. The state does hold a SHA-256 hash of the two keys, so that a
  new key re-runs the setup. The saved plan holds the keys, so `deploy.ps1` deletes it on exit. The
  agent process is started without `GH_TOKEN`. Only the harness script itself pushes and opens PRs.

## How a run works (`harness/bw1-harness.sh`)

1. **Guards.** The run stops if the stop file exists or if another run holds the lock. It refuses
   unless both keys are in `secrets.env`. It also stops if the day's PRs, spend or runs are at
   their caps, or if `MAX_OPEN_PRS` harness PRs are already open.
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
   with a summary file. The run enforces `--max-turns`, `--max-budget-usd`, a wall-clock timeout,
   and deny rules for git/gh/network tools.
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
| agent spend per run, USD (`max_usd_per_run`) | 5 |
| agent spend per UTC day, USD (`max_usd_per_day`) | 25 |
| runs per UTC day (`max_runs_per_day`) | 12 |
| agent turns per run (`max_turns`) | 150 |
| agent wall clock per run, minutes (`run_timeout_min`) | 120 |
| whole run, systemd `TimeoutStartSec` | 6 h |
| gap between runs (`run_interval`) | 2 h |
| kill switch | `/etc/bw1-harness/STOP` |

The daily spend cap counts what Claude Code reports per run (`total_cost_usd`), so one run can go
over it by at most `max_usd_per_run`. If the installed Claude Code has no `--max-budget-usd`, the run
logs a warning and relies on the turn cap, the timeout and the daily cap.

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
     "anthropic": "sk-ant-REPLACE_ME",
     "github": "github_pat_REPLACE_ME"
   }
   ```

   * `digitalocean`: a DigitalOcean API token with write scope (droplets, SSH keys, firewalls).
   * `anthropic`: an Anthropic Console API key for Claude Code, billed per token.
     `-AnthropicKey` overrides it.
   * `github`: a token for the account that owns the fork. `-GithubToken` overrides it.
     * A fine-grained PAT on the fork needs Contents read/write and Pull requests read/write.
     * Add Pull requests read/write on `openblack/bw1-decomp` if `pr_repo` points upstream.
     * For `orig_source = "ghcr"` you need a classic PAT with `read:packages` and access to the
       private `openblack/bw1-build` package.
2. **A fork** of openblack/bw1-decomp under that account.
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

1. Checks that all three secrets are present.
2. Checks the Terraform version, including against any existing state.
3. Runs `terraform init` and `terraform plan -out`.
4. Prints the resources it will create, the droplet size, the fork/PR target, the model and the limits.
5. Asks you to type `deploy`, then applies that exact plan.
6. Prints the droplet IP and the commands to check it.

The secrets go to Terraform only as `DIGITALOCEAN_TOKEN`, `TF_VAR_anthropic_api_key` and
`TF_VAR_github_token`, set in the script's own process.

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
* **Revoke:** delete the Anthropic key and the GitHub token at their providers. That stops
  everything at once, wherever the keys are.
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
| Anthropic usage | capped at `max_usd_per_day` = $25 per day, so at most about $750 per month; actual cost depends on the model and how often runs find work |
| bandwidth, snapshots | negligible; no volumes or reserved IPs are created |

Destroy the droplet when you are not using it. Billing is hourly.

## Open questions for you

1. **Keys.** Who owns the Anthropic key, and what monthly limit is set on it in the Anthropic
   Console? That limit is a second cap, independent of this project. Which GitHub account owns the
   token?
2. **Where PRs go.** The default opens draft PRs inside your fork, for you to review and forward.
   PRs straight to `openblack/bw1-decomp` (`pr_repo`) need the maintainers' agreement.
3. **Game files.** Upload your own `orig/` (`archive`, the default)? Or get `read:packages` access to
   the private CI image (`ghcr`)? Note that `bw1-build` is private to the openblack organisation.
4. **Size and region.** `s-8vcpu-16gb` in `lon1`, always on, or something smaller or larger?
5. **Limits and model.** 3 PRs per day, 5 open, $5 per run, $25 per day, model `claude-opus-5-5` (Opus 5.5), a run every
   2 h. Do these suit you?
6. **Commit identity.** Which `git_author_name` / `git_author_email` should go on harness commits?
7. **Not verified until the first run:**
   * that the full BW1W120 build passes on Ubuntu under `wibo`, including the one ICC unit. CI does
     this in a container whose contents are private.
   * which Claude Code flags the installed version supports (`--max-budget-usd`).
8. **No Ghidra.** The interactive workflow uses Ghidra, which is not on the droplet, so the agent
   works from objdiff assembly and the Mac symbols. Expect a lower hit rate than interactive work.
   Flipping a fully matched unit to `Matching` in `configure.py` is left to a human.
