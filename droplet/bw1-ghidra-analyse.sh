#!/usr/bin/env bash
# (Re)build the droplet's Ghidra project for the workers' Ghidra MCP (root):
#   bw1-ghidra-analyse            analyse if the inputs changed, then restart bw1-ghidra.service
#   bw1-ghidra-analyse --force    analyse even if nothing changed
#   bw1-ghidra-analyse --stale    exit 0 if a new analysis is needed, 1 if not (changes nothing)
#
# Inputs are copied out of the sandbox's host clone (the checked-out HEAD): orig/<version>/runblack-decrypted.exe
# and config/<version>/symbols.txt. The new project is built next to the live one, which keeps serving until
# the analysis is done; then the service is stopped, the projects swapped and the service started again.
# Edits agents made through MCP live in the old project and are dropped with it.
set -Eeuo pipefail

# shellcheck disable=SC1091
source /etc/bw1-sandbox/ghidra.conf
: "${GAME_VERSION:=BW1W120}"
CLONE=/opt/bw1-sandbox/bw1-ai-sandbox/bw1-decomp
OPT=/opt/bw1-ghidra
VAR=/var/lib/bw1-ghidra
EXE="$CLONE/orig/$GAME_VERSION/runblack-decrypted.exe"
SYMS="$CLONE/config/$GAME_VERSION/symbols.txt"
STAMP="$VAR/projects/live/inputs.sha256"

inputs_hash() {
	{
		sha256sum "$EXE" "$SYMS" "$OPT/scripts/ApplyBw1Symbols.java" | awk '{print $1}'
		readlink -f "$OPT/ghidra"
	} | sha256sum | awk '{print $1}'
}

mode="${1:-}"
case "$mode" in "" | --force | --stale) ;; *) echo "usage: bw1-ghidra-analyse [--force|--stale]" >&2; exit 64 ;; esac
[[ -f "$EXE" && -f "$SYMS" ]] || { echo "missing $EXE or $SYMS" >&2; exit 2; }
want="$(inputs_hash)"
have="$(cat "$STAMP" 2>/dev/null || true)"
if [[ "$mode" == --stale ]]; then [[ "$want" != "$have" || ! -d "$VAR/projects/live/$GAME_VERSION.rep" ]]; exit; fi
if [[ "$mode" != --force && "$want" == "$have" && -d "$VAR/projects/live/$GAME_VERSION.rep" ]]; then
	echo "project is up to date ($GAME_VERSION)"
	systemctl start bw1-ghidra.service
	exit 0
fi

as_ghidra() { runuser -u bw1-ghidra -- "$@"; }
install -o bw1-ghidra -g bw1-ghidra -m 0640 "$EXE" "$VAR/input/runblack-decrypted.exe"
install -o bw1-ghidra -g bw1-ghidra -m 0640 "$SYMS" "$VAR/input/symbols.txt"
NEW="$VAR/projects/new"
rm -rf "$NEW"
as_ghidra mkdir -p "$NEW"
echo "analysing $GAME_VERSION into $NEW (this takes a while)"
# Nice, and a few cores less than the droplet has, so running workers keep their CPUs.
cpus=$(($(nproc) > 4 ? $(nproc) - 2 : 2))
as_ghidra env JAVA_HOME="$OPT/jdk" PATH="$OPT/jdk/bin:/usr/bin:/bin" HOME="$VAR" GHIDRA_HEADLESS_MAXMEM=4G \
	nice -n 10 "$OPT/ghidra/support/analyzeHeadless" "$NEW" "$GAME_VERSION" \
	-import "$VAR/input/runblack-decrypted.exe" -overwrite -max-cpu "$cpus" \
	-scriptPath "$OPT/scripts" -preScript ApplyBw1Symbols.java "$VAR/input/symbols.txt" \
	-log "$VAR/log/analyse.log" -scriptlog "$VAR/log/analyse-scripts.log"
grep -h "ApplyBw1Symbols:" "$VAR/log/analyse-scripts.log" | tail -n 3 || true
[[ -d "$NEW/$GAME_VERSION.rep" ]] || { echo "analysis produced no project" >&2; exit 1; }
echo "$want" | as_ghidra tee "$NEW/inputs.sha256" >/dev/null

systemctl stop bw1-ghidra.service
rm -rf "$VAR/projects/old"
if [[ -d "$VAR/projects/live" ]]; then mv "$VAR/projects/live" "$VAR/projects/old"; fi
mv "$NEW" "$VAR/projects/live"
rm -rf "$VAR/projects/old"
systemctl start bw1-ghidra.service
echo "analysis done; bw1-ghidra.service started"
