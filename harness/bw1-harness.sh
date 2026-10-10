#!/usr/bin/env bash
# One run of the bw1-decomp matching harness:
#   sync upstream -> build + baseline -> choose work -> run the agent -> verify ->
#   commit only improvements -> push a new namespaced branch to the fork -> open a PR.
#
# The systemd timer starts one run at a time. Every step is logged to $LOG_DIR/harness.log and
# each run keeps its prompt, agent transcript, reports and verification logs in $LOG_DIR/runs/<id>.
#
# Usage:
#   bw1-harness.sh [--config FILE] [--dry-run]
#
# --dry-run prints what the run would do. It reads the existing report.json and state if they
# exist, renders the prompt, and prints every command it would run, but it calls no API, runs
# no build, and changes nothing in the repository.

set -Eeuo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE=/etc/bw1-harness/config.env
SECRETS_FILE=/etc/bw1-harness/secrets.env
DRY_RUN=0

while [[ $# -gt 0 ]]; do
	case "$1" in
	--config) CONFIG_FILE="$2"; shift 2 ;;
	--secrets) SECRETS_FILE="$2"; shift 2 ;;
	--dry-run) DRY_RUN=1; shift ;;
	-h | --help) sed -n '2,16p' "$0"; exit 0 ;;
	*) echo "unknown argument: $1" >&2; exit 64 ;;
	esac
done

[[ -r "$CONFIG_FILE" ]] || { echo "config not readable: $CONFIG_FILE" >&2; exit 64; }
# shellcheck source=config.env.example
source "$CONFIG_FILE"

: "${REPO_DIR:?}" "${STATE_DIR:?}" "${LOG_DIR:?}" "${STOP_FILE:?}" "${VERSION:=BW1W120}"
: "${UPSTREAM_REPO:?}" "${FORK_REPO:?}" "${PR_REPO:=$FORK_REPO}" "${PR_BASE:=main}" "${PR_DRAFT:=true}"
[[ "${FORK_REPO,,}" == openblack/* ]] && { echo "FORK_REPO must be your own fork, never openblack/*" >&2; exit 2; }
: "${BRANCH_PREFIX:=harness/}" "${GIT_AUTHOR_NAME:?}" "${GIT_AUTHOR_EMAIL:?}"
: "${CLAUDE_MODEL:=claude-opus-5-5}" "${MAX_TURNS:=150}" "${MAX_USD_PER_RUN:=5}" "${RUN_TIMEOUT_MIN:=120}"
: "${CYCLES_PER_FUNCTION:=12}" "${MAX_PRS_PER_DAY:=3}" "${MAX_OPEN_PRS:=5}" "${MAX_USD_PER_DAY:=25}"
: "${MAX_RUNS_PER_DAY:=12}" "${MAX_FUNCTIONS:=4}" "${MIN_SIZE:=16}" "${MAX_SIZE:=600}"
: "${MIN_FUZZY:=50}" "${MAX_FUZZY:=99.5}" "${COOLDOWN_DAYS:=7}" "${MIN_FUZZY_GAIN:=0.5}"
: "${ALLOWED_PATHS:=src/*,include/*}" "${VENV_DIR:=/opt/bw1-harness/venv}"

if [[ -z "$BRANCH_PREFIX" || "$BRANCH_PREFIX" != */ || "$BRANCH_PREFIX" == main* || "$BRANCH_PREFIX" == master* ]]; then
	echo "BRANCH_PREFIX must be a namespace ending in '/', e.g. harness/" >&2
	exit 64
fi

RUN_ID="$(date -u +%Y%m%d-%H%M%S)-$$"
RUN_DIR="$LOG_DIR/runs/$RUN_ID"
LOG_FILE="$LOG_DIR/harness.log"
STATE_FILE="$STATE_DIR/state.json"
PY="$VENV_DIR/bin/python3"
[[ -x "$PY" ]] || PY="${PYTHON:-python3}" # PYTHON=python for dry runs on a dev machine
LIB=("$PY" "$HARNESS_DIR/harness_lib.py")
BUILD="build/$VERSION"

