#!/bin/sh
# Serialize full-stack runs (make app-up + Playwright) across every worktree of this
# repository. The Docker VM is shared: concurrent full stacks starve each other of CPU and
# memory (25 s registrations, OOM-killed APIs) and make E2E failures look like regressions.
#
#   scripts/fullstack-lock.sh run <command...>   # hold the lock while <command> runs
#   scripts/fullstack-lock.sh status             # who holds it, if anyone
#
# The lock lives in git's common directory, so all linked worktrees share it. A lock whose
# holder process is gone is reclaimed. QUANTLAB_LOCK_TIMEOUT (seconds, default 3600) bounds
# the wait.
set -eu

common=$(git rev-parse --git-common-dir)
lock="$(cd "$common" && pwd)/quantlab-fullstack.lock"

holder() { cat "$lock/owner" 2>/dev/null || echo "unknown holder"; }

case "${1:-}" in
  status)
    if [ -d "$lock" ]; then echo "held: $(holder)"; else echo "free"; fi
    ;;
  run)
    shift
    [ "$#" -gt 0 ] || { echo "usage: $0 run <command...>" >&2; exit 2; }
    timeout=${QUANTLAB_LOCK_TIMEOUT:-3600}
    waited=0
    until mkdir "$lock" 2>/dev/null; do
      pid=$(sed -n 's/^pid=\([0-9]*\).*/\1/p' "$lock/owner" 2>/dev/null || true)
      if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
        echo "fullstack-lock: reclaiming stale lock ($(holder))" >&2
        rm -rf "$lock"
        continue
      fi
      if [ "$waited" -ge "$timeout" ]; then
        echo "fullstack-lock: gave up after ${timeout}s; held by $(holder)" >&2
        exit 75
      fi
      [ $((waited % 60)) -eq 0 ] && echo "fullstack-lock: waiting; held by $(holder)" >&2
      sleep 5
      waited=$((waited + 5))
    done
    trap 'rm -rf "$lock"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    echo "pid=$$ worktree=$(git rev-parse --show-toplevel) since=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$lock/owner"
    "$@"
    ;;
  *)
    echo "usage: $0 {run <command...>|status}" >&2
    exit 2
    ;;
esac
