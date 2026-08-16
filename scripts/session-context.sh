#!/usr/bin/env bash
# Printed into every new Kiro session, so a fresh tab starts out knowing what the other
# tabs left behind.
#
# Several short-lived sessions share this one working tree, and a working tree has exactly
# one git index and one HEAD. That makes uncommitted work the shared hazard: a tab that
# runs `git add -A` commits somebody else's half-finished change under its own message,
# and two tabs editing one file means the second silently overwrites the first. Neither
# failure announces itself, so the fix is to make the state visible up front rather than
# discovered at commit time.
set -uo pipefail

root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$root" || exit 0

echo "## Shared repo state (session start)"
echo
echo "Branch: $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
echo
echo "Recent commits:"
git log --oneline -3 2>/dev/null | sed 's/^/  /'
echo

dirty=$(git status --porcelain 2>/dev/null)
if [ -z "$dirty" ]; then
  echo "Working tree is clean — no other session has work in flight."
  exit 0
fi

tracked=$(printf '%s\n' "$dirty" | grep -vc '^??' || true)
echo "UNCOMMITTED WORK ($tracked tracked) — treat as another session's, not yours:"
printf '%s\n' "$dirty" | grep -v '^??' | sed 's/^/  /' | head -25
untracked=$(printf '%s\n' "$dirty" | grep -c '^??' || true)
if [ "$untracked" -gt 0 ]; then
  echo "  (+ $untracked untracked path(s))"
fi
echo
echo "Rules for this tree:"
echo "  1. Stage files BY NAME. Never 'git add -A' or 'git add .' — it commits the work above."
echo "  2. Don't edit a file listed above unless you know it's yours. Wait for it to land."
echo "  3. Commit your own work before this tab closes, or it's orphaned for the next one."
echo "  4. Check 'git log' before assuming a file is unchanged since you last read it."
