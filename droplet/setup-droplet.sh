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
#   ORIG_SOURCE GAME_VERSION BW1_BUILD_REPO BW1_BUILD_REF KEEP_BW1_BUILD_KEY WORKER_CPUS WORKER_MEMORY PUBLISH_INTERVAL PUBLISHER_ENABLED AUTORESUME_ENABLED
#   PORTAL_ENABLED ORCHESTRATOR_TOOLS CLAUDE_VERSION
set -Eeuo pipefail

: "${SANDBOX_REPO:=https://github.com/bwrsandman/bw1-ai-sandbox.git}"
: "${SANDBOX_REF:?pin the sandbox to a commit}"
: "${UPSTREAM_REPO:=openblack/bw1-decomp}" "${BW1_DECOMP_REF:=main}" "${FORK_REPO:?}"
: "${ORIG_SOURCE:=archive}" "${GAME_VERSION:=BW1W120}" "${BW1_BUILD_REPO:=openblack/bw1-build}" "${BW1_BUILD_REF:=main}"
: "${KEEP_BW1_BUILD_KEY:=false}" "${WORKER_CPUS:=2}" "${WORKER_MEMORY:=4g}" "${PUBLISH_INTERVAL:=30min}"
: "${PUBLISHER_ENABLED:=true}" "${AUTORESUME_ENABLED:=true}" "${PORTAL_ENABLED:=false}"
: "${ORCHESTRATOR_TOOLS:=true}" "${CLAUDE_VERSION:=stable}"

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
apt-get install -y -q docker.io git git-lfs rsync python3 python3-venv python3-libvirt ninja-build openssh-server \
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
sed -e "s|@WORKER_CPUS@|$WORKER_CPUS|" -e "s|@WORKER_MEMORY@|$WORKER_MEMORY|" -e "s|@GAME_VERSION@|$GAME_VERSION|g" \
	"$DROPLET/sandbox.droplet.toml.in" \
	>"$SB/sandbox.droplet.toml"
chmod 0644 "$SB/sandbox.droplet.toml"

