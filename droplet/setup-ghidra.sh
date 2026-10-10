#!/usr/bin/env bash
# Headless Ghidra for the workers' Ghidra MCP (the sandbox README's "Ghidra MCP", with Ghidra on the droplet
# instead of on your PC). Called by setup-droplet.sh; safe to run again on its own (as root):
#   GHIDRA_ENABLED=true GAME_VERSION=BW1W120 bash /opt/bw1-sandbox/droplet/setup-ghidra.sh
#
#   /opt/bw1-ghidra/<jdk>, /opt/bw1-ghidra/<ghidra>   pinned downloads, SHA-256 checked
#   /opt/bw1-ghidra/scripts                            ApplyBw1Symbols.java, GhidraMcpServer.java
#   /var/lib/bw1-ghidra                                user bw1-ghidra: input copies, project, logs
#   bw1-ghidra-analyse.service                         import + symbols.txt + auto-analysis (oneshot)
#   bw1-ghidra.service                                 GhidraMCP's HTTP API on <docker0 gateway>:GHIDRA_PORT
#   /etc/bw1-sandbox/ghidra.conf                       GHIDRA_PORT (read by bw1-egress) and GHIDRA_MODE
#   <sandbox>/local/                                   mcp.json, the pinned bridge_mcp_ghidra.py and its
#                                                      Python packages (pylib/) for workers
#
# Environment: GHIDRA_ENABLED (true|false) GHIDRA_MODE (rw|ro) GHIDRA_PORT GAME_VERSION
set -Eeuo pipefail

: "${GHIDRA_ENABLED:=true}" "${GHIDRA_MODE:=rw}" "${GHIDRA_PORT:=8080}" "${GAME_VERSION:=BW1W120}"

# Pinned versions. Ghidra 12.1 needs JDK 21. The bridge and GhidraMcpServer.java follow GhidraMCP 1.4.
JDK_URL="https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12.1%2B1/OpenJDK21U-jdk_x64_linux_hotspot_21.0.12.1_1.tar.gz"
JDK_SHA256=ce79869e1307ed8ee1e2baa86a412b1eb5b75d10a01006d788a6f968bcfaee94
JDK_DIR=jdk-21.0.12.1+1
GHIDRA_URL="https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.1.4_build/ghidra_12.1.4_PUBLIC_20260921.zip"
GHIDRA_SHA256=ddac49f903da9d5bac833e5cc79395098b9c33cfd3279be5f31bd00387d2d4db
GHIDRA_DIR=ghidra_12.1.4_PUBLIC
GHIDRAMCP_COMMIT=27f316f80139e2d5dec882519a1bdf4aa46ac04c # tag 1.4
BRIDGE_URL="https://raw.githubusercontent.com/LaurieWired/GhidraMCP/$GHIDRAMCP_COMMIT/bridge_mcp_ghidra.py"
BRIDGE_SHA256=88a9b20b67f4399e8bd6a7d769923b61c9d9d7160eb887db4f53187807cd2707

DROPLET=/opt/bw1-sandbox/droplet
SB=/opt/bw1-sandbox/bw1-ai-sandbox
OPT=/opt/bw1-ghidra
VAR=/var/lib/bw1-ghidra
CACHE=/var/cache/bw1-ghidra
CONF=/etc/bw1-sandbox/ghidra.conf

case "$GHIDRA_MODE" in rw | ro) ;; *) echo "GHIDRA_MODE must be rw or ro" >&2; exit 2 ;; esac
[[ "$GHIDRA_PORT" =~ ^[0-9]{2,5}$ ]] || { echo "GHIDRA_PORT must be a port number" >&2; exit 2; }

say() { printf '\n== %s ==\n' "$*"; }

if [[ "$GHIDRA_ENABLED" != true ]]; then
	say "ghidra: off"
	systemctl disable --now bw1-ghidra.service bw1-ghidra-analyse.service 2>/dev/null || true
	install -d -m 0755 /etc/bw1-sandbox
	printf 'GHIDRA_PORT=\nGHIDRA_MODE=%s\n' "$GHIDRA_MODE" >"$CONF"
	# Take back only what this script put into local/.
	if [[ -f "$SB/local/.bw1-ghidra" ]]; then
		rm -rf "$SB/local/mcp.json" "$SB/local/bridge_mcp_ghidra.py" "$SB/local/pylib" "$SB/local/.bw1-ghidra"
	fi
	exit 0
fi

say "ghidra: JDK and Ghidra (pinned, SHA-256 checked)"
apt-get install -y -q unzip >/dev/null
install -d -m 0755 "$OPT" "$CACHE"
fetch() { # url sha256 file
	if [[ ! -f "$3" ]] || ! echo "$2  $3" | sha256sum -c --quiet 2>/dev/null; then
		curl -fsSL --retry 3 -o "$3.part" "$1"
		echo "$2  $3.part" | sha256sum -c --quiet || { echo "ERROR: checksum mismatch for $1" >&2; rm -f "$3.part"; exit 5; }
		mv "$3.part" "$3"
	fi
}
if [[ ! -x "$OPT/$JDK_DIR/bin/java" ]]; then
	fetch "$JDK_URL" "$JDK_SHA256" "$CACHE/jdk.tar.gz"
	rm -rf "$OPT/$JDK_DIR.tmp" && mkdir "$OPT/$JDK_DIR.tmp"
	tar -xzf "$CACHE/jdk.tar.gz" -C "$OPT/$JDK_DIR.tmp" --strip-components=1 --no-same-owner
	mv "$OPT/$JDK_DIR.tmp" "$OPT/$JDK_DIR"
