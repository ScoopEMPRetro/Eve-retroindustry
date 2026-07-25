#!/usr/bin/env bash
# SessionStart hook: warns when the local checkout is behind the remote —
# both the main repo (application) and the private notes (CLAUDE.md via symlink).
# Prevents editing a stale state / desync between machines.
# Nothing sensitive is committed — the notes URL is resolved at runtime from the symlink.
set -uo pipefail
proj="${CLAUDE_PROJECT_DIR:-$(pwd)}"
msgs=()

check() {  # $1 = repo directory, $2 = label
  local d="$1" label="$2" up behind
  [ -d "$d/.git" ] || return 0
  git -C "$d" fetch --quiet 2>/dev/null || return 0
  up=$(git -C "$d" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null) || return 0
  behind=$(git -C "$d" rev-list --count "HEAD..$up" 2>/dev/null) || return 0
  if [ -n "$behind" ] && [ "$behind" -gt 0 ]; then
    msgs+=("$label: $behind commit(s) behind remote — before editing run: git -C \"$d\" pull")
  fi
}

check "$proj" "Main repo (application)"

# Private notes: CLAUDE.md in the root is a symlink into the notes repo.
cl="$proj/CLAUDE.md"
if [ -L "$cl" ]; then
  notes=$(dirname "$(readlink -f "$cl" 2>/dev/null)")
  [ -n "$notes" ] && check "$notes" "Notes (CLAUDE.md)"
fi

if [ "${#msgs[@]}" -eq 0 ]; then
  jq -n '{suppressOutput: true}'
else
  text="⚠️ Repository freshness:"$'\n'"$(printf '%s\n' "${msgs[@]}")"
  jq -n --arg t "$text" \
    '{systemMessage: $t, hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $t}}'
fi
