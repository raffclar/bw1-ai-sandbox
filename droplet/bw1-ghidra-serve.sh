#!/bin/sh
# bw1-ghidra.service: GhidraMCP's HTTP API (GhidraMcpServer.java) on the docker0 gateway only, so worker
# containers reach it as ghidra.host and the public interface never sees it. Runs as bw1-ghidra.
set -eu
. /etc/bw1-sandbox/ghidra.conf
: "${GAME_VERSION:=BW1W120}"
[ -n "${GHIDRA_PORT:-}" ] || { echo "GHIDRA_PORT is empty: Ghidra MCP is off" >&2; exit 2; }
BIND="$(ip -4 -o addr show docker0 | awk '{print $4}' | cut -d/ -f1 | head -n1)"
[ -n "$BIND" ] || { echo "docker0 has no IPv4 address yet" >&2; exit 1; }
OPT=/opt/bw1-ghidra
VAR=/var/lib/bw1-ghidra
RO=""
[ "${GHIDRA_MODE:-rw}" = ro ] && RO=-readOnly
export JAVA_HOME="$OPT/jdk" PATH="$OPT/jdk/bin:/usr/bin:/bin" HOME="$VAR" GHIDRA_HEADLESS_MAXMEM=3G
# IPv4 sockets: an IPv4-mapped IPv6 listener would not match the unit's IPAddressAllow.
export GHIDRA_HEADLESS_JAVA_OPTIONS="-Djava.net.preferIPv4Stack=true"
exec "$OPT/ghidra/support/analyzeHeadless" "$VAR/projects/live" "$GAME_VERSION" \
	-process runblack-decrypted.exe -noanalysis $RO \
	-scriptPath "$OPT/scripts" -postScript GhidraMcpServer.java "$BIND" "$GHIDRA_PORT" "${GHIDRA_MODE:-rw}" \
	-log "$VAR/log/serve.log" -scriptlog "$VAR/log/serve-scripts.log"
