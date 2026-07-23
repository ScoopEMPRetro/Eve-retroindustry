#!/usr/bin/env bash
# SessionStart hook: upozorní, když je lokální checkout pozadu za remote —
# hlavní repo (aplikace) i privátní poznámky (CLAUDE.md přes symlink).
# Zabraňuje editaci zastaralého stavu / desynchronizaci mezi stroji.
# Nic citlivého se necommituje — URL poznámek se zjišťuje za běhu ze symlinku.
set -uo pipefail
proj="${CLAUDE_PROJECT_DIR:-$(pwd)}"
msgs=()

check() {  # $1 = adresář repa, $2 = popisek
  local d="$1" label="$2" up behind
  [ -d "$d/.git" ] || return 0
  git -C "$d" fetch --quiet 2>/dev/null || return 0
  up=$(git -C "$d" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null) || return 0
  behind=$(git -C "$d" rev-list --count "HEAD..$up" 2>/dev/null) || return 0
  if [ -n "$behind" ] && [ "$behind" -gt 0 ]; then
    msgs+=("$label: $behind commit(s) za remote — před úpravami spusť: git -C \"$d\" pull")
  fi
}

check "$proj" "Hlavní repo (aplikace)"

# Privátní poznámky: CLAUDE.md v rootu je symlink do notes repa.
cl="$proj/CLAUDE.md"
if [ -L "$cl" ]; then
  notes=$(dirname "$(readlink -f "$cl" 2>/dev/null)")
  [ -n "$notes" ] && check "$notes" "Poznámky (CLAUDE.md)"
fi

if [ "${#msgs[@]}" -eq 0 ]; then
  jq -n '{suppressOutput: true}'
else
  text="⚠️ Aktuálnost repozitářů:"$'\n'"$(printf '%s\n' "${msgs[@]}")"
  jq -n --arg t "$text" \
    '{systemMessage: $t, hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $t}}'
fi