mkdir -p "$RUN_DIR" "$STATE_DIR"

log() { printf '%s [%s] %s\n' "$(date -u +%FT%TZ)" "$RUN_ID" "$*" | tee -a "$LOG_FILE" >&2; }
die() { log "ABORT: $*"; exit "${2:-1}"; }
stopped() { [[ -e "$STOP_FILE" ]]; }
check_stop() { if stopped; then log "stop file $STOP_FILE present: stopping"; exit 0; fi; }

# run CMD...: execute, or print it in a dry run. Output goes to the run log.
run() {
	if ((DRY_RUN)); then
		log "[dry-run] would run: $*"
		return 0
	fi
	log "+ $*"
	"$@"
}

on_error() { log "failed at line $1 (exit $2); see $RUN_DIR"; }
trap 'on_error $LINENO $?' ERR

if ((!DRY_RUN)); then
	# Keep 30 days of per-run folders.
	find "$LOG_DIR/runs" -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -rf {} + 2>/dev/null || true
fi
log "=== run start (dry_run=$DRY_RUN, version=$VERSION, model=$CLAUDE_MODEL) ==="
check_stop

# --- one run at a time ------------------------------------------------------------------------
if command -v flock >/dev/null 2>&1; then
	exec 9>"$STATE_DIR/harness.lock"
	flock -n 9 || die "another run holds $STATE_DIR/harness.lock" 0
elif ((DRY_RUN)); then
	log "[dry-run] flock not available here; a real run takes $STATE_DIR/harness.lock"
else
	die "flock (util-linux) is required"
fi

# --- secrets: both keys must exist, else refuse ----------------------------------------------
ANTHROPIC_API_KEY=""
GH_TOKEN=""
if [[ -r "$SECRETS_FILE" ]]; then
	while IFS='=' read -r key value; do
		case "$key" in
		ANTHROPIC_API_KEY) ANTHROPIC_API_KEY="$value" ;;
		GH_TOKEN) GH_TOKEN="$value" ;;
		esac
	done <"$SECRETS_FILE"
fi
if ((DRY_RUN)); then
	log "[dry-run] secrets: ANTHROPIC_API_KEY $([[ -n "$ANTHROPIC_API_KEY" ]] && echo present || echo MISSING), GH_TOKEN $([[ -n "$GH_TOKEN" ]] && echo present || echo MISSING) (a real run refuses unless both are present)"
else
	[[ -n "$ANTHROPIC_API_KEY" ]] || die "ANTHROPIC_API_KEY missing in $SECRETS_FILE: refusing to run" 2
	[[ -n "$GH_TOKEN" ]] || die "GH_TOKEN missing in $SECRETS_FILE: refusing to run" 2
fi

# gh and git get the token only through these wrappers; the agent never sees it.
gh_() { GH_TOKEN="$GH_TOKEN" GH_PROMPT_DISABLED=1 gh "$@"; }
git_() { git -C "$REPO_DIR" -c user.name="$GIT_AUTHOR_NAME" -c user.email="$GIT_AUTHOR_EMAIL" "$@"; }

# --- daily limits ----------------------------------------------------------------------------
LIMIT_ARGS=(--state "$STATE_FILE" --max-prs-per-day "$MAX_PRS_PER_DAY" --max-usd-per-day "$MAX_USD_PER_DAY" --max-runs-per-day "$MAX_RUNS_PER_DAY")
if ! limits="$("${LIB[@]}" limits check "${LIMIT_ARGS[@]}")"; then
	die "daily limit: $limits" 0
fi
log "limits ok: $limits"

# --- open harness PRs: cap them and skip their units -----------------------------------------
SKIP_UNITS=""
if ((DRY_RUN)); then
	log "[dry-run] would run: gh pr list --repo $PR_REPO --author @me --state open (cap $MAX_OPEN_PRS)"
