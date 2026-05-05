# Operator hardening notes

This directory holds opt-in configuration for the human operator running
Claude Code locally against the ai-dlc repo. None of it is required to
build or deploy the platform — the deployed agents enforce their own
policy via SDK hooks (see `agents/implementer/src/implementer/hooks.py`)
and Strands hook providers.

## `claude-settings.json`

A JSON template intended to be merged by hand into your global
`~/.claude/settings.json`. It does three things:

1. Disables `enableAllProjectMcpServers` so a compromised `.mcp.json` in
   any cloned repo can't auto-attach an MCP server to your session.
2. Adds a deny list for read access to AWS/SSH/GPG/credential files and
   piped-to-shell `curl` / `wget` patterns.
3. Adds two `PreToolUse` Bash guards (compound-command-safe) for
   `rm -rf` and direct-push-to-main.

The patterns and rationale are adapted from
[trailofbits/claude-code-config](https://github.com/trailofbits/claude-code-config).
Apply selectively — merge with your existing settings rather than
overwriting them.
