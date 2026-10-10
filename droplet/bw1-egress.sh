#!/usr/bin/env bash
# Worker egress control on the droplet: the stand-in for bw1-ai-sandbox's libvirt nwfilter.
#
# Worker containers run on Docker's default bridge (docker0). For traffic from docker0:
#   * forwarded (to the internet): only TCP 443 to the Anthropic API ranges is allowed (DOCKER-USER);
#     everything else is dropped, including DNS (workers resolve api.anthropic.com through --add-host),
#     the DigitalOcean metadata service, GitHub and the rest of the internet;
#   * to the droplet itself (sshd, the portal, anything listening): dropped (INPUT), except TCP GHIDRA_PORT on
#     the docker0 gateway when the Ghidra MCP is on (bw1-ghidra.service, /etc/bw1-sandbox/ghidra.conf);
#   * IPv6: dropped.
# Replies to allowed connections pass (conntrack). The host's own traffic is not affected here; the
# DigitalOcean cloud firewall limits it to the ports it needs.
#
# Usage (root): bw1-egress apply | check | remove | show
# Settings: /etc/bw1-sandbox/egress.conf (API_NETS="160.79.104.0/23", BRIDGE=docker0) and
# /etc/bw1-sandbox/ghidra.conf (GHIDRA_PORT=8080, empty or absent when the Ghidra MCP is off).
set -euo pipefail

API_NETS="160.79.104.0/23"
BRIDGE=docker0
GHIDRA_PORT=""
if [[ -r /etc/bw1-sandbox/egress.conf ]]; then
	# shellcheck disable=SC1091
	source /etc/bw1-sandbox/egress.conf
fi
if [[ -r /etc/bw1-sandbox/ghidra.conf ]]; then
	GHIDRA_PORT="$(sed -n 's/^GHIDRA_PORT=\([0-9]*\)$/\1/p' /etc/bw1-sandbox/ghidra.conf | tail -n1)"
fi
FWD=BW1-WORKERS-OUT
IN=BW1-WORKERS-IN
GUARD=BW1-GHIDRA-GUARD

bridge_ip() { ip -4 -o addr show "$BRIDGE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1; }

valid_net() { [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$ ]]; }

api_targets() {
	local n
	for n in $API_NETS; do valid_net "$n" && echo "$n"; done
	# Resolved addresses on top, as sandbox.py lockdown does.
	getent ahostsv4 api.anthropic.com 2>/dev/null | awk '{print $1}' | sort -u | while read -r n; do
		valid_net "$n" && echo "$n"
	done
	return 0
}

apply() {
	iptables -nL DOCKER-USER >/dev/null 2>&1 || { echo "DOCKER-USER chain missing: is docker running?" >&2; exit 1; }
	iptables -N "$FWD" 2>/dev/null || iptables -F "$FWD"
	iptables -A "$FWD" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
	local t
	while read -r t; do
		iptables -A "$FWD" -p tcp -d "$t" --dport 443 -j RETURN
	done < <(api_targets)
	iptables -A "$FWD" -m limit --limit 6/min -j LOG --log-prefix "bw1-worker-drop: "
	iptables -A "$FWD" -j DROP
	iptables -C DOCKER-USER -i "$BRIDGE" -j "$FWD" 2>/dev/null || iptables -I DOCKER-USER 1 -i "$BRIDGE" -j "$FWD"

	iptables -N "$IN" 2>/dev/null || iptables -F "$IN"
	iptables -A "$IN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
	local gw
	gw="$(bridge_ip)"
	if [[ -n "$GHIDRA_PORT" && -n "$gw" ]]; then
		# The one host port workers may open: Ghidra MCP on the gateway address (ghidra.host).
		iptables -A "$IN" -p tcp -d "$gw" --dport "$GHIDRA_PORT" -j RETURN
	fi
	iptables -A "$IN" -j DROP
	iptables -C INPUT -i "$BRIDGE" -j "$IN" 2>/dev/null || iptables -I INPUT 1 -i "$BRIDGE" -j "$IN"

	# The Ghidra port from anywhere but docker0 and loopback: dropped, whatever the service binds to. The
	# DigitalOcean cloud firewall refuses it too.
	iptables -N "$GUARD" 2>/dev/null || iptables -F "$GUARD"
	if [[ -n "$GHIDRA_PORT" ]]; then
		iptables -A "$GUARD" -i "$BRIDGE" -j RETURN
		iptables -A "$GUARD" -i lo -j RETURN
		iptables -A "$GUARD" -p tcp --dport "$GHIDRA_PORT" -j DROP
		iptables -C INPUT -j "$GUARD" 2>/dev/null || iptables -I INPUT 2 -j "$GUARD"
	else
		iptables -D INPUT -j "$GUARD" 2>/dev/null || true
	fi

	if command -v ip6tables >/dev/null; then
		ip6tables -C FORWARD -i "$BRIDGE" -j DROP 2>/dev/null || ip6tables -I FORWARD 1 -i "$BRIDGE" -j DROP
		ip6tables -C INPUT -i "$BRIDGE" -j DROP 2>/dev/null || ip6tables -I INPUT 1 -i "$BRIDGE" -j DROP
	fi
	echo "worker egress locked: tcp/443 to $(api_targets | tr '\n' ' ')only${GHIDRA_PORT:+; host $gw:$GHIDRA_PORT (Ghidra MCP)}"
}

check() {
	iptables -C DOCKER-USER -i "$BRIDGE" -j "$FWD" 2>/dev/null || { echo "DOCKER-USER jump missing" >&2; return 1; }
	iptables -C INPUT -i "$BRIDGE" -j "$IN" 2>/dev/null || { echo "INPUT jump missing" >&2; return 1; }
	iptables -C "$FWD" -j DROP 2>/dev/null || { echo "$FWD has no final DROP" >&2; return 1; }
	iptables -C "$IN" -j DROP 2>/dev/null || { echo "$IN has no final DROP" >&2; return 1; }
	if [[ -n "$GHIDRA_PORT" ]]; then
		iptables -C INPUT -j "$GUARD" 2>/dev/null || { echo "INPUT jump to $GUARD missing" >&2; return 1; }
		iptables -C "$GUARD" -p tcp --dport "$GHIDRA_PORT" -j DROP 2>/dev/null ||
			{ echo "$GUARD does not drop tcp/$GHIDRA_PORT" >&2; return 1; }
	fi
	echo "worker egress rules in place"
}

remove() {
	iptables -D DOCKER-USER -i "$BRIDGE" -j "$FWD" 2>/dev/null || true
	iptables -D INPUT -i "$BRIDGE" -j "$IN" 2>/dev/null || true
	iptables -F "$FWD" 2>/dev/null || true
	iptables -X "$FWD" 2>/dev/null || true
	iptables -F "$IN" 2>/dev/null || true
	iptables -X "$IN" 2>/dev/null || true
	iptables -D INPUT -j "$GUARD" 2>/dev/null || true
	iptables -F "$GUARD" 2>/dev/null || true
	iptables -X "$GUARD" 2>/dev/null || true
	echo "worker egress rules REMOVED: workers have open internet until the next apply"
}

case "${1:-}" in
apply) apply ;;
check) check ;;
remove) remove ;;
show) iptables -S "$FWD"; iptables -S "$IN"; iptables -S "$GUARD" 2>/dev/null || true; api_targets ;;
*) echo "usage: bw1-egress apply|check|remove|show" >&2; exit 64 ;;
esac