else
	open_json="$(gh_ pr list --repo "$PR_REPO" --author "@me" --state open --limit 100 --json headRefName,title)"
	open_count="$(printf '%s' "$open_json" | "$PY" -c "import json,sys; p='$BRANCH_PREFIX'; print(sum(1 for x in json.load(sys.stdin) if x['headRefName'].startswith(p)))")"
	((open_count < MAX_OPEN_PRS)) || die "$open_count harness PRs open (cap $MAX_OPEN_PRS): waiting for review" 0
	SKIP_UNITS="$(printf '%s' "$open_json" | "$PY" -c "import json,sys,re; print(','.join(m.group(1) for x in json.load(sys.stdin) for m in [re.search(r'\[unit (\S+)\]', x['title'])] if m))")"
	log "open harness PRs: $open_count; units skipped: ${SKIP_UNITS:-none}"
fi
check_stop

# --- sync to upstream main and build the baseline --------------------------------------------
cd "$REPO_DIR" 2>/dev/null || { ((DRY_RUN)) && log "[dry-run] $REPO_DIR does not exist yet"; }
run git_ fetch --prune upstream main
run git_ checkout --force -B harness-work upstream/main
run git_ reset --hard upstream/main
run git_ clean -fd -e build/ -e orig/
if ((DRY_RUN)); then BASE_SHA="<upstream/main>"; else BASE_SHA="$(git_ rev-parse HEAD)"; fi
log "base commit $BASE_SHA"

run "$PY" configure.py --version "$VERSION"
run ninja >"$RUN_DIR/build-base.log" 2>&1 || die "upstream build failed; see $RUN_DIR/build-base.log"
run ninja baseline >"$RUN_DIR/baseline.log" 2>&1 || die "ninja baseline failed"
REPORT="$REPO_DIR/$BUILD/report.json"
BASELINE="$REPO_DIR/$BUILD/baseline.json"
if ((!DRY_RUN)); then cp "$BASELINE" "$RUN_DIR/baseline.json"; fi
check_stop

# --- choose work -----------------------------------------------------------------------------
PICK_REPORT="$BASELINE"
[[ -f "$PICK_REPORT" ]] || PICK_REPORT="$REPORT"
[[ -f "$PICK_REPORT" ]] || die "no report at $PICK_REPORT to choose work from" 0
if ! "${LIB[@]}" pick --report "$PICK_REPORT" --objdiff "$REPO_DIR/objdiff.json" --repo "$REPO_DIR" \
	--state "$STATE_FILE" --skip-units "$SKIP_UNITS" --max-functions "$MAX_FUNCTIONS" \
	--min-size "$MIN_SIZE" --max-size "$MAX_SIZE" --min-fuzzy "$MIN_FUZZY" --max-fuzzy "$MAX_FUZZY" \
	--cooldown-days "$COOLDOWN_DAYS" >"$RUN_DIR/work.json"; then
	die "nothing to work on: $(cat "$RUN_DIR/work.json")" 0
fi
UNIT="$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['unit'])" "$RUN_DIR/work.json")"
SOURCE="$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['source_path'])" "$RUN_DIR/work.json")"
mapfile -t FN_KEYS < <("$PY" -c "import json,sys; w=json.load(open(sys.argv[1])); [print(w['unit']+'::'+f['name']) for f in w['functions']]" "$RUN_DIR/work.json")
log "work: $UNIT ($SOURCE), ${#FN_KEYS[@]} function(s)"
if ((DRY_RUN)); then
	log "[dry-run] would record attempts for cooldown: ${FN_KEYS[*]}"
else
	"${LIB[@]}" limits attempt --state "$STATE_FILE" --keys "${FN_KEYS[@]}" >/dev/null
	"${LIB[@]}" limits run "${LIMIT_ARGS[@]}" >/dev/null
fi

"${LIB[@]}" render --work "$RUN_DIR/work.json" --template "$HARNESS_DIR/prompt.md.tmpl" \
	--summary "$RUN_DIR/agent-summary.md" --cycles "$CYCLES_PER_FUNCTION" >"$RUN_DIR/prompt.md"
log "prompt written to $RUN_DIR/prompt.md"
check_stop

