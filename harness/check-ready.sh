#!/usr/bin/env bash
# ExecStartPre of bw1-harness.service: refuse to start unless a Claude credential (subscription OAuth
# token or API key) and the GitHub token exist, the stop file is
# absent and the original game files are in place.
set -euo pipefail
# shellcheck source=config.env.example
source /etc/bw1-harness/config.env
secrets=/etc/bw1-harness/secrets.env

fail() { echo "bw1-harness not ready: $*" >&2; exit 1; }
[[ ! -e "$STOP_FILE" ]] || fail "stop file $STOP_FILE exists"
[[ -r "$secrets" ]] || fail "$secrets missing"
grep -Eq '^(CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_API_KEY)=.+' "$secrets" ||
	fail "no Claude credential (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY)"
grep -Eq '^GH_TOKEN=.+' "$secrets" || fail "GH_TOKEN is empty"
[[ -f "$REPO_DIR/orig/$VERSION/runblack-decrypted.exe" ]] || fail "orig/$VERSION/runblack-decrypted.exe missing"
command -v claude >/dev/null || fail "claude not installed"
echo "ready"
