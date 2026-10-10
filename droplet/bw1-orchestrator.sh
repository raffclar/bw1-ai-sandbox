#!/usr/bin/env bash
# Start the sandbox's orchestrator on the droplet: Claude Code with Remote Control, in the sandbox
# folder (so its sandbox-orchestrator skill loads), with the droplet's MCP server instead of the
# repo's .mcp.json (which would start the unpatched mcp_server.py). Run it as the sandbox user inside
# tmux, after `claude` has been logged in once (/login). Drive it from claude.ai/code or the app.
set -euo pipefail
cd /opt/bw1-sandbox/bw1-ai-sandbox
export SANDBOX_CONFIG=/opt/bw1-sandbox/bw1-ai-sandbox/sandbox.droplet.toml
exec claude --remote-control "${1:-bw1-orchestrator}" \
	--mcp-config /opt/bw1-sandbox/droplet/mcp.droplet.json --strict-mcp-config