# --- run the agent ---------------------------------------------------------------------------
# Deny rules hold even in bypassPermissions mode. The agent has no GitHub token in its
# environment, and the repo's pre-push hook rejects anything but new harness branches.
DISALLOWED="Bash(git push:*),Bash(git commit:*),Bash(git reset:*),Bash(git rebase:*),Bash(git checkout:*),Bash(git switch:*),Bash(git stash:*),Bash(git remote:*),Bash(git config:*),Bash(gh:*),Bash(curl:*),Bash(wget:*),Bash(ssh:*),Bash(scp:*),Bash(sudo:*),WebFetch,WebSearch"
CLAUDE_ARGS=(-p --model "$CLAUDE_MODEL" --max-turns "$MAX_TURNS" --output-format stream-json --verbose
	--permission-mode bypassPermissions --disallowedTools "$DISALLOWED")
if command -v claude >/dev/null 2>&1 && claude --help 2>/dev/null | grep -q -- '--max-budget-usd'; then
	CLAUDE_ARGS+=(--max-budget-usd "$MAX_USD_PER_RUN")
else
	log "WARNING: this claude has no --max-budget-usd; relying on --max-turns, the timeout and the daily cap"
fi

if ((DRY_RUN)); then
	log "[dry-run] would run (in $REPO_DIR, ANTHROPIC_API_KEY=<redacted>, GH_TOKEN unset): timeout ${RUN_TIMEOUT_MIN}m claude ${CLAUDE_ARGS[*]} < $RUN_DIR/prompt.md"
else
	log "agent start: model $CLAUDE_MODEL, max turns $MAX_TURNS, \$$MAX_USD_PER_RUN, ${RUN_TIMEOUT_MIN}m"
	set +e
	env -u GH_TOKEN -u GITHUB_TOKEN ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" DISABLE_AUTOUPDATER=1 \
		PATH="$VENV_DIR/bin:$PATH" \
		timeout --kill-after=60 "${RUN_TIMEOUT_MIN}m" claude "${CLAUDE_ARGS[@]}" <"$RUN_DIR/prompt.md" \
		>"$RUN_DIR/transcript.jsonl" 2>"$RUN_DIR/agent-stderr.log"
	agent_exit=$?
	set -e
	cost_json="$("${LIB[@]}" cost --transcript "$RUN_DIR/transcript.jsonl")"
	COST="$("$PY" -c "import json,sys; print(json.loads(sys.argv[1])['usd'])" "$cost_json")"
	"${LIB[@]}" limits spend "${LIMIT_ARGS[@]}" --amount "$COST" >/dev/null
	log "agent exit $agent_exit, $cost_json"
fi
COST="${COST:-0}"
check_stop

discard() {
	log "discarding changes: $*"
	run git_ reset --hard "$BASE_SHA"
	run git_ clean -fd -e build/ -e orig/
	log "=== run end (no PR) ==="
	exit 0
}

# --- verify ----------------------------------------------------------------------------------
if ((DRY_RUN)); then
	log "[dry-run] would check changed paths against: $ALLOWED_PATHS"
	log "[dry-run] would run: python3 tools/decomp-verify.py --changed --run --baseline $BUILD/baseline.json --clang-format $VENV_DIR/bin/clang-format"
	log "[dry-run] would run: python3 tools/decomp-regress.py --refresh --baseline $BUILD/baseline.json --fail-on-regression"
	log "[dry-run] would run: harness_lib.py compare --unit $UNIT (needs a new matched function or a +$MIN_FUZZY_GAIN% unit fuzzy gain, and nothing worse)"
