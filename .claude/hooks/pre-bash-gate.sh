#!/usr/bin/env bash
# PreToolUse gate for Bash. Reads Claude Code's tool_input JSON from stdin,
# inspects the command, denies destructive operations with exit code 2 (deny
# + surface stderr to the agent). All other commands pass through (exit 0).
#
# Guardrails encoded here:
#   - never force-push to main
#   - never --no-verify a commit or push (skips hooks; symptoms outlive the workaround)
#   - never rm -rf home / system paths (sandbox paths under /tmp are fine)
#   - never git reset --hard main (destroys uncommitted work; use a soft reset)
#   - never git tag -f v1 without an accompanying gh release create later
#     (this one is soft: a WARNING, not a deny — the sequence is manual)

set -eu

input=$(cat)
cmd=$(echo "$input" | jq -r '.tool_input.command // empty' 2>/dev/null || echo "")

# No command payload (non-Bash tool or malformed input): allow.
[ -z "$cmd" ] && exit 0

deny() {
  echo "Denied by pre-bash-gate: $1" >&2
  exit 2
}

warn() {
  echo "Warning from pre-bash-gate: $1" >&2
}

# Force-push to main (feature-branch force-push is fine)
if echo "$cmd" | grep -qE 'git push[^|;&]*(--force|-f|\+)[^|;&]*(origin[[:space:]]+)?(refs/heads/)?main([[:space:]]|$)'; then
  deny "force-push to main. Use a feature branch."
fi

# --no-verify skips hooks — hides the underlying failure
if echo "$cmd" | grep -qE 'git[[:space:]]+(commit|push)[[:space:]].*(--no-verify)'; then
  deny "--no-verify skips hooks. Fix the underlying failure instead."
fi

# rm -rf on /, /home, $HOME, ~
if echo "$cmd" | grep -qE 'rm[[:space:]]+-rf?[[:space:]]+(/[^t]|/home|\$HOME|~)'; then
  deny "destructive rm -rf on a system path. Move to /tmp if this is intentional."
fi

# git reset --hard on main
if echo "$cmd" | grep -qE 'git[[:space:]]+reset[[:space:]]+--hard[[:space:]].*main'; then
  deny "git reset --hard on main. Consider a soft reset or a new branch."
fi

# Force-move v1 tag without a paired gh release create: WARN, don't block.
# (The release sequence is manual; this is a nudge, not a policy.)
if echo "$cmd" | grep -qE 'git[[:space:]]+tag[[:space:]]+-f[[:space:]]+v1'; then
  warn "moving v1. Remember: paired 'gh release create vX.Y.Z' is required for the release to show in the UI."
fi

exit 0