fi
if [[ ! -x "$OPT/$GHIDRA_DIR/support/analyzeHeadless" ]]; then
	fetch "$GHIDRA_URL" "$GHIDRA_SHA256" "$CACHE/ghidra.zip"
	rm -rf "$OPT/unpack.tmp" && mkdir "$OPT/unpack.tmp"
	unzip -q "$CACHE/ghidra.zip" -d "$OPT/unpack.tmp"
	mv "$OPT/unpack.tmp/$GHIDRA_DIR" "$OPT/$GHIDRA_DIR"
	rm -rf "$OPT/unpack.tmp"
fi
ln -sfn "$OPT/$JDK_DIR" "$OPT/jdk"
ln -sfn "$OPT/$GHIDRA_DIR" "$OPT/ghidra"
chown -R root:root "$OPT"
chmod -R go-w "$OPT"
rm -f "$CACHE/jdk.tar.gz" "$CACHE/ghidra.zip"

say "ghidra: user, scripts, services"
id bw1-ghidra >/dev/null 2>&1 || useradd --system --home-dir "$VAR" --shell /usr/sbin/nologin bw1-ghidra
install -d -o bw1-ghidra -g bw1-ghidra -m 0750 "$VAR" "$VAR/input" "$VAR/projects" "$VAR/log"
install -d -m 0755 "$OPT/scripts" "$OPT/bin"
install -m 0644 "$DROPLET/ghidra/ApplyBw1Symbols.java" "$DROPLET/ghidra/GhidraMcpServer.java" "$OPT/scripts/"
install -m 0755 "$DROPLET/bw1-ghidra-analyse.sh" /usr/local/sbin/bw1-ghidra-analyse
install -m 0755 "$DROPLET/bw1-ghidra-serve.sh" "$OPT/bin/bw1-ghidra-serve"
install -d -m 0755 /etc/bw1-sandbox
printf 'GHIDRA_PORT=%s\nGHIDRA_MODE=%s\nGAME_VERSION=%s\n' "$GHIDRA_PORT" "$GHIDRA_MODE" "$GAME_VERSION" >"$CONF"
chmod 0644 "$CONF"
install -m 0644 "$DROPLET/systemd/bw1-ghidra.service" "$DROPLET/systemd/bw1-ghidra-analyse.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable bw1-ghidra.service

say "ghidra: MCP config and bridge for workers ($SB/local)"
# The sandbox's documented spot for this (local/ is left to the user; sync copies it to /opt/mcp in workers).
install -d -o root -g root -m 0755 "$SB/local"
fetch "$BRIDGE_URL" "$BRIDGE_SHA256" "$CACHE/bridge_mcp_ghidra.py"
install -m 0644 "$CACHE/bridge_mcp_ghidra.py" "$SB/local/bridge_mcp_ghidra.py"
# The bridge needs mcp<2 (FastMCP), but the worker image's unpinned `pip install mcp` gets 2.x. So its packages
# are vendored into local/pylib (first on PYTHONPATH through mcp.json): hash-pinned wheels for the image's
# CPython 3.13 on x86_64, installed with the host venv's pip for that target. Reinstalled when the list changes.
REQS="$DROPLET/ghidra/bridge-requirements.txt"
if ! cmp -s "$REQS" "$SB/local/pylib/.requirements.txt"; then
	rm -rf "$SB/local/pylib.tmp"
	/opt/bw1-sandbox/venv/bin/pip install -q --target "$SB/local/pylib.tmp" --no-deps --require-hashes \
		--only-binary=:all: --implementation cp --python-version 3.13 --abi cp313 --abi abi3 --abi none \
		--platform manylinux_2_34_x86_64 --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64 \
		--platform manylinux2014_x86_64 --platform any -r "$REQS"
	install -m 0644 "$REQS" "$SB/local/pylib.tmp/.requirements.txt"
	rm -rf "$SB/local/pylib" && mv "$SB/local/pylib.tmp" "$SB/local/pylib"
	chown -R root:root "$SB/local/pylib"
	chmod -R a+rX,go-w "$SB/local/pylib"
fi
sed "s|ghidra.host:8080/|ghidra.host:$GHIDRA_PORT/|" "$DROPLET/ghidra/mcp.json" >"$SB/local/mcp.json"
chmod 0644 "$SB/local/mcp.json"
echo "installed by /opt/bw1-sandbox/droplet/setup-ghidra.sh" >"$SB/local/.bw1-ghidra"
# Once the worker image exists (not yet on a fresh droplet), check that the bridge imports with these packages.
if docker image inspect bw1-worker >/dev/null 2>&1; then
	if docker run --rm --network none --user 2000:2000 --cap-drop ALL --read-only -e PYTHONPATH=/opt/mcp/pylib \
		-e PYTHONDONTWRITEBYTECODE=1 -v "$SB/local:/opt/mcp:ro" --entrypoint python3 bw1-worker \
		-c "import mcp.server.fastmcp, requests" ; then
		echo "bridge imports in the worker image"
	else
		echo "WARNING: bridge_mcp_ghidra.py does not import in the worker image (Python changed?)" >&2
	fi
fi

say "ghidra: analysis"
# Re-analyses only when the executable, symbols.txt, the symbols script or Ghidra changed, in the background
# (it takes a while); the service starts when it is done. `bw1-ghidra-analyse --force` redoes it.
if bw1-ghidra-analyse --stale; then
	systemctl start --no-block bw1-ghidra-analyse.service
	echo "analysis started in the background: journalctl -fu bw1-ghidra-analyse"
else
	systemctl restart bw1-ghidra.service
	echo "project up to date; bw1-ghidra.service (re)started"
fi
