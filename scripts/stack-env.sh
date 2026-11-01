#!/bin/sh
# Print KEY=VALUE settings that give this checkout its own Docker Compose project, host
# ports and databases, so several git worktrees can run stacks and tests side by side.
#
# The main checkout keeps the historical defaults (project "quantlab", Postgres 5433,
# Redis 6380, API 8000, web 3000). A linked worktree gets a slot 1..89 derived from its
# path; override it with QUANTLAB_SLOT. Variables already set in the environment win.
#
#   eval "$(scripts/stack-env.sh)"     # use in a shell
#   make up                            # the Makefile does this automatically
set -eu

# Outside a git checkout (an exported tarball) fall back to the current directory, which
# gets the main checkout's defaults, instead of failing and leaving every port empty.
top=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
if [ -f "$top/.git" ]; then
  default_slot=$(( $(printf %s "$top" | cksum | cut -d' ' -f1) % 89 + 1 ))
else
  default_slot=0
fi
slot=${QUANTLAB_SLOT:-$default_slot}

if [ "$slot" -eq 0 ]; then
  project=quantlab pg=5433 redis=6380 api=8000 web=3000
  jaeger=16686 otlp=4318 prom=9090 grafana=3001
  restart=unless-stopped
else
  # A worktree's dev stack is not brought back when Docker restarts (#162): several would
  # start at once in the shared VM. Crash-recovery probes set unless-stopped explicitly.
  restart=no
  base=$(( 20000 + slot * 20 ))
  project="quantlab-$(basename "$top" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9\n' '-')"
  pg=$(( base + 3 )) redis=$(( base + 4 )) api=$(( base + 0 )) web=$(( base + 1 ))
  jaeger=$(( base + 6 )) otlp=$(( base + 7 )) prom=$(( base + 9 )) grafana=$(( base + 11 ))
fi

emit() { eval "current=\${$1:-}"; echo "$1=${current:-$2}"; }
emit QUANTLAB_SLOT "$slot"
emit COMPOSE_PROJECT_NAME "$project"
emit QUANTLAB_PG_PORT "$pg"
emit QUANTLAB_REDIS_PORT "$redis"
emit QUANTLAB_API_PORT "$api"
emit QUANTLAB_WEB_PORT "$web"
emit QUANTLAB_JAEGER_PORT "$jaeger"
emit QUANTLAB_OTLP_PORT "$otlp"
emit QUANTLAB_PROMETHEUS_PORT "$prom"
emit QUANTLAB_GRAFANA_PORT "$grafana"
emit QUANTLAB_RESTART_POLICY "$restart"
# URLs follow an overridden port (QUANTLAB_PG_PORT=... make up), not just the slot's.
pg=${QUANTLAB_PG_PORT:-$pg} redis=${QUANTLAB_REDIS_PORT:-$redis} web=${QUANTLAB_WEB_PORT:-$web}
emit QUANTLAB_DATABASE_URL "postgresql+psycopg://quantlab:quantlab@localhost:$pg/quantlab"
emit QUANTLAB_REDIS_URL "redis://:${QUANTLAB_REDIS_PASSWORD:-quantlab-dev}@localhost:$redis/0"
emit E2E_BASE_URL "http://localhost:$web"

# The default stack's minimum Docker VM memory (#175, #220, docs/RUNBOOK.md "Resources"): the
# sum of the memory limits (2 workers at 1536 MiB, the API at 1880 MiB) plus the measured
# peaks of the services without one, rounded up. A warning on stderr only, because stdout
# is eval'd. Nothing is changed. Silent when docker is unavailable or QUANTLAB_SKIP_VM_CHECK=1.
min_vm_mib=5120
if [ "${QUANTLAB_SKIP_VM_CHECK:-}" != 1 ]; then
  vm_bytes=$(docker info --format '{{.MemTotal}}' 2>/dev/null || true)
  case "$vm_bytes" in
    ''|*[!0-9]*) ;;
    *)
      vm_mib=$((vm_bytes / 1048576))
      if [ "$vm_mib" -lt "$min_vm_mib" ]; then
        echo "stack-env: warning: the Docker VM has ${vm_mib} MiB; the default stack (2 workers at 1536 MiB, the API at 1880 MiB) needs ${min_vm_mib} MiB (5 GiB). See docs/RUNBOOK.md, Resources (#175, #220)." >&2
      fi
      ;;
  esac
fi
