#!/bin/sh
# #253: run the reproduction and the backtest failure count on macOS, then in the Linux API
# image built from the current branch (OpenBLAS). Run under scripts/fullstack-lock.sh.
SP="$PWD/benchmarks/issue253"  # run from the repo root
echo "== macOS"
uv run python "$SP/repro253.py"
uv run python "$SP/ab253.py"
set -a
eval "$(QUANTLAB_SLOT=31 scripts/stack-env.sh)"
set +a
[ "$COMPOSE_PROJECT_NAME" = "quantlab-agent-3-data" ] || { echo "refusing: project $COMPOSE_PROJECT_NAME"; exit 2; }
docker compose -p "$COMPOSE_PROJECT_NAME" build api >/dev/null 2>&1 || { echo "build failed"; exit 3; }
echo "== Linux image"
docker run --rm -v "$SP:/scratch:ro" -e QUANTLAB_SKIP_VM_CHECK=1 "$COMPOSE_PROJECT_NAME-api" sh -c '
  cd /app && python /scratch/repro253.py && python /scratch/ab253.py'
