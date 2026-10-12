#!/usr/bin/env bash
# Provision the droplet to run bw1-ai-sandbox with the droplet itself standing in for the sandbox VM.
# Terraform uploads this folder to /opt/bw1-sandbox/droplet, the settings to /etc/bw1-sandbox and the
# two secrets, then runs this script as root. It is idempotent: re-running it is safe.
#
# Layout:
#   /opt/bw1-sandbox/bw1-ai-sandbox            the sandbox at a pinned commit (root-owned, read-only)
#   /opt/bw1-sandbox/bw1-ai-sandbox/bw1-decomp its project clone (sandbox user) + orig/ + host toolchain
#   /srv/decomp                                what the sandbox VM's cloud-init makes (mirror, workers, shared)
#   users: decomp   = the VM's user (docker group, sudo), reachable only by ssh from 127.0.0.1
#          sandbox  = runs sandbox.py, the portal, autoresume and the orchestrator; holds the Claude token
#          publisher= holds the GitHub token; pushes branches and opens PRs on the fork
#
# Environment (set by Terraform): SANDBOX_REPO SANDBOX_REF UPSTREAM_REPO BW1_DECOMP_REF FORK_REPO
#   REPO_SOURCE ORIG_SOURCE GAME_VERSION FEEDBACK_TO_SANDBOX WORKER_CPUS WORKER_MEMORY PUBLISH_INTERVAL PUBLISHER_ENABLED AUTORESUME_ENABLED
#   PORTAL_ENABLED ORCHESTRATOR_TOOLS CLAUDE_VERSION GHIDRA_ENABLED GHIDRA_MODE
set -Eeuo pipefail

: "${SANDBOX_REPO:=https://github.com/bwrsandman/bw1-ai-sandbox.git}"
: "${SANDBOX_REF:?pin the sandbox to a commit}"
: "${UPSTREAM_REPO:=openblack/bw1-decomp}" "${BW1_DECOMP_REF:=main}" "${FORK_REPO:?}"
: "${REPO_SOURCE:=local}" "${ORIG_SOURCE:=local}" "${GAME_VERSION:=BW1W120}" "${FEEDBACK_TO_SANDBOX:=false}" "${WORKER_CPUS:=2}" "${WORKER_MEMORY:=4g}" "${PUBLISH_INTERVAL:=30min}"
: "${PUBLISHER_ENABLED:=true}" "${AUTORESUME_ENABLED:=true}" "${PORTAL_ENABLED:=false}"
: "${ORCHESTRATOR_TOOLS:=true}" "${CLAUDE_VERSION:=stable}" "${GHIDRA_ENABLED:=true}" "${GHIDRA_MODE:=rw}"

case "$GAME_VERSION" in
BW1W100 | BW1W110) MSVC=6.4 ;;
BW1W120) MSVC=6.5 ;;
*) echo "GAME_VERSION $GAME_VERSION is not wired up (BW1W100, BW1W110, BW1W120)" >&2; exit 2 ;;
esac

