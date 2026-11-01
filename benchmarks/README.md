# Benchmarks

`run_all.py` (library and storage) and `load_and_scaling.py` (HTTP load, worker scaling)
write JSON results to `results/`. The measurement rules live in `harness.py`.

Against a worktree's own stack, point the scripts at its ports (`scripts/stack-env.sh`):
`QUANTLAB_BENCH_PG` and `QUANTLAB_REDIS_URL` for `run_all.py`, and `QUANTLAB_BENCH_API`
for `load_and_scaling.py`. For `scaling`, export the stack's environment too, because it
runs `docker compose` against the compose project.

## What every result records
- **Variance.** Each timed quantity is measured `--repeats` times (default 5; 3 for load
  levels) and reported as median and IQR, with the raw samples kept. Setup steps timed once
  are suffixed `_single`. Retrieval also keeps the median of each repeat
  (`repeat_medians_ms`).
- **Machine and load.** Recorded: platform, CPUs, memory, Docker VM memory, git sha (and
  whether the tree was dirty), and the 1/5/15-minute load and `uptime` at three points: the
  start, the end, and after the run has settled for `--settle` idle seconds (default 180,
  #160).
  - **The rule:** if the 1-minute load exceeds half the cores at the start or after
    settling, the run is `contended: true` and the file name ends in `-contended`.
  - **Why settle:** read straight at the end, the load average is mostly the benchmark's own
    work, including what it caused in the Docker VM. A `large` run could never pass on
    8 cores that way. After 180 s, the run's contribution has decayed to about 5%
    (`residue_fraction`, `exp(-settle/60)`).
  - **Kept for comparison:** the end reading, and `run_load_estimate` (end minus settled),
    which estimates the run's own load.
  - **Why not subtract the run's CPU:** a process-tree CPU count can't see work the run
    causes in the Docker VM or the I/O it triggers.
  - **Limitation:** background load that comes and goes during the run isn't seen.
  - `--settle 0` restores the old end-of-run rule.
- **Cache state.** Reads are `warm` unless `--cold` is given and this process may drop the
  OS page cache. Then each retrieval repeat starts cold. Dropping caches is opt-in:
  - macOS: run `sudo -v` first so `sudo -n purge` works non-interactively;
  - Linux: run as root, so the harness can write `/proc/sys/vm/drop_caches`.

  Without those privileges the run stays labelled `warm` and records why.
- **In-process or not.** Rows measured inside the benchmark process (engine, library and
  DuckDB reads, Redis via the library) say `in_process: true`. Rows from worker processes,
  PostgreSQL round trips and HTTP say `false`.
- **Warm pools.** The parallel section starts every pool worker and imports the engine in
  each before timing, behind a barrier that every worker must reach (`warmed_workers`).
- **Sampling.** Retrieval samples symbols without replacement (#1), with a fresh seeded
  sample per repeat. The Redis cache is cleared before each repeat, so every miss is a
  real miss.

All data is synthetic (`data_is_synthetic: true`).

## Publishing
Numbers in `docs/BENCHMARKS.md` come only from a quiet machine, with no other agent or
build running and `contended: false` under the rule above. Contended runs may be kept for
diagnosis but are never published. Results written before #160 have no `settled` reading
and are not reclassified; they were judged by the end-of-run rule.
