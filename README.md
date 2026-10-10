# bw1-sandbox droplet: bw1-ai-sandbox on DigitalOcean

This runs [bwrsandman/bw1-ai-sandbox](https://github.com/bwrsandman/bw1-ai-sandbox), the maintainer's harness for
[openblack/bw1-decomp](https://github.com/openblack/bw1-decomp), on a DigitalOcean droplet. The harness runs several
`claude -p --dangerously-skip-permissions` matching workers, each in a hardened Docker container. A host-side
**publisher** turns finished workers into **draft PRs on your fork** (`raffclar/bw1-decomp`), and never anywhere
else. Terraform builds the droplet and assigns it to the existing DigitalOcean project "for bw1-decomp".

**Nothing here is switched on.** You switch it on with `terraform/deploy.ps1`, which refuses to run without all of:

* a DigitalOcean token;
* your Claude subscription token, from `claude setup-token`;
* a GitHub token for the publisher;
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
                                         publisher                        GitHub token; pushes + opens draft PRs
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
| isolation boundary | KVM guest: workers share the **VM's** kernel | containers share the **droplet's** kernel with the host, the publisher (GitHub token), the bw1-build clone (game files) and the Claude token |
| escape surface | KVM | container escape (kernel, Docker, runc) gives the whole droplet |
| network filter | libvirt nwfilter, enforced by the host outside the VM | iptables on the same kernel; root on the droplet can change it. Workers have no root and no capabilities, but a container escape would bypass it |
| host files | unreachable, no shares | not mounted into workers; reachable only after an escape |
| GitHub credentials | on your PC only; nothing pushes | on the droplet: `GH_TOKEN` for the publisher (0600 `publisher`), and the bw1-build credential during the clone (root, deleted afterwards by default). Never in containers |
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
Three sources (`orig_source`):

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
* **`git`: the private `openblack/bw1-build` repo.** It is an alternative, no longer the default. On the host, root makes
  a shallow clone into `/var/lib/bw1-build` (0700) with LFS skipped. It then runs `git lfs pull --include
  "orig/<game_version>/**,orig/libs/**,orig/directx7.0/**"`, so only the version in use is fetched. The files are
  copied into the sandbox's clone, checked for leftover LFS pointers and verified the same way. The credential needs
  one of these:
  * **(a) recommended: a read-only deploy key.** Run `scripts/new-bw1-build-key.ps1`. It writes
    `keys/bw1-build-deploy` (gitignored) and prints the public key and
    `https://github.com/openblack/bw1-build/settings/keys/new`. Leave "Allow write access" unchecked. Adding one
    needs admin rights on bw1-build. The key goes to the droplet only (root, 0600), GitHub's host key is pinned, and
    the key is deleted after a verified clone unless `keep_bw1_build_key = true`.
  * **(b) a token.** Put `"github_bw1_build"` in the tokens file, with Contents: read on `openblack/bw1-build` only.
    A fine-grained PAT covers one owner, so your raffclar token can't read openblack's repo. deploy.ps1 refuses
    the token if it equals `"github"`.

  Either credential is kept apart from `GH_TOKEN` and never reaches the sandbox user or a worker.
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

## Publisher

On a timer (`publish_interval`, 30 min), as the `publisher` user:

1. **Export.** `bw1-sandbox-export` runs first, as `sandbox`. It is the sandbox's own `fetch` for finished workers,
   and it writes their state, final result and `sandbox/<name>` head to `/var/lib/bw1-sandbox/export/workers.json`.
2. **Pick.** It takes workers that are exited, not stopped on the usage limit, have new commits, and are not
   already published or rejected.
3. **Fetch.** It fetches each one from the sandbox clone into its own bare clone. Only objects and refs come across,
   so worker code is **never built or run** on the host.
4. **Refuse.** It rejects anything that touches `orig/`, `.gitattributes` or LFS pointers, or any change outside
   `allowed_paths` (`src/*`, `include/*`, `tools/matching_ledger.jsonl`). It also rejects changes over 40 files
   or 4000 lines.
5. **Push.** It pushes the worker's commits unchanged as a **new** branch, `ai-sandbox/<date>-<name>-<sha7>`, to
   `fork_repo` only. The pre-push hook refuses openblack URLs, any ref outside `ai-sandbox/`, updates (so force
   pushes), deletions and game files.
6. **Open a draft PR.** It opens one on `fork_repo`, base `main`, with the commits, files, the worker's final summary
   and a "not verified" note.
7. **Limits.** At most `max_prs_per_day` (3) per day. It waits while `max_open_prs` (5) are open. It refuses a
   `FORK_REPO` or `PR_REPO` that is `openblack/*`, or a `PR_REPO` that differs from `FORK_REPO`.

## Switching it on

1. **Tokens file** (`E:\api_tokens.json`; never in this repo). The format, with placeholders:

   ```json
   {
     "digitalocean": "dop_v1_REPLACE_ME",
     "claude_oauth": "sk-ant-oat01-REPLACE_ME",
     "github": "github_pat_REPLACE_ME",
     "github_bw1_build": "github_pat_REPLACE_ME_OPTIONAL"
   }
   ```

   * `digitalocean`: write access to droplets, SSH keys, firewalls and projects.
   * `claude_oauth`: run `claude setup-token` on your PC, logged in to your subscription, and paste the output here.
     The sandbox runs workers on this token (`sandbox.py token` stores exactly `CLAUDE_CODE_OAUTH_TOKEN=<token>`).
     deploy.ps1 writes the same file, because `sandbox.py token` only reads it interactively (`getpass`).
     An `anthropic` API key is not used.
   * `github`: a fine-grained PAT on `raffclar/bw1-decomp` with Contents and Pull requests read/write.
   * `github_bw1_build`: only for `orig_source = "git"`, option (b) above.
2. **Fork.** `raffclar/bw1-decomp` must exist. Keep its `main` in step with upstream yourself; the publisher never
   touches `main`.
3. **Game files.** By default `E:/bw1-original.zip` must exist; nothing else is needed. For `git`, run
   `scripts/new-bw1-build-key.ps1` and add the key on GitHub, or use (b).
4. **`terraform/terraform.tfvars`.** Copy it from `terraform.tfvars.example`. Your current `terraform.tfvars` still
   has settings of the old harness (`pr_draft`, `git_author_*` …). Terraform only warns about them, but remove them.
5. **Run:**

   ```powershell
   powershell -ExecutionPolicy Bypass -File E:\bw1-decomp\terraform\deploy.ps1           # -PlanOnly to stop before apply
   ```

   The script checks the secrets and the game files (the zip exists, or a bw1-build credential for `git`), refuses secrets in tfvars, and checks the Terraform
   version. It then runs `init` and `plan -out`, and prints a summary: resources, the DO project, droplet, sandbox
   commit, game files, workers, publisher and services. It applies only after you type `deploy`.

   The secrets reach Terraform only as `DIGITALOCEAN_TOKEN`, `TF_VAR_claude_oauth_token`, `TF_VAR_github_token` and
   `TF_VAR_bw1_build_token`. The saved plan holds them, so the script deletes it on exit. The state holds only a
   hash. A deploy key is read from its file by Terraform and is never printed in the plan.

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
ssh root@<ip> systemctl list-timers bw1-publisher.timer bw1-egress.timer
ssh root@<ip> tail -n 50 /var/log/bw1-publisher/publisher.log
ssh root@<ip> sudo -u publisher python3 /opt/bw1-sandbox/droplet/publisher.py --config /etc/bw1-sandbox/publisher.env --dry-run
```

`terraform output` lists the commands, the DO project link and the PR list link.

## Stopping and tearing down

* **Pause publishing:** `touch /etc/bw1-sandbox/STOP`. The publisher stops.
* **Pause workers:** `bw1-sandbox pause NAME`, or `stop NAME`.
* **Stop everything:** `terraform output stop_command`. It creates the stop file, disables the timers, autoresume
  and the portal, and stops all worker containers.
* **Revoke:** revoke the Claude token (claude.ai account settings), the GitHub token and the bw1-build deploy key or
  token on GitHub.
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
   zip? Alternatively, use `orig_source = "git"` (bw1-build has `orig/libs` and `directx7.0`), which needs a
   deploy key (admin on bw1-build) or a `github_bw1_build` token. Is it OK for the game files to sit on a droplet
   at all? They stay on the host and in the workers' read-only mount.
3. **Threat model.** Do you accept containers on a shared kernel next to the publisher and its GitHub token,
   compared with the sandbox's VM?
4. **Who spawns workers?** Nothing does automatically: the orchestrator (Remote Control), the portal, or you.
   Do you want a scheduled spawner?
5. **Size and limits.** `s-8vcpu-16gb` and about 3 workers, 3 PRs per day, 5 open. How much of your subscription's
   limits can the workers use?
6. **Not verified until the first apply:**
   * the sandbox's Docker-over-paramiko path against a local `decomp@127.0.0.1`;
   * `docker>=7` with the pinned sandbox;
   * the Ubuntu toolchain targets (`build/tools/wibo`, `llvm`, ICC);
   * `git lfs pull` over a deploy key;
   * whether `claude --remote-control` combines with `--mcp-config` / `--strict-mcp-config`.