if [[ "${FORK_REPO,,}" == openblack/* ]]; then
	echo "FORK_REPO must be your own fork, never openblack/*" >&2
	exit 2
fi

BASE=/opt/bw1-sandbox
DROPLET=$BASE/droplet
SB=$BASE/bw1-ai-sandbox
CLONE=$SB/bw1-decomp
VENV=$BASE/venv
SB_HOME=$BASE/home
STATE=$SB_HOME/.local/share/bw1-sandbox
PUB_HOME=/var/lib/bw1-publisher
PUB_CLONE=$PUB_HOME/bw1-decomp.git
ETC=/etc/bw1-sandbox

say() { printf '\n== %s ==\n' "$*"; }
as() { local u="$1"; shift; sudo -u "$u" -H "$@"; }

say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q docker.io git rsync python3 python3-venv python3-libvirt ninja-build openssh-server \
	iptables curl ca-certificates sudo iproute2 tmux
if ! command -v gh >/dev/null; then
	install -m 0755 -d /etc/apt/keyrings
	curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli.gpg
	chmod a+r /etc/apt/keyrings/githubcli.gpg
	echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
		>/etc/apt/sources.list.d/github-cli.list
	apt-get update -q
	apt-get install -y -q gh
fi

say "ssh: keys only"
# SSH is open to any address, so only key logins are allowed and failed attempts are banned for a while.
apt-get install -y -q fail2ban
cat >/etc/ssh/sshd_config.d/10-bw1-keys-only.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PermitEmptyPasswords no
MaxAuthTries 3
EOF
sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd
systemctl enable --now fail2ban

say "docker"
# No container-to-container traffic on docker0, no privilege gain by default, no IPv6.
install -d /etc/docker
cat >/etc/docker/daemon.json <<'JSON'
{ "icc": false, "no-new-privileges": true, "ipv6": false, "log-driver": "local" }
JSON
systemctl enable docker
systemctl restart docker

say "users"
getent group decomp >/dev/null || groupadd --gid 1000 decomp
id decomp >/dev/null 2>&1 || useradd --uid 1000 --gid decomp --groups docker --create-home --shell /bin/bash decomp
usermod -aG docker decomp
# As in the sandbox VM's cloud-init: decomp runs vm_agent.py and rsync through sudo.
echo "decomp ALL=(ALL) NOPASSWD:ALL" >/etc/sudoers.d/90-bw1-decomp
id sandbox >/dev/null 2>&1 || useradd --system --create-home --home-dir "$SB_HOME" --shell /bin/bash sandbox
id publisher >/dev/null 2>&1 || useradd --system --create-home --home-dir "$PUB_HOME" --shell /bin/bash publisher
usermod -aG sandbox publisher # read the sandbox clone and the export, nothing else
cat >/etc/sudoers.d/91-bw1-sandbox <<'SUDO'
sandbox ALL=(root) NOPASSWD: /usr/local/sbin/bw1-egress apply, /usr/local/sbin/bw1-egress check
SUDO
chmod 0440 /etc/sudoers.d/90-bw1-decomp /etc/sudoers.d/91-bw1-sandbox
visudo -cq

say "sandbox VM layout on the droplet (from the sandbox's cloud-init.yaml.in)"
install -d -o decomp -g decomp /srv/decomp /srv/decomp/workers /srv/decomp/bin /srv/decomp/image
install -d -o root -g root -m 0755 /srv/decomp/shared /srv/decomp/shared/tree /srv/decomp/shared/data /srv/decomp/shared/mcp
install -d -o root -g root -m 0700 /srv/decomp/secrets
if [[ ! -d /srv/decomp/mirror.git ]]; then
	as decomp git init -q --bare /srv/decomp/mirror.git
	as decomp git -C /srv/decomp/mirror.git config gc.auto 0
	as decomp git -C /srv/decomp/mirror.git config receive.autogc false
fi
as decomp git config --global --add safe.directory '*'

say "bw1-ai-sandbox at $SANDBOX_REF"
if [[ ! -d "$SB/.git" ]]; then
	git clone -q "$SANDBOX_REPO" "$SB"
fi
git -C "$SB" fetch -q origin
git -C "$SB" checkout -q --detach "$SANDBOX_REF"
# Root-owned and read-only for everyone else, except the project clone inside it.
find "$SB" -path "$CLONE" -prune -o -exec chown root:root {} +
find "$SB" -path "$CLONE" -prune -o -exec chmod go-w {} +
# The settings, with the worker hooks (droplet/hooks + quality_gate.py) packed into the worker setup.
python3 "$DROPLET/render_config.py" toml --template "$DROPLET/sandbox.droplet.toml.in" --out "$SB/sandbox.droplet.toml" \
	--set WORKER_CPUS="$WORKER_CPUS" --set WORKER_MEMORY="$WORKER_MEMORY" --set GAME_VERSION="$GAME_VERSION"
chmod 0644 "$SB/sandbox.droplet.toml"
# The workers' preamble: upstream's preamble.md (left untouched) with the harness rules inserted between markers
# before its "## Task" heading; regenerated on every run, so it is idempotent.
python3 "$DROPLET/render_config.py" preamble --upstream "$SB/preamble.md" --section "$DROPLET/preamble-droplet.md" \
	--out "$SB/preamble.droplet.md"
chmod 0644 "$SB/preamble.droplet.md"

say "python environment"
[[ -x "$VENV/bin/python3" ]] || python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q "docker>=7" paramiko cryptography
if [[ "$ORCHESTRATOR_TOOLS" == true ]]; then "$VENV/bin/pip" install -q mcp; fi
chmod 0755 "$DROPLET" "$DROPLET"/*.py "$DROPLET"/*.sh "$DROPLET/pre-push" "$DROPLET/hooks" "$DROPLET"/hooks/* \
	"$DROPLET/hospital" "$DROPLET/hospital/verify.sh"
chmod 0644 "$DROPLET"/hospital/*.md

say "bw1-decomp clone ($REPO_SOURCE)"
# The sandbox's host clone. Remotes: `upstream` = UPSTREAM_REPO (fetch; push DISABLED), `fork` = FORK_REPO
# (fetch; push DISABLED here too: the publisher pushes from its own clone, with its own credential).
install -d -o sandbox -g sandbox -m 0750 "$CLONE"
BUNDLE="$BASE/incoming/repo.bundle"
HEADFILE="$BASE/incoming/repo-head.txt"
if [[ "$REPO_SOURCE" == local ]]; then
	if [[ -f "$BUNDLE" ]]; then
		read -r HEAD_BRANCH HEAD_SHA <"$HEADFILE"
		[[ "$HEAD_SHA" =~ ^[0-9a-f]{40}$ && "$HEAD_BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] ||
			{ echo "ERROR: bad repo-head.txt" >&2; exit 2; }
		chmod 0644 "$BUNDLE"
		# git bundle verify must run inside a repository, so make the clone first.
		[[ -d "$CLONE/.git" ]] || as sandbox git init -q "$CLONE"
		as sandbox git -C "$CLONE" bundle verify -q "$BUNDLE"
		# Every branch and tag as-is, and the local clone's own remote-tracking refs under fork/.
		as sandbox git -C "$CLONE" fetch -q --update-head-ok --force "$BUNDLE" \
			'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*' 'refs/remotes/origin/*:refs/remotes/fork/*'
		if [[ "$HEAD_BRANCH" == HEAD ]]; then
			as sandbox git -C "$CLONE" checkout -q -f --detach "$HEAD_SHA"
		else
			as sandbox git -C "$CLONE" checkout -q -f "$HEAD_BRANCH"
			as sandbox git -C "$CLONE" reset -q --hard "$HEAD_SHA"
		fi
		rm -f "$BUNDLE" "$HEADFILE"
		echo "host clone at $HEAD_BRANCH @ ${HEAD_SHA:0:8} (from your local clone)"
	elif [[ ! -d "$CLONE/.git" ]]; then
		echo "ERROR: REPO_SOURCE=local but no uploaded bundle and no existing clone" >&2
		exit 2
	fi
else
	[[ -d "$CLONE/.git" ]] || as sandbox git clone -q --origin upstream "https://github.com/$UPSTREAM_REPO.git" "$CLONE"
fi
for r in upstream fork; do
	url="https://github.com/$UPSTREAM_REPO.git"
	[[ $r == fork ]] && url="https://github.com/$FORK_REPO.git"
	as sandbox git -C "$CLONE" remote get-url "$r" >/dev/null 2>&1 || as sandbox git -C "$CLONE" remote add "$r" "$url"
	as sandbox git -C "$CLONE" remote set-url "$r" "$url"
	as sandbox git -C "$CLONE" remote set-url --push "$r" DISABLED
done
as sandbox git -C "$CLONE" remote remove origin 2>/dev/null || true
as sandbox git -C "$CLONE" fetch -q upstream main
if [[ "$REPO_SOURCE" == upstream ]]; then
	as sandbox git -C "$CLONE" checkout -q "$BW1_DECOMP_REF"
fi

say "original game files ($ORIG_SOURCE, $GAME_VERSION)"
# The game files stay on the host: in the sandbox's clone (sandbox user, 0750) and the read-only copy
# `sync` makes for workers.
case "$ORIG_SOURCE" in
local | archive)
	# local: deploy.ps1's orig-local.tar.gz from your clone (top folder orig/). archive: bw1-original.zip (top
	# folder bw1-original/) or a pack-orig.ps1 .tar.gz (orig/). unpack-orig.py
	# refuses any other top folder, path traversal and links; it unpacks into a fresh folder first.
	ARCHIVE=""
	for a in "$BASE/incoming/orig-archive.zip" "$BASE/incoming/orig-archive.tar.gz"; do
		if [[ -f "$a" ]]; then ARCHIVE="$a"; fi
	done
	if [[ -n "$ARCHIVE" ]]; then
		STAGE="$(mktemp -d /var/lib/bw1-orig-stage.XXXXXX)"
		python3 "$DROPLET/unpack-orig.py" "$ARCHIVE" "$STAGE"
		as sandbox mkdir -p "$CLONE/orig"
		rsync -a "$STAGE/" "$CLONE/orig/"
		chown -R sandbox:sandbox "$CLONE/orig"
		rm -rf "$STAGE"
		UNPACKED_ARCHIVE="$ARCHIVE"
	fi
	;;
manual) echo "manual: copy the files to $CLONE/orig yourself, then re-run this script" ;;
*) echo "unknown ORIG_SOURCE $ORIG_SOURCE" >&2; exit 2 ;;
esac
if (cd "$CLONE" && grep " orig/" "config/$GAME_VERSION/build.sha1" | sha1sum -c --quiet); then
	echo "orig/$GAME_VERSION hashes OK"
else
	echo "ERROR: orig/$GAME_VERSION files missing or wrong (see README) - not continuing" >&2
	exit 3
fi
# Unpacked and verified: the uploaded archive is no longer needed.
if [[ -n "${UNPACKED_ARCHIVE:-}" ]]; then rm -f "$UNPACKED_ARCHIVE"; fi

say "host toolchain (sandbox README step 3)"
as sandbox bash -c "cd '$CLONE' && python3 configure.py --version $GAME_VERSION && ninja build/tools/dtk build/compilers/MSVC/$MSVC"
# sandbox.toml's worker setup also points at these; build them too where the targets exist.
for t in build/tools/objdiff-cli build/tools/wibo build/tools/llvm build/compilers/ICC/5.0.1; do
	as sandbox bash -c "cd '$CLONE' && ninja '$t'" || echo "WARNING: ninja $t failed; workers may need it"
done
# The compiler-header patch writes into build/compilers, which workers get read-only: patch it here, before `sync`
# copies the toolchain out (the worker setup in sandbox.droplet.toml checks it and marks the step done).
if as sandbox bash -c "cd '$CLONE' && ninja -t query pre-compile" >/dev/null 2>&1; then
	as sandbox bash -c "cd '$CLONE' && ninja pre-compile"
fi
# clang-format 22, which the project formats with (AGENTS.md, CI): the worker image's Debian package is older. Installed
# into the toolchain so `sync` hands it to workers; their setup puts build/tools/clang-format22/bin first on PATH.
CF="$CLONE/build/tools/clang-format22"
as sandbox "$VENV/bin/python3" -m pip install -q --upgrade --target "$CF" "clang-format>=22,<23"
CF_BIN=$(find "$CF" -path "*data/bin/clang-format" -type f | head -1)
[[ -n "$CF_BIN" ]] || { echo "ERROR: clang-format 22 binary not found under $CF" >&2; exit 3; }
as sandbox mkdir -p "$CF/bin"
as sandbox ln -sfn "../${CF_BIN#"$CF"/}" "$CF/bin/clang-format"

# bw1-decomp's Getting Started: libircmt.lib can be copied out of the downloaded ICC compiler.
ICC_LIB="$CLONE/build/compilers/ICC/5.0.1/lib/libircmt.lib"
if [[ ! -f "$CLONE/orig/libs/icc-5.0.115/libircmt.lib" && -f "$ICC_LIB" ]]; then
	as sandbox install -D -m 0644 "$ICC_LIB" "$CLONE/orig/libs/icc-5.0.115/libircmt.lib"
fi
# Report (not fatal) the libraries a full link needs that orig/ lacks. Per-object matching does not
# need them; `ninja` with the final link and hash check does (see README "Game files").
missing=()
for f in libs/msvc6.5/libcmt.lib libs/msvc6.5/libcpmt.lib libs/msvc6.5/msvcrtd.lib libs/msvc6.4/libcmt.lib \
	libs/msvc6.0/libcimt.lib libs/amaths-2.0/amaths.lib libs/icc-5.0.115/libircmt.lib directx7.0/lib/ddraw.lib; do
	[[ -f "$CLONE/orig/$f" ]] || missing+=("orig/$f")
done
if ((${#missing[@]})); then
	echo "WARNING: missing for a full link (per-object matching still works): ${missing[*]}"
fi

say "Claude token and ssh key for the sandbox user"
install -d -o sandbox -g sandbox -m 0700 "$SB_HOME/.local" "$SB_HOME/.local/share" "$STATE"
if [[ -s $ETC/claude-token.env ]]; then
	# Same file `sandbox.py token` writes: CLAUDE_CODE_OAUTH_TOKEN=<token from claude setup-token>, mode 0600.
	install -o sandbox -g sandbox -m 0600 "$ETC/claude-token.env" "$STATE/token.env"
	shred -u "$ETC/claude-token.env" 2>/dev/null || rm -f "$ETC/claude-token.env"
fi
grep -q '^CLAUDE_CODE_OAUTH_TOKEN=.' "$STATE/token.env" || { echo "ERROR: no Claude token" >&2; exit 4; }
[[ -f "$STATE/id_ed25519" ]] || as sandbox ssh-keygen -q -t ed25519 -N "" -C bw1-sandbox -f "$STATE/id_ed25519"
install -d -o decomp -g decomp -m 0700 /home/decomp/.ssh
# decomp is reachable only from this machine, only with the sandbox's key.
printf 'from="127.0.0.1,::1" %s\n' "$(cat "$STATE/id_ed25519.pub")" >/home/decomp/.ssh/authorized_keys
chown decomp:decomp /home/decomp/.ssh/authorized_keys
chmod 0600 /home/decomp/.ssh/authorized_keys
systemctl enable --now ssh

say "GitHub token for the publisher"
install -d -m 0755 "$ETC"
# Fork credential and the openblack PR-only credential: publisher only, never git's credential store,
# never the sandbox user or a worker.
for f in github.env github-openblack-pr.env; do
	if [[ -f $ETC/$f ]]; then
		chown publisher:publisher "$ETC/$f"
		chmod 0600 "$ETC/$f"
	fi
done
chown root:root "$ETC/publisher.env" "$ETC/egress.conf"
chmod 0644 "$ETC/publisher.env" "$ETC/egress.conf"

# Headless Ghidra + GhidraMCP's HTTP API for the workers (sandbox README "Ghidra MCP"); before the firewall,
# which reads its port, and before `sync`, which copies local/ (mcp.json + bridge) to workers.
GHIDRA_ENABLED="$GHIDRA_ENABLED" GHIDRA_MODE="$GHIDRA_MODE" GAME_VERSION="$GAME_VERSION" bash "$DROPLET/setup-ghidra.sh"

say "worker egress firewall"
install -m 0755 "$DROPLET/bw1-egress.sh" /usr/local/sbin/bw1-egress
install -m 0644 "$DROPLET/systemd/bw1-egress.service" "$DROPLET/systemd/bw1-egress.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now bw1-egress.service bw1-egress.timer
/usr/local/sbin/bw1-egress check

say "sandbox: create (image + lockdown), sync, check"
SBX=(env "SANDBOX_DIR=$SB" "$VENV/bin/python3" "$DROPLET/sandbox_droplet.py" sandbox)
if ! as sandbox "${SBX[@]}" check >/dev/null 2>&1; then
	as sandbox "${SBX[@]}" create
fi
as sandbox "${SBX[@]}" sync
as sandbox "${SBX[@]}" check

say "publisher clone"
install -d -o sandbox -g sandbox -m 0750 /var/lib/bw1-sandbox /var/lib/bw1-sandbox/export
install -d -o publisher -g publisher /var/log/bw1-publisher
if [[ ! -d "$PUB_CLONE" ]]; then
	as publisher git clone -q --bare "https://github.com/$UPSTREAM_REPO.git" "$PUB_CLONE"
fi
# origin = openblack, fetch only: its push URL is disabled (the publisher never pushes there).
as publisher git -C "$PUB_CLONE" remote set-url --push origin DISABLED
as publisher git -C "$PUB_CLONE" remote get-url fork >/dev/null 2>&1 ||
	as publisher git -C "$PUB_CLONE" remote add fork "https://github.com/$FORK_REPO.git"
as publisher git -C "$PUB_CLONE" remote set-url fork "https://github.com/$FORK_REPO.git"
as publisher git -C "$PUB_CLONE" fetch -q origin "+refs/heads/main:refs/remotes/origin/main"
install -m 0755 -o publisher -g publisher "$DROPLET/pre-push" "$PUB_CLONE/hooks/pre-push"
as publisher git config --global --replace-all safe.directory "$CLONE"
as publisher git config --global --add safe.directory "$CLONE/.git"
chmod -R g+rX "$CLONE/.git"

if [[ "$ORCHESTRATOR_TOOLS" == true ]]; then
	say "Claude Code for the orchestrator ($CLAUDE_VERSION)"
	if [[ ! -x "$SB_HOME/.local/bin/claude" ]]; then
		as sandbox bash -c "curl -fsSL https://claude.ai/install.sh | bash -s '$CLAUDE_VERSION'"
	fi
	install -m 0755 "$DROPLET/bw1-orchestrator.sh" /usr/local/bin/bw1-orchestrator
	# Host-side Claude compacts at 42% of its context window too (CLAUDE_AUTOCOMPACT_PCT_OVERRIDE; self-checked).
	as sandbox python3 "$DROPLET/render_config.py" claude-settings --file "$SB_HOME/.claude/settings.json"
fi
cat >/usr/local/bin/bw1-sandbox <<EOF
#!/bin/sh
# bw1-ai-sandbox's CLI on the droplet, e.g. bw1-sandbox status (run as the sandbox user).
exec env SANDBOX_DIR=$SB $VENV/bin/python3 $DROPLET/sandbox_droplet.py sandbox "\$@"
EOF
chmod 0755 /usr/local/bin/bw1-sandbox

say "hospital: cases, precedent log, integrity manifest"
# Cases are written by the sandbox user and read by the publisher (group sandbox). The folder is root's and sticky,
# so the sandbox user can add case folders but cannot replace the root-owned precedent log.
CASES=/var/lib/bw1-sandbox/cases
install -d -o root -g sandbox -m 1775 "$CASES"
if [[ ! -f "$CASES/precedents.md" ]]; then
	printf '# Precedents (the Chief'"'"'s decisions; they bind every case)\n\n' >"$CASES/precedents.md"
fi
chown root:root "$CASES/precedents.md"
chmod 0644 "$CASES/precedents.md"
# The Discharge container's caps: half the droplet's CPUs, and memory that leaves two 4g workers, the host and Ghidra
# theirs (8 vCPUs / 16 GB droplet). Change them here for another droplet size.
printf 'GAME_VERSION=%s\nDISCHARGE_CPUS=4\nDISCHARGE_MEMORY=5g\n' "$GAME_VERSION" >"$ETC/hospital.env"
chmod 0644 "$ETC/hospital.env"
cat >/usr/local/bin/bw1-hospital <<EOF
#!/bin/sh
# The hospital pipeline (droplet/hospital.py): as the sandbox user for cases, as root for the Chief's commands.
set -a; [ -r $ETC/hospital.env ] && . $ETC/hospital.env; set +a
exec env SANDBOX_DIR=$SB GAME_VERSION=$GAME_VERSION $VENV/bin/python3 $DROPLET/hospital.py "\$@"
EOF
chmod 0755 /usr/local/bin/bw1-hospital
# Tamper protection: sha256 of the gate, hospital.py, the hooks, the prompts and the publisher, which the publisher
# and the Discharge Nurse check before they act. Re-run this step after changing any of them.
python3 "$DROPLET/integrity.py" write --droplet "$DROPLET" --out "$ETC/integrity.json"
chown root:root "$ETC/integrity.json"
chmod 0644 "$ETC/integrity.json"
python3 "$DROPLET/integrity.py" check --manifest "$ETC/integrity.json"

say "services"
install -d -o publisher -g sandbox -m 2770 /var/lib/bw1-sandbox/feedback
for u in bw1-sandbox-autoresume.service bw1-sandbox-portal.service bw1-sandbox-export.service bw1-publisher.service \
	bw1-publisher-comments.service bw1-publisher-comments.timer bw1-sandbox-feedback.service bw1-sandbox-feedback.timer \
	bw1-hospital-charge.service bw1-hospital-charge.timer; do
	install -m 0644 "$DROPLET/systemd/$u" /etc/systemd/system/
done
sed "s|@PUBLISH_INTERVAL@|$PUBLISH_INTERVAL|" "$DROPLET/systemd/bw1-publisher.timer" >/etc/systemd/system/bw1-publisher.timer
systemctl daemon-reload
toggle() { if [[ "$2" == true ]]; then systemctl enable --now "$1"; else systemctl disable --now "$1" 2>/dev/null || true; fi; }
toggle bw1-sandbox-autoresume.service "$AUTORESUME_ENABLED"
toggle bw1-sandbox-portal.service "$PORTAL_ENABLED"
toggle bw1-publisher.timer "$PUBLISHER_ENABLED"
toggle bw1-publisher-comments.timer "$PUBLISHER_ENABLED"
# Off by default: reviewer comments become follow-up prompts for the worker that made the PR.
toggle bw1-sandbox-feedback.timer "$FEEDBACK_TO_SANDBOX"
# The hospital's charge timer is installed but never switched on here: the Chief starts it with
# `systemctl enable --now bw1-hospital-charge.timer` once the pipeline has been tried by hand.
say "done: no workers are running; spawn them with the orchestrator, the portal or bw1-sandbox spawn"