else
	changed="$(git_ status --porcelain --untracked-files=all | cut -c4-)"
	[[ -n "$changed" ]] || discard "the agent changed nothing"
	printf '%s\n' "$changed" >"$RUN_DIR/changed-files.txt"
	if ! printf '%s\n' "$changed" | "${LIB[@]}" paths --allowed "$ALLOWED_PATHS" >"$RUN_DIR/paths.json"; then
		discard "files outside $ALLOWED_PATHS: $(cat "$RUN_DIR/paths.json")"
	fi
	"$PY" tools/decomp-verify.py --changed --run --baseline "$BUILD/baseline.json" \
		--clang-format "$VENV_DIR/bin/clang-format" --log-dir "$RUN_DIR/verify" >"$RUN_DIR/verify.log" 2>&1 ||
		discard "decomp-verify failed; see $RUN_DIR/verify.log"
	"$PY" tools/decomp-regress.py --refresh --baseline "$BUILD/baseline.json" --fail-on-regression \
		>"$RUN_DIR/regress.log" 2>&1 || discard "regressions; see $RUN_DIR/regress.log"
	cp "$REPORT" "$RUN_DIR/report.json"
	"${LIB[@]}" compare --baseline "$RUN_DIR/baseline.json" --report "$RUN_DIR/report.json" --unit "$UNIT" \
		--min-fuzzy-gain "$MIN_FUZZY_GAIN" >"$RUN_DIR/compare.json" || discard "no improvement: $(head -c 400 "$RUN_DIR/compare.json")"
	log "improvement: $(head -c 400 "$RUN_DIR/compare.json")"
fi
check_stop

# --- commit, push a new branch, open the PR ----------------------------------------------------
"${LIB[@]}" limits check "${LIMIT_ARGS[@]}" >/dev/null || discard "PR limit reached during the run"

SHORT="${UNIT##*/}"
SLUG="$(printf '%s' "$SHORT" | tr -c 'A-Za-z0-9_-' '-' | cut -c1-40)"
BRANCH="${BRANCH_PREFIX}$(date -u +%Y%m%d)-${SLUG}-${RUN_ID##*-}"
TITLE="$SHORT: Work on functions [unit $UNIT]"
case "$BRANCH" in
"$BRANCH_PREFIX"*) ;;
*) die "refusing branch $BRANCH outside $BRANCH_PREFIX" ;;
esac

if ((DRY_RUN)); then
	log "[dry-run] would commit '$SHORT: Work on functions' (author $GIT_AUTHOR_NAME <$GIT_AUTHOR_EMAIL>, Co-Authored-By trailer)"
	log "[dry-run] would push: git push fork HEAD:refs/heads/$BRANCH (new branch only, never forced)"
	log "[dry-run] would run: gh pr create --repo $PR_REPO --base $PR_BASE --head ${FORK_REPO%%/*}:$BRANCH --title '$TITLE' $([[ "$PR_DRAFT" == true ]] && echo --draft)"
	log "=== dry run end ==="
	exit 0
fi

"${LIB[@]}" prbody --compare "$RUN_DIR/compare.json" --summary "$RUN_DIR/agent-summary.md" \
	--run-id "$RUN_ID" --model "$CLAUDE_MODEL" --cost "$COST" >"$RUN_DIR/pr-body.md"

git_ add -A -- src include
git_ commit -q -F - <<EOF
$SHORT: Work on functions

Automated harness run $RUN_ID on $UNIT; see the pull request for the per-function results.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
log "committed $(git_ rev-parse --short HEAD)"

# The pre-push hook (installed by setup-droplet.sh) refuses anything but creating a new
# branch under $BRANCH_PREFIX, so this can never overwrite or force anything.
gh_ auth setup-git >/dev/null 2>&1 || true
GH_TOKEN="$GH_TOKEN" git_ -c credential.helper= -c "credential.helper=!gh auth git-credential" \
	push fork "HEAD:refs/heads/$BRANCH" >"$RUN_DIR/push.log" 2>&1 || die "push failed; see $RUN_DIR/push.log"
log "pushed $BRANCH to $FORK_REPO"

PR_ARGS=(--repo "$PR_REPO" --base "$PR_BASE" --head "${FORK_REPO%%/*}:$BRANCH" --title "$TITLE" --body-file "$RUN_DIR/pr-body.md")
[[ "$PR_DRAFT" == true ]] && PR_ARGS+=(--draft)
pr_url="$(gh_ pr create "${PR_ARGS[@]}")" || die "gh pr create failed"
"${LIB[@]}" limits pr "${LIMIT_ARGS[@]}" >/dev/null
log "opened $pr_url"

git_ checkout -q --force -B harness-work "$BASE_SHA"
log "=== run end (PR $pr_url) ==="
