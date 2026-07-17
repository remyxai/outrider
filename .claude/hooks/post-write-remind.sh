#!/usr/bin/env bash
# PostToolUse feedback for Edit/Write. Reads Claude Code's tool_result JSON
# from stdin, checks which file was touched, and surfaces context-specific
# reminders. Nothing enforced — just prompts so the agent doesn't forget.

set -eu

input=$(cat)
path=$(echo "$input" | jq -r '.tool_input.file_path // .tool_input.path // empty' 2>/dev/null || echo "")

[ -z "$path" ] && exit 0

case "$path" in
  */src/run.py)
    echo "Reminder: run 'python3 -m pytest tests/ -q' before committing changes to src/run.py." >&2
    ;;
  */action.yml)
    echo "Reminder: validate action.yml with 'python3 -c \"import yaml; yaml.safe_load(open(\\\"action.yml\\\"))\"'." >&2
    ;;
  */docs/backends.md|*/docs/customization.md)
    echo "Reminder: docs edit — check that every mentioned input/value maps to a real name in action.yml (avoid the pin-method-style doc-vs-code drift)." >&2
    ;;
esac

exit 0
