#!/usr/bin/env bash
# PreToolUse Bash gate for the SPAWNED coding agent (not the dev-time hook in
# .claude/hooks/, which only governs Claude Code sessions working ON this repo).
#
# Wired via `claude --settings <json>` in src/run.py so it actually loads in the
# agent's headless session. Verified: PreToolUse hooks fire in headless `-p` mode
# even under `--dangerously-skip-permissions` (exit 2 = deny + surface stderr).
#
# Threat model: indirect prompt injection from untrusted GitHub issue/PR text
# (arXiv:2607.20759). Attack success is capability-shaped — supply-chain installs
# 96.6%, config/persistence high. This gate removes the high-leverage Bash
# capabilities so a compliant-but-injected agent cannot reach them. It does NOT
# try to detect malicious intent (the paper shows that fails); it removes reach.
#
# Scope + known gaps (by design, covered elsewhere):
#   - Only governs the Bash tool. File writes to manifests/CI/hooks via Edit/Write
#     are caught by the post-session diff gate, not here.
#   - Cannot parse network calls hidden inside `python -c`/scripts the agent writes;
#     that is the runner-egress-allowlist follow-up.
set -eu

input=$(cat)
cmd=$(echo "$input" | jq -r '.tool_input.command // empty' 2>/dev/null || echo "")

# No command payload (non-Bash tool or malformed input): allow.
[ -z "$cmd" ] && exit 0

deny() {
  echo "Denied by agent-bash-gate (injection hardening): $1" >&2
  exit 2
}

# ── Package installs (supply-chain: 96.6% attack success) ───────────────────
if echo "$cmd" | grep -qE '(^|[|;&[:space:]])(pip[0-9]*|pip3|uv|poetry|conda|mamba|npm|pnpm|yarn|apt|apt-get|brew|cargo|gem)([[:space:]]+(pip[[:space:]]+)?)(install|add)([[:space:]]|$)'; then
  deny "package install. Repo deps are pre-installed; new deps must be reviewed as a diff, not installed in-session."
fi
if echo "$cmd" | grep -qE 'python[0-9.]*[[:space:]]+-m[[:space:]]+pip[[:space:]]+install'; then
  deny "python -m pip install. New deps must be reviewed as a diff, not installed in-session."
fi

# ── Network egress / fetch (exfil + remote payload fetch) ───────────────────
if echo "$cmd" | grep -qE '(^|[|;&[:space:]])(curl|wget|nc|ncat|netcat|telnet|scp|sftp|ssh)([[:space:]]|$)'; then
  deny "network command. Outbound fetch/exfil is not permitted in the coding session."
fi
if echo "$cmd" | grep -qE '/dev/(tcp|udp)/'; then
  deny "raw /dev/tcp network access."
fi

# ── GitHub write ops (the orchestrator publishes; the agent must not) ───────
if echo "$cmd" | grep -qE '(^|[|;&[:space:]])gh[[:space:]]+(pr|issue|release)[[:space:]]+(create|edit|merge|close|comment|delete)'; then
  deny "gh write op. The orchestrator opens PRs/Issues after the session; the agent must not."
fi
if echo "$cmd" | grep -qE '(^|[|;&[:space:]])gh[[:space:]]+api[[:space:]].*(-X[[:space:]]*(POST|PATCH|PUT|DELETE)|--method[[:space:]]*(POST|PATCH|PUT|DELETE))'; then
  deny "gh api write (mutating method). Read-only gh api is allowed."
fi

# ── git push (orchestrator pushes post-session) ─────────────────────────────
if echo "$cmd" | grep -qE '(^|[|;&[:space:]])git[[:space:]]+push([[:space:]]|$)'; then
  deny "git push. The orchestrator handles pushing after validation."
fi

# ── Inherited destructive-op guards (from the dev-time pre-bash-gate) ───────
if echo "$cmd" | grep -qE 'git[[:space:]]+(commit|push)[[:space:]].*(--no-verify)'; then
  deny "--no-verify skips hooks. Fix the underlying failure instead."
fi
if echo "$cmd" | grep -qE 'rm[[:space:]]+-rf?[[:space:]]+(/[^t]|/home|\$HOME|~)'; then
  deny "destructive rm -rf on a system path."
fi
if echo "$cmd" | grep -qE 'git[[:space:]]+reset[[:space:]]+--hard[[:space:]].*main'; then
  deny "git reset --hard on main."
fi

exit 0
