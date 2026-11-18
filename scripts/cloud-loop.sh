#!/usr/bin/env bash
# GitHub Actions entry point: run `check` every minute for up to LOOP_MINUTES, saving state.json
# to the `state` branch when alerts/failures change (and every 15 checks as a heartbeat).
# The workflow restarts every 30 minutes and cancels the previous run, so checks are near continuous.
set -u
LOOP_MINUTES=${LOOP_MINUTES:-350}
end=$(( $(date +%s) + LOOP_MINUTES * 60 ))
git config user.name "ticketwatch-bot"
git config user.email "ticketwatch-bot@users.noreply.github.com"

save_state() {
  local blob tree parent commit
  blob=$(git hash-object -w state.json) || return
  tree=$(printf '100644 blob %s\tstate.json\n' "$blob" | git mktree)
  parent=$(git rev-parse -q --verify refs/remotes/origin/state || true)
  commit=$(git commit-tree "$tree" ${parent:+-p "$parent"} -m "state $(date -u +%FT%TZ)")
  if git push -q origin "$commit:refs/heads/state"; then
    git update-ref refs/remotes/origin/state "$commit"
  else
    git fetch -q origin state || true   # someone else moved it; retry next time
  fi
}

last="" n=0
while [ "$(date +%s)" -lt "$end" ]; do
  start=$(date +%s)
  python ticketwatch.py check || echo "check crashed (exit $?)"
  n=$((n + 1))
  if [ -f state.json ]; then
    sig=$(python -c "import json;s=json.load(open('state.json'));print(json.dumps([s.get('alerted'),s.get('failures')],sort_keys=True))" 2>/dev/null)
    if [ "$sig" != "$last" ] || [ $((n % 15)) -eq 0 ]; then save_state; last=$sig; fi
  fi
  left=$(( 60 - ($(date +%s) - start) ))
  sleep $(( left > 5 ? left : 5 ))
done
