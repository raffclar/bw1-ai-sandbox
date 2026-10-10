# bw1-sandbox droplet: bw1-ai-sandbox on DigitalOcean

This runs [bwrsandman/bw1-ai-sandbox](https://github.com/bwrsandman/bw1-ai-sandbox), the maintainer's harness for
[openblack/bw1-decomp](https://github.com/openblack/bw1-decomp), on a DigitalOcean droplet. The harness runs several
`claude -p --dangerously-skip-permissions` matching workers, each in a hardened Docker container. A host-side
**publisher** pushes finished workers as new branches to **your fork only** (`raffclar/bw1-decomp`). It opens
**draft PRs** on the fork, or, with `pr_target = "openblack"`, on `openblack/bw1-decomp` from those branches.
It also follows the comments on them. Terraform builds the droplet and assigns it to the existing DigitalOcean project "for bw1-decomp".

**Nothing here is switched on.** You switch it on with `terraform/deploy.ps1`, which refuses to run without all of:

* a DigitalOcean token;
* your Claude subscription token, from `claude setup-token`;
* a GitHub token for the fork, plus a PR-only token for openblack if `pr_target = "openblack"`;
* the game files: by default your zip, `E:/bw1-original.zip`, which must exist.

No worker starts by itself. You start them yourself with the orchestrator, the portal or the CLI.

> [!CAUTION]
> bw1-ai-sandbox's own warning applies in full: **its security features are best effort.** Agents run unsupervised
> with `--dangerously-skip-permissions`. This port also gives up the sandbox's VM boundary (next section). The
> droplet being **disposable** is the main protection. Treat it as untrusted, keep nothing else on it, and tear it
> down when you are not using it.

## Why the droplet stands in for the VM

bw1-ai-sandbox puts its workers in a libvirt/KVM VM. That needs `/dev/kvm` on the host, meaning nested
virtualisation on a droplet. DigitalOcean documents no nested virtualisation for droplets; there is no page for it on
docs.digitalocean.com. What exists are community answers that contradict each other. A 2021 staff answer says
it works in all regions but is not recommended "because there is often very poor performance". Other staff answers
and a support reply say it is not supported. A 2022 feature request says it works on Intel hosts but not on AMD. You
can't choose the CPU vendor on regular droplets.

Sources:
[1](https://digitalocean.com/community/questions/does-digitalocean-support-kvm-or-nested-virtulzation),
[2](https://www.digitalocean.com/community/questions/is-nested-kvm-enabled-on-digital-ocean-servers-can-i-run-kvm-in-a-droplet),
[3](https://ideas.digitalocean.com/core-compute-platform/p/enable-nested-kvm-in-amd-servers).

You chose the droplet itself as the "VM" (option a). The other options were a provider with nested
virtualisation or bare metal, or your own Linux machine. On those, the unmodified sandbox works as its README describes.

## What runs where

```
your PC                                  droplet (Ubuntu 24.04, project "for bw1-decomp")
deploy.ps1 ── SSH ──────────────────►    root: provisioning, bw1-egress (iptables), unpacking the game files
  tokens file -> env only                decomp (uid 1000, docker, sudo)   = the sandbox VM's user, ssh from 127.0.0.1 only
                                         sandbox  runs sandbox.py / portal / autoresume / orchestrator; Claude token
                                         publisher                        GitHub tokens; pushes (fork only), draft PRs, comments
                                         docker0: worker containers w-<name> (uid 2000, cap-drop ALL, no-new-privileges,
                                                  read-only rootfs, private /tmp, mirror and shared/ read-only)
```

* **The sandbox, unchanged.** It is cloned at a pinned commit (`sandbox_ref`) into `/opt/bw1-sandbox/bw1-ai-sandbox`,
  owned by root and read-only to the users who run it.
* **The wrapper.** `droplet/sandbox_droplet.py` loads the sandbox's own `sandbox.py` and replaces only the
  libvirt-facing methods of `Sandbox` in memory. It then runs the sandbox's CLI, portal or MCP server. Everything
  else is the sandbox's code: the paramiko connection (now to `decomp@127.0.0.1`), `vm_agent.py`, the Docker SDK
  path (`dockervm.py`), sync, spawn, fetch, review, autoresume and the worker hardening.
* **Worker egress.** `bw1-egress` adds iptables rules on docker0 (a `DOCKER-USER` jump, plus `INPUT`, plus IPv6
  DROP). They replace the sandbox's libvirt nwfilter. Workers reach **only TCP 443 to the Anthropic API ranges**
  (`api_nets` plus the resolved `api.anthropic.com`). They have no DNS, as in the VM, because the sandbox adds
  `api.anthropic.com` through `--add-host`. They can't reach GitHub, the DigitalOcean metadata service, the
  droplet's sshd or portal, or each other (`icc: false`). The worker image build uses the host network. The timer
  re-applies the rules every 15 minutes. `bw1-sandbox check` proves them from a real worker-image container.
* **Host egress.** The DigitalOcean cloud firewall allows inbound SSH from `ssh_allowed_cidrs` only. Outbound it
  allows only TCP 443 and 80, DNS and NTP. The host needs those for GitHub, Ubuntu mirrors, the tool downloads
  and the Anthropic API.
* **Publisher.** A timer runs `bw1-sandbox-export` as `sandbox`, which is the sandbox's own `fetch` for finished
  workers. It then runs `publisher.py` as `publisher`. Details are under [Publisher](#publisher).

### Threat model compared with bw1-ai-sandbox

| | bw1-ai-sandbox (VM) | this droplet |
|---|---|---|
| isolation boundary | KVM guest: workers share the **VM's** kernel | containers share the **droplet's** kernel with the host, the publisher (GitHub tokens), the game files and the Claude token |
| escape surface | KVM | container escape (kernel, Docker, runc) gives the whole droplet |
| network filter | libvirt nwfilter, enforced by the host outside the VM | iptables on the same kernel; root on the droplet can change it. Workers have no root and no capabilities, but a container escape would bypass it |
| host files | unreachable, no shares | not mounted into workers; reachable only after an escape |
| GitHub credentials | on your PC only; nothing pushes | on the droplet, publisher only (0600): the fork token and, with `pr_target = "openblack"`, the openblack PR-only token. Never in containers |
| Claude OAuth token | in every worker's env (as upstream) | the same |
| what limits damage | the VM, then your PC | **the droplet is disposable**: nothing else on it, a fine-grained token scoped to your fork, draft PRs only, `terraform destroy` |

The host-side users are trusted, not isolated from each other: `decomp` has sudo and Docker, as in the VM. Anything
running as `sandbox` holds the ssh key for `decomp`, so it can become root. That includes the orchestrator Claude
session, which upstream also runs on your host with normal permissions. Only the worker containers are the
untrusted zone.

### Changes to bw1-ai-sandbox

These live only in `droplet/sandbox_droplet.py`. Nothing in the sandbox checkout is modified, so the list can be
offered upstream or rebased onto a new `sandbox_ref`.

| method | upstream (VM) | droplet |
|---|---|---|
| `vm_state`, `vm_ip` | libvirt domain, DHCP lease | `running` once provisioned, `127.0.0.1` |
| `host_ip` | libvirt network address | docker0 gateway (only used for `ghidra.host`) |
| `locked` | nwfilter attached | `sudo bw1-egress check` |
| `setup_host` | enables libvirt sockets | no-op (provisioning) |
| `create` | virt-install, cloud-init, image, lockdown | push_bin, image, lockdown |
| `build_image` | refuses while locked; builds on the VM's network | builds with `network_mode=host` |
| `lockdown` | defines and attaches the nwfilter | `sudo bw1-egress apply`, then `check` |
| `check` | probes from inside the VM | probes from a throwaway worker-image container on docker0, including GitHub, the DO metadata service and host sshd |
| `unlock`, `start`, `shutdown`, `destroy`, `ghidra_forward`, `_set_filter`, `_restart` | libvirt operations | refused, with a reason |
| `portal` | `--pants-down-mode` opens it to any host | wrapper refuses `--pants-down-mode` and any non-loopback `--host` |

Configuration: `sandbox.droplet.toml` sets worker CPUs and memory and the game version, as the sandbox README
describes for other versions. `mcp.droplet.json` points the orchestrator at the wrapper's MCP server instead of the
repo's `.mcp.json`. Python dependencies come from a venv (Ubuntu's `python3-libvirt` plus pip `docker>=7`, `paramiko`,
`cryptography` and `mcp`). The libvirt bindings are only imported, never used.

## Game files

The copyrighted files are never committed here, and nothing downloads them. They go to the droplet **host** only.
The sandbox's `sync` copies them to `/srv/decomp/shared/data`, which workers mount **read-only**, as upstream.
Two sources (`orig_source`):

* **`archive` (default): your zip.** `orig_archive_path` defaults to `E:/bw1-original.zip`.
  * `deploy.ps1` checks that the file exists and prints its size, without opening it.
  * Terraform uploads it over SSH to `/opt/bw1-sandbox/incoming/`.
  * `droplet/unpack-orig.py` unpacks it on the host. Every member must sit under one top folder, `bw1-original/`
    or `orig/`, which is stripped, so `bw1-original/BW1W120/…` becomes `bw1-decomp/orig/BW1W120/…`. Any other top
    folder, more than one top folder, absolute paths, `..`, backslashes, drive letters, symlinks and hard links are
    refused before anything is written.
  * It then verifies `orig/BW1W120` against `config/BW1W120/build.sha1` (the exe and four DLLs) and stops if
    they don't match.
  * The uploaded archive is deleted after a successful unpack and check.

  A `.tar.gz` from `scripts/pack-orig.ps1` works the same way.
* **`manual`:** copy the files to `/opt/bw1-sandbox/bw1-ai-sandbox/bw1-decomp/orig/` and re-run the setup.

**What your zip covers.** From the list of names in it:

* It has `BW1W120/` (decrypted and encrypted exe, the four DLLs), `BW1WCI/`, `directx7.0/include/` and
  `libs/msvc6.5/uuid.lib`. That is enough for the hash check, the dtk split, and per-object compiling and diffing,
  which is how workers match functions (`ninja <object>`, `decomp-diff.py`).
* It does **not** have the libraries a full `ninja` link with the final SHA-1 check needs: `libs/msvc6.5/libcmt.lib`,
  `libcpmt.lib`, `msvcrtd.lib`, `libs/msvc6.4/libcmt.lib`, `libs/msvc6.0/libcimt.lib`, `libs/amaths-2.0/amaths.lib`
  and `directx7.0/lib/`. bw1-decomp's Getting Started explains where each comes from.
* The setup copies `libs/icc-5.0.115/libircmt.lib` out of the downloaded ICC compiler itself, as Getting Started
  describes. It warns about the rest but does not fail.

The game files never leave the droplet host and the workers' read-only mount. The publisher refuses any worker branch
that touches `orig/` or `.gitattributes`, or that adds a Git LFS pointer. Its pre-push hook checks the same again.

## Remotes and credentials

| remote | role | credential (tokens file key) | where it is stored |
|---|---|---|---|
| `raffclar/bw1-decomp` (`fork_repo`) | the **only** push target: new `ai-sandbox/*` branches. Draft PRs and comments there when `pr_target = "fork"` | `github`: fine-grained PAT, resource owner raffclar, only this repo | droplet `/etc/bw1-sandbox/github.env`, 0600 `publisher`; git's credential helper for the push, gh for PRs |
| `openblack/bw1-decomp` (`upstream_repo`) | fetch (anonymous). With `pr_target = "openblack"`: draft PRs with head `raffclar:ai-sandbox/*`, and comments on them. **Never a push, update or delete** | `github_openblack_pr`: fine-grained PAT, resource owner openblack, only this repo, **no Contents write** | droplet `/etc/bw1-sandbox/github-openblack-pr.env`, 0600 `publisher`; used only by the publisher's gh wrapper, never by git |

`scripts/new-github-credentials.ps1` prints these steps (page, owner, repository, permissions, key). It can also
write a placeholder tokens template with `-OutFile` (`-WhatIf` supported). It generates nothing secret: GitHub tokens
are only made on github.com.

### Permissions (fine-grained tokens; everything not listed: No access)

| permission | `github` (raffclar/bw1-decomp) | `github_openblack_pr` (openblack/bw1-decomp) | needed for |
|---|---|---|---|
| Metadata | Read | Read | mandatory for every fine-grained token |
| Contents | **Read and write** | **Read** | fork: pushing branches. openblack: PR files and commits. Not write: no push, merge or branch deletion |
| Pull requests | Read and write | Read and write | create, list and view PRs; read and post PR conversation comments, review comments and reviews |
| Commit statuses | Read | Read | CI state of PR heads (`comments`) |
| Actions | Read | Read | workflow runs of PR heads (`comments`; openblack's Build workflow) |
| Issues | No | No | not needed: the issue-comment endpoints on PRs also accept Pull requests |
| Checks | (none) | (none) | the Checks API is not available to fine-grained PATs; CI is read through Commit statuses and Actions |
| Workflows, Administration, Secrets, Variables, Environments, Webhooks, Deployments, Pages, Discussions, others | No | No | nothing uses them |

Sources:

* the [permissions for fine-grained tokens](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens):
  * `POST /repos/{o}/{r}/pulls` needs Pull requests: write;
  * `GET/POST /repos/{o}/{r}/issues/{n}/comments` is listed under **both** Issues and Pull requests;
  * `POST /pulls/{n}/comments` and `/pulls/{n}/reviews` need Pull requests: write;
  * `GET /commits/{ref}/status` needs Commit statuses: read;
  * `GET /actions/runs` needs Actions: read;
* the [token limitations](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

### PR-only rights on openblack: what works

* **A fine-grained PAT can do it, because raffclar is a member of openblack.** Checked read-only with
  `gh api user/memberships/orgs/openblack`: role `member`. `repos/openblack/bw1-decomp` reports push access.
  * Fine-grained tokens "cannot contribute to public repos where the user is not a member" or is only an outside
    collaborator ([docs](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)).
    As a member, a token whose resource owner is **openblack**, limited to `bw1-decomp`, works. With Pull requests:
    write and Contents: read, it can open PRs and comment, but cannot push, merge (merging needs Contents: write)
    or delete branches. Your account's write access does not widen the token.
  * The org may require **approval** of fine-grained tokens. Until an owner approves it, the token "can only read
    public resources", so the first `pr create` fails until then.
  * GitHub's REST docs add that you "must have write access to the head" of a cross-repository PR. raffclar owns the
    fork. Whether GitHub accepts an openblack-scoped token for a `raffclar:` head is **not verifiable offline**, so
    the first openblack PR is the test.
* **A classic PAT is not least privilege.** `public_repo` grants "read/write access to code … for public
  repositories" ([scopes](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)). Since
  raffclar can push to openblack, a classic token **could push there**. Use it only as a fallback, if the org forbids
  fine-grained tokens or the head check above fails, and then only with `-AllowSameTokenForOpenblack` or a separate
  classic token. In that case only our guards (below) keep it PR-only. A cleaner fallback is a GitHub App installed on
  openblack by an owner, with Pull requests: write only.
* **No token type gives "PRs but not push" on its own** except the fine-grained token with Contents: read, so the
  publisher also enforces it in layers:
  1. git pushes go only to the remote `fork`, and only if its push URL is exactly `https://github.com/raffclar/bw1-decomp.git`;
  2. the pre-push hook refuses openblack URLs, refs outside `ai-sandbox/`, updates, deletions and game files;
  3. the publisher clone's `origin` (openblack) has its push URL set to `DISABLED`;
  4. every gh call goes through `GhGuard`. Only `pr create` (draft, harness head), `pr list`, `pr view`, `pr comment`
     and `pr review --comment` on harness PRs, and `GET` on five read endpoints are allowed. Editing or deleting
     comments, approving, closing, merging, ready, reopen, labels, issues, other repos, other gh commands, other HTTP
     methods and `api` with fields are refused;
  5. the openblack token is passed only to that wrapper's gh process, never to git.

  `droplet/tests/test_publisher.py` tests each of these.

## Publisher

On a timer (`publish_interval`, 30 min), as the `publisher` user:

1. **Export.** `bw1-sandbox-export` runs first, as `sandbox`. It is the sandbox's own `fetch` for finished workers,
   and it writes their state, final result and `sandbox/<name>` head to `/var/lib/bw1-sandbox/export/workers.json`.
2. **Pick.** It takes workers that are exited, not stopped on the usage limit, have new commits, and are not already
   published or rejected.
3. **Fetch.** It fetches each one from the sandbox clone into its own bare clone. Only objects and refs come across,
   so worker code is **never built or run** on the host.
4. **Refuse.** It rejects anything that touches `orig/`, `.gitattributes` or LFS pointers, any change outside
   `allowed_paths`, and changes over 40 files or 4000 lines.
5. **Push.** It pushes the commits unchanged as a **new** branch, `ai-sandbox/<date>-<name>-<sha7>`, to
   `raffclar/bw1-decomp` only.
6. **Open a draft PR** on the PR target, base `main`:
   * `pr_target = "fork"`: on `raffclar/bw1-decomp`, head `<branch>`;
   * `pr_target = "openblack"`: on `openblack/bw1-decomp`, head `raffclar:<branch>`.
7. **Limits, per target.** `max_prs_per_day` (3) per day; it waits while `max_open_prs` (5) harness PRs are open on
   the target.

**Comments** (`bw1-publisher-comments.timer`, every 15 min; or `publisher.py comments`):

* It covers the open harness PRs on both targets (head `ai-sandbox/*` in raffclar), each with that target's token.
* It fetches new conversation comments, review comments and reviews since the last check, plus the head's commit
  status and Actions runs.
* It appends them to `/var/lib/bw1-publisher/comments/comments.jsonl` and logs a summary. Each item is seen once.

**Replying:** `publisher.py reply --target fork|openblack --pr N --body-file F [--review]` posts a new conversation
comment, or a comment-only review, on that PR. It works only if the PR is a harness PR. It never edits or deletes
anyone's comments.

**Feeding reviews back to workers (off by default).** With `feedback_to_sandbox = true`:

* new comments from `feedback_authors` (default: the fork owner only) are queued in `/var/lib/bw1-sandbox/feedback/<worker>.md`;
* `bw1-sandbox-feedback.timer` resumes the worker that made the PR, with the sandbox's own `resume`, using them as
  its follow-up prompt;
* comments from anyone else are stored but never reach a worker. On public openblack PRs anyone can comment, so
  widening `feedback_authors` lets strangers prompt your workers.

## Switching it on

1. **Tokens file** (`E:\api_tokens.json`; never in this repo). The format, with placeholders:

   ```json
   {
     "digitalocean": "dop_v1_REPLACE_ME",
     "claude_oauth": "sk-ant-oat01-REPLACE_ME",
     "github": "github_pat_REPLACE_ME",
     "github_openblack_pr": "github_pat_REPLACE_ME_ONLY_FOR_PR_TARGET_OPENBLACK"
   }
   ```

   * `digitalocean`: write access to droplets, SSH keys, firewalls and projects.
   * `claude_oauth`: run `claude setup-token` on your PC, logged in to your subscription, and paste the output here.
     The sandbox runs workers on this token (`sandbox.py token` stores exactly `CLAUDE_CODE_OAUTH_TOKEN=<token>`).
     deploy.ps1 writes the same file, because `sandbox.py token` only reads it interactively (`getpass`).
     An `anthropic` API key is not used.
   * `github` and `github_openblack_pr`: see [Remotes and credentials](#remotes-and-credentials), or run
     `scripts/new-github-credentials.ps1`. `github_openblack_pr` is required only with `pr_target = "openblack"`.
     deploy.ps1 refuses it if it equals `github`, unless you pass `-AllowSameTokenForOpenblack`, and then it warns.
2. **Fork.** `raffclar/bw1-decomp` must exist. Keep its `main` in step with upstream yourself; the publisher never
   touches `main`.
3. **Game files.** By default `E:/bw1-original.zip` must exist; nothing else is needed.
4. **`terraform/terraform.tfvars`.** Copy it from `terraform.tfvars.example`. Your current `terraform.tfvars` still
   has settings of the old harness (`pr_draft`, `git_author_*` …). Terraform only warns about them, but remove them.
5. **Run:**

   ```powershell
   powershell -ExecutionPolicy Bypass -File E:\bw1-decomp\terraform\deploy.ps1           # -PlanOnly to stop before apply
   ```

   The script checks the secrets, and the openblack PR token when `pr_target = "openblack"`. It checks that the zip
   exists and prints its size, refuses secrets in tfvars, and checks the Terraform version. It then runs `init` and
   `plan -out`, and prints a summary: resources, the DO project, droplet, sandbox commit, game files, workers, both
   remotes and their roles, publisher and services. It applies only after you type `deploy`.

   The secrets reach Terraform only as `DIGITALOCEAN_TOKEN`, `TF_VAR_claude_oauth_token`, `TF_VAR_github_token` and
   `TF_VAR_openblack_pr_token`. The saved plan holds them, so the script deletes it on exit. The state holds only a
   hash.

Provisioning installs everything, builds the host toolchain (sandbox README step 3), builds the worker image,
applies and checks the worker firewall, `sync`s and runs `check`. It takes a while because of the first toolchain
download and the image build.

## Using it

```bash
ssh root@<ip>
sudo -iu sandbox
bw1-sandbox check                  # workers reach the Anthropic API and nothing else
bw1-sandbox spawn farmer "Follow the decomp-matching skill for unit VillagerFarmer." -m opus -e high
bw1-sandbox status                 # workers + session/week usage
bw1-sandbox logs farmer
```

* **Orchestrator.** This also answers the earlier question: yes, the harness can be run from a skill.
  bw1-ai-sandbox's own `sandbox-orchestrator` skill drives the workers through its MCP server. On the droplet:
  1. `sudo -iu sandbox`, then `tmux`.
  2. Run `claude` once and `/login` (Remote Control needs a full login).
  3. Run `bw1-orchestrator`. It starts `claude --remote-control bw1-orchestrator` in the sandbox folder, with
     the droplet MCP config.
  4. From claude.ai/code or the app, say "use the sandbox-orchestrator skill: run 3 workers on …".

  Approve the `sandbox` MCP tools as the sandbox README lists. The skill never pushes; the publisher does that.
* **Portal.** Set `portal_enabled = true` (it listens on 127.0.0.1:8765 only), then on your PC run
  `ssh -N -L 8765:127.0.0.1:8765 root@<ip>` and open the `https://127.0.0.1:8765/?token=…` URL from
  `journalctl -u bw1-sandbox-portal`. Check the certificate fingerprint printed there. `--pants-down-mode` and
  non-localhost hosts are refused.
* **Autoresume** (`autoresume_enabled`) runs as a service. The portal is started with `--no-autoresume` so the two
  don't both resume workers.
* **Ghidra MCP** is off: there is no Ghidra on the droplet, `local/` is empty, and `ghidra-forward` is refused. Workers
  use objdiff output and the Mac symbols.

## Checking it works

```bash
ssh root@<ip> tail -n 40 /var/log/bw1-sandbox-setup.log              # provisioning, orig hash check
ssh root@<ip> sudo -iu sandbox bw1-sandbox check                      # firewall proven from a container
ssh root@<ip> /usr/local/sbin/bw1-egress show                         # the rules
ssh root@<ip> systemctl list-timers bw1-publisher.timer bw1-publisher-comments.timer bw1-egress.timer
ssh root@<ip> tail -n 50 /var/log/bw1-publisher/publisher.log
ssh root@<ip> sudo -u publisher python3 /opt/bw1-sandbox/droplet/publisher.py --config /etc/bw1-sandbox/publisher.env --dry-run
ssh root@<ip> sudo -u publisher python3 /opt/bw1-sandbox/droplet/publisher.py --config /etc/bw1-sandbox/publisher.env --dry-run comments
python3 droplet/tests/test_publisher.py -v                            # offline guard tests (any machine with git + bash)
```

`terraform output` lists the commands, the DO project link and the PR list link.

## Stopping and tearing down

* **Pause publishing:** `touch /etc/bw1-sandbox/STOP`. The publisher stops.
* **Pause workers:** `bw1-sandbox pause NAME`, or `stop NAME`.
* **Stop everything:** `terraform output stop_command`. It creates the stop file, disables the timers, autoresume
  and the portal, and stops all worker containers.
* **Revoke:** revoke the Claude token (claude.ai account settings) and both GitHub tokens
  (https://github.com/settings/personal-access-tokens).
* **Tear down:** `terraform\teardown.ps1`. It runs `terraform destroy` on the droplet, the firewall, the SSH key and
  the project assignment, after you type `destroy`. **The DigitalOcean project itself is left in place**, because
  Terraform only assigns to it (`do_project_id`) and never manages it. Pushed branches and PRs stay on GitHub.

## Costs (October 2026 list prices; check before applying)

| item | estimate |
|---|---|
| droplet `s-8vcpu-16gb` (default; about 3 workers at 2 CPUs / 4 GB) | about $96 per month (about $0.14 per hour) |
| `c-8` dedicated CPU / `s-16vcpu-32gb` (about 6 workers) | about $168 / $192 per month |
| Claude | your subscription; workers use its session and week limits, **shared with your own use**. No per-token bill |
| GitHub, DO project, firewall, bandwidth | none or negligible |

Billing is hourly: destroy the droplet when idle.

## Open questions for you

1. **bw1-decomp commit for workers.** The sandbox's `preamble.md` and orchestrator skill use `tools/idioms.py`,
   `tools/decomp-similar.py`, `tools/decomp-queue.py` and a `decomp-matching` skill. None of them is on
   `openblack/bw1-decomp` main (`c293511d`, 2026-10-08). Which branch has them? Set `bw1_decomp_ref` to it, or
   workers will be told to use tools that aren't there.
2. **Libraries missing from your zip.** It has no CRT, AMaths or DirectX `lib/` files, so workers can compile and
   diff single objects but can't run a full `ninja` link with the final hash check. Should those files go into the
   zip? Is it OK for the game files to sit on a droplet at all? They stay on the host and in the workers' read-only
   mount.
3. **Threat model.** Do you accept containers on a shared kernel next to the publisher and its GitHub token,
   compared with the sandbox's VM?
4. **PR target.** Draft PRs go to the fork until you set `pr_target = "openblack"`. Do openblack's maintainers want
   harness PRs directly? Should `feedback_to_sandbox` be on, and for which reviewers?
5. **Who spawns workers?** Nothing does automatically: the orchestrator (Remote Control), the portal, or you.
   Do you want a scheduled spawner?
6. **Size and limits.** `s-8vcpu-16gb` and about 3 workers, 3 PRs per day, 5 open. How much of your subscription's
   limits can the workers use?
7. **Not verified until the first apply:**
   * the sandbox's Docker-over-paramiko path against a local `decomp@127.0.0.1`;
   * `docker>=7` with the pinned sandbox;
   * the Ubuntu toolchain targets (`build/tools/wibo`, `llvm`, ICC);
   * that GitHub accepts the openblack-scoped token for a PR with head `raffclar:<branch>`, and whether openblack
     requires approving it;
   * whether `claude --remote-control` combines with `--mcp-config` / `--strict-mcp-config`.
