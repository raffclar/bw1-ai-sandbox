#!/usr/bin/env bash
# Worker egress control on the droplet: the stand-in for bw1-ai-sandbox's libvirt nwfilter.
#
# Worker containers run on Docker's default bridge (docker0). For traffic from docker0:
#   * forwarded (to the internet): only TCP 443 to the Anthropic API ranges is allowed (DOCKER-USER);
#     everything else is dropped, including DNS (workers resolve api.anthropic.com through --add-host),
#     the DigitalOcean metadata service, GitHub and the rest of the internet;
#   * to the droplet itself (sshd, the portal, anything listening): dropped (INPUT);
#   * IPv6: dropped.
# Replies to allowed connections pass (conntrack). The host's own traffic is not affected here; the
# DigitalOcean cloud firewall limits it to the ports it needs.
#
# Usage (root): bw1-egress apply | check | remove | show
# Settings: /etc/bw1-sandbox/egress.conf (API_NETS="160.79.104.0/23", BRIDGE=docker0).
set -euo pipefail

API_NETS="160.79.104.0/23"
BRIDGE=docker0
if [[ -r /etc/bw1-sandbox/egress.conf ]]; then
	# shellcheck disable=SC1091
	source /etc/bw1-sandbox/egress.conf
fi
FWD=BW1-WORKERS-OUT
IN=BW1-WORKERS-IN

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
	iptables -A "$IN" -j DROP
	iptables -C INPUT -i "$BRIDGE" -j "$IN" 2>/dev/null || iptables -I INPUT 1 -i "$BRIDGE" -j "$IN"

	if command -v ip6tables >/dev/null; then
		ip6tables -C FORWARD -i "$BRIDGE" -j DROP 2>/dev/null || ip6tables -I FORWARD 1 -i "$BRIDGE" -j DROP
		ip6tables -C INPUT -i "$BRIDGE" -j DROP 2>/dev/null || ip6tables -I INPUT 1 -i "$BRIDGE" -j DROP
	fi
	echo "worker egress locked: tcp/443 to $(api_targets | tr '\n' ' ')only"
}

check() {
	iptables -C DOCKER-USER -i "$BRIDGE" -j "$FWD" 2>/dev/null || { echo "DOCKER-USER jump missing" >&2; return 1; }
	iptables -C INPUT -i "$BRIDGE" -j "$IN" 2>/dev/null || { echo "INPUT jump missing" >&2; return 1; }
	iptables -C "$FWD" -j DROP 2>/dev/null || { echo "$FWD has no final DROP" >&2; return 1; }
	iptables -C "$IN" -j DROP 2>/dev/null || { echo "$IN has no final DROP" >&2; return 1; }
	echo "worker egress rules in place"
}

remove() {
	iptables -D DOCKER-USER -i "$BRIDGE" -j "$FWD" 2>/dev/null || true
	iptables -D INPUT -i "$BRIDGE" -j "$IN" 2>/dev/null || true
	iptables -F "$FWD" 2>/dev/null || true
	iptables -X "$FWD" 2>/dev/null || true
	iptables -F "$IN" 2>/dev/null || true
	iptables -X "$IN" 2>/dev/null || true
	echo "worker egress rules REMOVED: workers have open internet until the next apply"
}

case "${1:-}" in
apply) apply ;;
check) check ;;
remove) remove ;;
show) iptables -S "$FWD"; iptables -S "$IN"; api_targets ;;
*) echo "usage: bw1-egress apply|check|remove|show" >&2; exit 64 ;;
esac