say "python environment"
[[ -x "$VENV/bin/python3" ]] || python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q "docker>=7" paramiko cryptography
if [[ "$ORCHESTRATOR_TOOLS" == true ]]; then "$VENV/bin/pip" install -q mcp; fi
chmod 0755 "$DROPLET" "$DROPLET"/*.py "$DROPLET"/*.sh "$DROPLET/pre-push"

say "bw1-decomp clone ($UPSTREAM_REPO @ $BW1_DECOMP_REF)"
install -d -o sandbox -g sandbox -m 0750 "$CLONE"
if [[ ! -d "$CLONE/.git" ]]; then
	as sandbox git clone -q "https://github.com/$UPSTREAM_REPO.git" "$CLONE"
fi
as sandbox git -C "$CLONE" fetch -q origin
as sandbox git -C "$CLONE" checkout -q "$BW1_DECOMP_REF"
as sandbox git -C "$CLONE" remote set-url --push origin DISABLED

say "original game files ($ORIG_SOURCE, $GAME_VERSION)"
# The game files stay on the host: in the sandbox's clone (sandbox user, 0750) and the read-only copy
# `sync` makes for workers. The bw1-build credential is used here only, by root, and never reaches
# GH_TOKEN, the sandbox user or a worker.
BW1_BUILD_DIR=/var/lib/bw1-build/bw1-build
BW1_BUILD_KEY=$ETC/bw1-build-deploy-key
BW1_BUILD_TOKEN_FILE=$ETC/bw1-build-token.env
GITHUB_KNOWN_HOSTS=/var/lib/bw1-build/known_hosts
fetch_bw1_build() {
	install -d -m 0700 /var/lib/bw1-build
	# GitHub's published SSH host key: no trust-on-first-use.
	echo "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" >"$GITHUB_KNOWN_HOSTS"
	local url include="orig/$GAME_VERSION/**,orig/libs/**,orig/directx7.0/**"
	local -a auth=()
	export GIT_LFS_SKIP_SMUDGE=1 # no LFS objects at clone time; only the version in use, below
	if [[ -s "$BW1_BUILD_KEY" ]]; then
		chmod 0600 "$BW1_BUILD_KEY"
		export GIT_SSH_COMMAND="ssh -i $BW1_BUILD_KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=$GITHUB_KNOWN_HOSTS -o StrictHostKeyChecking=yes"
		url="git@github.com:$BW1_BUILD_REPO.git"
	elif [[ -s "$BW1_BUILD_TOKEN_FILE" ]]; then
		# shellcheck disable=SC2016 # expanded by git's credential helper shell, from the environment
		auth=(-c credential.helper= -c 'credential.helper=!f() { echo username=x-access-token; echo "password=$BW1_BUILD_TOKEN"; }; f')
		BW1_BUILD_TOKEN="$(sed -n 's/^BW1_BUILD_TOKEN=//p' "$BW1_BUILD_TOKEN_FILE")"
		export BW1_BUILD_TOKEN
		url="https://github.com/$BW1_BUILD_REPO.git"
	else
		echo "ERROR: no bw1-build credential (deploy key or token) on the droplet" >&2
		return 1
	fi
	if [[ ! -d "$BW1_BUILD_DIR/.git" ]]; then
		git "${auth[@]}" clone -q --depth 1 --branch "$BW1_BUILD_REF" "$url" "$BW1_BUILD_DIR"
	else
		git "${auth[@]}" -C "$BW1_BUILD_DIR" fetch -q --depth 1 origin "$BW1_BUILD_REF"
		git -C "$BW1_BUILD_DIR" reset -q --hard FETCH_HEAD
	fi
	git -C "$BW1_BUILD_DIR" lfs install --local >/dev/null
	git "${auth[@]}" -C "$BW1_BUILD_DIR" lfs pull --include "$include"
	unset GIT_SSH_COMMAND BW1_BUILD_TOKEN GIT_LFS_SKIP_SMUDGE
	# Any file still an LFS pointer means the pull did not get it.
	if grep -rlI --include='*.exe' --include='*.dll' --include='*.lib' '^version https://git-lfs' \
		"$BW1_BUILD_DIR/orig/$GAME_VERSION" "$BW1_BUILD_DIR/orig/libs" "$BW1_BUILD_DIR/orig/directx7.0" | head -n 5 | grep .; then
		echo "ERROR: LFS pointers left after git lfs pull (above)" >&2
		return 1
	fi
	as sandbox mkdir -p "$CLONE/orig"
	rsync -a --delete "$BW1_BUILD_DIR/orig/$GAME_VERSION/" "$CLONE/orig/$GAME_VERSION/"
	rsync -a --delete "$BW1_BUILD_DIR/orig/libs/" "$CLONE/orig/libs/"
	rsync -a --delete "$BW1_BUILD_DIR/orig/directx7.0/" "$CLONE/orig/directx7.0/"
	chown -R sandbox:sandbox "$CLONE/orig"
}
case "$ORIG_SOURCE" in
git) fetch_bw1_build ;;
archive)
	# bw1-original.zip (top folder bw1-original/) or a pack-orig.ps1 .tar.gz (top folder orig/). unpack-orig.py
	# refuses any other top folder, path traversal and links; it unpacks into a fresh folder first.
	ARCHIVE=""
	for a in "$BASE/incoming/orig-archive.zip" "$BASE/incoming/orig-archive.tar.gz"; do [[ -f "$a" ]] && ARCHIVE="$a"; done
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
if [[ "$KEEP_BW1_BUILD_KEY" != true ]]; then
	# Verified: the credential is no longer needed on the droplet.
	for f in "$BW1_BUILD_KEY" "$BW1_BUILD_TOKEN_FILE"; do
		[[ -e "$f" ]] && { shred -u "$f" 2>/dev/null || rm -f "$f"; }
	done
fi
[[ -s "$BW1_BUILD_TOKEN_FILE" ]] || rm -f "$BW1_BUILD_TOKEN_FILE"
[[ -s "$BW1_BUILD_KEY" ]] || rm -f "$BW1_BUILD_KEY"

say "host toolchain (sandbox README step 3)"
as sandbox bash -c "cd '$CLONE' && python3 configure.py --version $GAME_VERSION && ninja build/tools/dtk build/compilers/MSVC/$MSVC"
# sandbox.toml's worker setup also points at these; build them too where the targets exist.
for t in build/tools/objdiff-cli build/tools/wibo build/tools/llvm build/compilers/ICC/5.0.1; do
	as sandbox bash -c "cd '$CLONE' && ninja '$t'" || echo "WARNING: ninja $t failed; workers may need it"
done

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
if [[ -f $ETC/github.env ]]; then
	chown publisher:publisher "$ETC/github.env"
	chmod 0600 "$ETC/github.env"
fi
chown root:root "$ETC/publisher.env" "$ETC/egress.conf"
chmod 0644 "$ETC/publisher.env" "$ETC/egress.conf"

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
fi
cat >/usr/local/bin/bw1-sandbox <<EOF
#!/bin/sh
# bw1-ai-sandbox's CLI on the droplet, e.g. bw1-sandbox status (run as the sandbox user).
exec env SANDBOX_DIR=$SB $VENV/bin/python3 $DROPLET/sandbox_droplet.py sandbox "\$@"
EOF
chmod 0755 /usr/local/bin/bw1-sandbox

say "services"
for u in bw1-sandbox-autoresume.service bw1-sandbox-portal.service bw1-sandbox-export.service bw1-publisher.service; do
	install -m 0644 "$DROPLET/systemd/$u" /etc/systemd/system/
done
sed "s|@PUBLISH_INTERVAL@|$PUBLISH_INTERVAL|" "$DROPLET/systemd/bw1-publisher.timer" >/etc/systemd/system/bw1-publisher.timer
systemctl daemon-reload
toggle() { if [[ "$2" == true ]]; then systemctl enable --now "$1"; else systemctl disable --now "$1" 2>/dev/null || true; fi; }
toggle bw1-sandbox-autoresume.service "$AUTORESUME_ENABLED"
toggle bw1-sandbox-portal.service "$PORTAL_ENABLED"
toggle bw1-publisher.timer "$PUBLISHER_ENABLED"
say "done: no workers are running; spawn them with the orchestrator, the portal or bw1-sandbox spawn"
