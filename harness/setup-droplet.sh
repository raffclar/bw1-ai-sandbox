#!/usr/bin/env bash
# Provision the harness droplet. Terraform uploads the harness to /opt/bw1-harness/harness,
# the settings to /etc/bw1-harness/config.env and the keys to /etc/bw1-harness/secrets.env,
# then runs this script as root. It is idempotent: re-running it after a change is safe.
#
# Environment (set by Terraform):
#   ORIG_SOURCE       archive | ghcr | manual
#   GHCR_IMAGE        private image holding /orig (only for ORIG_SOURCE=ghcr)
#   CLAUDE_VERSION    Claude Code version for the native installer (stable, latest or x.y.z)
#   CLANG_FORMAT_SPEC pip requirement for clang-format (the repo's CI uses clang-format 22)
#   HARNESS_ENABLED   true: enable and start the timer; false: install it disabled
#   RUN_INTERVAL      systemd time span between runs, e.g. 2h
set -Eeuo pipefail

: "${ORIG_SOURCE:=archive}" "${GHCR_IMAGE:=ghcr.io/openblack/bw1-build:main}"
: "${CLAUDE_VERSION:=stable}" "${CLANG_FORMAT_SPEC:=clang-format>=22,<23}"
: "${HARNESS_ENABLED:=false}" "${RUN_INTERVAL:=2h}"

HOME_DIR=/opt/bw1-harness
HARNESS_DIR=$HOME_DIR/harness
INCOMING=$HOME_DIR/incoming
# shellcheck source=config.env.example
source /etc/bw1-harness/config.env

say() { printf '\n== %s ==\n' "$*"; }
as_harness() { sudo -u harness -H "$@"; }

say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git python3 python3-venv python3-pip ninja-build curl ca-certificates unzip \
	jq sudo util-linux coreutils
if ! command -v gh >/dev/null; then
	install -m 0755 -d /etc/apt/keyrings
	curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli.gpg
	chmod a+r /etc/apt/keyrings/githubcli.gpg
	echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
		>/etc/apt/sources.list.d/github-cli.list
	apt-get update -q
	apt-get install -y -q gh
fi

say "user and directories"
id harness >/dev/null 2>&1 || useradd --system --create-home --home-dir "$HOME_DIR/home" --shell /bin/bash harness
install -d -o harness -g harness "$HOME_DIR" "$INCOMING" "$STATE_DIR" "$LOG_DIR" "$(dirname "$REPO_DIR")"
chown -R root:root "$HARNESS_DIR"
chmod 0755 "$HARNESS_DIR" "$HARNESS_DIR"/*.sh "$HARNESS_DIR"/git-hooks/*
# Secrets: readable by root and the harness user only.
chown root:harness /etc/bw1-harness /etc/bw1-harness/secrets.env /etc/bw1-harness/config.env
chmod 0750 /etc/bw1-harness
chmod 0640 /etc/bw1-harness/secrets.env /etc/bw1-harness/config.env

say "python tools"
[[ -x "$VENV_DIR/bin/python3" ]] || python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install -q --upgrade pip
"$VENV_DIR/bin/pip" install -q pefile pyyaml certifi ninja "$CLANG_FORMAT_SPEC"

say "Claude Code ($CLAUDE_VERSION)"
if ! as_harness bash -lc 'command -v claude' >/dev/null 2>&1; then
	as_harness bash -c "curl -fsSL https://claude.ai/install.sh | bash -s '$CLAUDE_VERSION'"
fi
ln -sf "$HOME_DIR/home/.local/bin/claude" /usr/local/bin/claude

say "repository"
if [[ ! -d "$REPO_DIR/.git" ]]; then
	as_harness git clone --origin upstream "https://github.com/$UPSTREAM_REPO.git" "$REPO_DIR"
fi
as_harness git -C "$REPO_DIR" remote get-url fork >/dev/null 2>&1 ||
	as_harness git -C "$REPO_DIR" remote add fork "https://github.com/$FORK_REPO.git"
as_harness git -C "$REPO_DIR" remote set-url fork "https://github.com/$FORK_REPO.git"
# Never push to upstream from here.
as_harness git -C "$REPO_DIR" remote set-url --push upstream DISABLED
install -m 0755 -o harness -g harness "$HARNESS_DIR/git-hooks/pre-push" "$REPO_DIR/.git/hooks/pre-push"

say "original game files ($ORIG_SOURCE)"
case "$ORIG_SOURCE" in
archive)
	if [[ -f "$INCOMING/orig.tar.gz" ]]; then
		as_harness mkdir -p "$REPO_DIR/orig"
		as_harness tar -xzf "$INCOMING/orig.tar.gz" -C "$REPO_DIR"
		rm -f "$INCOMING/orig.tar.gz"
	fi
	;;
ghcr)
	# Needs a token that can read the private package (read:packages).
	if ! command -v docker >/dev/null; then apt-get install -y -q docker.io; fi
	GH_TOKEN="$(sed -n 's/^GH_TOKEN=//p' /etc/bw1-harness/secrets.env)"
	gh_user="$(GH_TOKEN="$GH_TOKEN" gh api user --jq .login)"
	printf '%s' "$GH_TOKEN" | docker login ghcr.io -u "$gh_user" --password-stdin
	docker pull "$GHCR_IMAGE"
	cid="$(docker create "$GHCR_IMAGE")"
	rm -rf "$INCOMING/orig" && docker cp "$cid:/orig" "$INCOMING/orig" && docker rm "$cid" >/dev/null
	docker logout ghcr.io
	mkdir -p "$REPO_DIR/orig" && cp -a "$INCOMING/orig/." "$REPO_DIR/orig/" && rm -rf "$INCOMING/orig"
	chown -R harness:harness "$REPO_DIR/orig"
	;;
manual) echo "copy the files to $REPO_DIR/orig yourself (see README)" ;;
*) echo "unknown ORIG_SOURCE $ORIG_SOURCE" >&2; exit 1 ;;
esac
if [[ -f "$REPO_DIR/config/$VERSION/build.sha1" ]]; then
	(cd "$REPO_DIR" && grep " orig/" "config/$VERSION/build.sha1" | sha1sum -c --quiet) &&
		echo "orig/$VERSION hashes OK" || echo "WARNING: orig/$VERSION files missing or wrong; runs will refuse to start"
fi

say "systemd"
sed "s|@RUN_INTERVAL@|$RUN_INTERVAL|" "$HARNESS_DIR/systemd/bw1-harness.timer" >/etc/systemd/system/bw1-harness.timer
install -m 0644 "$HARNESS_DIR/systemd/bw1-harness.service" /etc/systemd/system/bw1-harness.service
install -m 0644 "$HARNESS_DIR/systemd/bw1-harness.logrotate" /etc/logrotate.d/bw1-harness
ln -sf "$HARNESS_DIR/bw1-harness.sh" /usr/local/bin/bw1-harness
systemctl daemon-reload
if [[ "$HARNESS_ENABLED" == true ]]; then
	systemctl enable --now bw1-harness.timer
	echo "harness timer ENABLED (every $RUN_INTERVAL); first run 10 minutes after boot/enable"
else
	systemctl disable --now bw1-harness.timer 2>/dev/null || true
	echo "harness timer installed DISABLED (harness_enabled = false)"
fi
say "done"
