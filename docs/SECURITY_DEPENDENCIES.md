# Dependency security audit

Locked third-party dependencies, audited for known vulnerabilities (#137). Each entry
records the date, the tool versions, the exact commands and their real output. Re-run the
audit and add a new dated entry; don't edit old results.

## 2026-10-31 audit (main at be2e5b8)

**Result:** no known vulnerability in any runtime dependency, Python or npm. One advisory
affects the web app's lint tooling only. It's accepted below.

| Scope | Packages | Tool | Result |
|---|---|---|---|
| Python, runtime (`--no-dev`) | 80 | pip-audit 2.10.1, PyPI advisory service | No known vulnerabilities |
| Python, runtime and dev (`--all-groups`) | 101 | pip-audit 2.10.1, PyPI advisory service | No known vulnerabilities |
| Python, runtime and dev (`--all-groups`) | 101 | pip-audit 2.10.1, OSV (`-s osv`) | No known vulnerabilities |
| npm, runtime (`--omit=dev`) | 61 | npm 10.9.7 (Node 22.22.2) | found 0 vulnerabilities |
| npm, with dev | 479 | npm 10.9.7 | 16 moderate, one advisory (below) |

The tools were uv 0.13.6, pip-audit 2.10.1 (run through `uvx`), npm 10.9.7 and Node 22.22.2,
run at 2026-10-31T14:44Z on macOS (arm64).

### Python

The locked requirements were exported from `uv.lock` without the workspace's own packages,
then audited as pinned, without re-resolving:

```
uv export --frozen --all-packages --no-dev      --no-emit-workspace --no-hashes --format requirements-txt -o req-prod.txt
uv export --frozen --all-packages --all-groups  --no-emit-workspace --no-hashes --format requirements-txt -o req-all.txt
uvx pip-audit -r req-all.txt --no-deps --disable-pip --progress-spinner off
uvx pip-audit -r req-all.txt --no-deps --disable-pip --progress-spinner off -s osv
```

Output: both services printed `No known vulnerabilities found`. pip-audit also printed
`--no-deps is supported, but users are encouraged to fully hash their pinned dependencies`,
which is expected because the export is pinned without hashes and `uv.lock` holds them. It
printed `Cache entry deserialization failed, entry ignored` warnings from its local HTTP
cache, which don't affect the result.

`req-prod.txt` (80 packages) holds the same packages CI audits, because the root project
depends on both workspace members. CI's latest passing run on main (run 41210896370, at
b85e3c0) printed `No known vulnerabilities found` in its *Dependency audit* step.

Versions of the security-relevant packages audited: fastapi 0.162.0, starlette 1.10.0,
pyjwt 2.15.1, sqlalchemy 2.1.6, psycopg 3.3.6, pydantic 2.14.2, rq 3.0.0, redis 8.1.0,
uvicorn 0.54.0, httpx 0.28.1.

### npm (`apps/web`)

```
npm audit --omit=dev      ->  found 0 vulnerabilities
npm audit --omit=dev --json  metadata: dependencies prod 61, dev 381, optional 88, total 479
npm audit                 ->  16 moderate severity vulnerabilities
```

CI's run 41210896370 printed `found 0 vulnerabilities` for
`npm audit --omit=dev --audit-level=high`.

All 16 moderate findings trace to one advisory:

| Advisory | Package | Affected | Installed | Pulled in by |
|---|---|---|---|---|
| [GHSA-g3hv-75wg-m249](https://github.com/advisories/GHSA-g3hv-75wg-m249): "minimatch has potential denial of service via globparts amplification" | minimatch | < 11.0.1 | 3.1.5 (6 copies), 10.2.6 | `eslint`, `eslint-config-next` and their plugins (`@typescript-eslint/*`, `eslint-plugin-import`, `-react`, `-jsx-a11y`, `@next/eslint-plugin-next`) |

- **Reachable?** No. minimatch is only in the lint toolchain. `npm ls --omit=dev minimatch`
  is empty, so nothing that ships or runs in production loads it. Lint matches the
  repository's own configured globs, which no request can reach. The advisory is a
  denial of service through a crafted pattern, and the patterns here are written by us.
- **Fix now?** No. `npm audit fix --force` would *downgrade* `eslint` to 4.0.0 and
  `eslint-config-next` to 0.2.4, a major regression. An `overrides` entry forcing
  minimatch ≥ 11.0.1 would cross two major versions under consumers written for the 3.x
  API, with no evidence it works.
- **Decision:** accepted for now. Revisit when eslint and eslint-config-next ship releases
  that depend on minimatch ≥ 11.0.1. Then propose the bump on the lead's lease
  (`apps/web/package*.json` are shared).

### Upgrades proposed

None needed for a vulnerability. Nothing is reported against a runtime dependency, and the
dev-only minimatch finding has no compatible fix yet.

## Warnings seen in the test suite

### RQ: `Composite key must contain job_id:execution_id`

**Cause: a test fixture, not production.**
- `tests/unit/test_rq_trusted.py::test_registry_cleanup_does_not_run_a_failure_callback`
  forged an abandoned job by adding a bare job id to the `StartedJobRegistry` sorted set.
- RQ 3.0.0's worker keys entries there as `<job id>:<execution id>`. On any other form,
  `rq.utils.parse_composite_key` warns (`rq/utils.py:614`).

**Production doesn't write these entries.**
- No code under `services/api/src` or `packages/quantlab/src` writes an RQ registry
  (`zadd`, `registry.add`). Only RQ's worker does, with an execution id.
- Our delivery ids (`<job id>`, `<job id>-<due µs>`, `<job id>-<due µs>-again`, #129/#132)
  contain no `:`, so a worker's `<delivery id>:<execution id>` entry parses into exactly two
  parts.

**Fixed (#137):**
- The fixture now writes `<job id>:lost-execution`, the form RQ writes.
- With `-W error::DeprecationWarning`, `tests/unit/test_rq_trusted.py` passes (13 tests).
  Before the fix it failed on that test.
- Both halves of the test still hold: no failure callback runs through our queue, and the
  callback does run through the control queue.

**Real worker paths checked:** the whole suite was run with only this warning made an
error: `pytest -q -W "error:Composite key must contain:DeprecationWarning"` gave 970 passed,
1 xfailed. That run includes `tests/integration/test_job_clock.py`, where a real RQ
`SimpleWorker` runs `<id>-<due>` and `<id>-again` deliveries.

### Starlette: `Using httpx with starlette.testclient is deprecated; install httpx2 instead`

**Test-only.** `fastapi.testclient` (Starlette 1.10.0's TestClient) emits it on import, and
no code under `services/api/src` or `packages/quantlab/src` imports `TestClient`. Nothing
breaks today. A future Starlette that drops httpx support would break the integration tests'
client, not the service.

**Note:** `httpx>=0.28` is declared as a *runtime* dependency of `quantlab-api`
(`services/api/pyproject.toml`), yet no runtime code imports it. Moving it to the dev group,
and adding `httpx2` there when Starlette requires it, changes `pyproject.toml` and `uv.lock`.
That needs a lease, so it's proposed on #137, not done here.

### PyJWT: `InsecureKeyLengthWarning: The HMAC key is 14 bytes long`

**Test-only.** `tests/integration/test_security.py::test_unauthenticated_and_tampered_tokens`
forges a token with the deliberately wrong key `"not-the-secret"` (14 bytes) to show it's
refused. PyJWT warns when *encoding* with that key. The service never signs with it.

**Production refuses short signing keys.**
- `Settings.require_api_secrets()` (`services/api/src/quantlab_api/config.py`) runs first in
  `create_app` with `QUANTLAB_ENVIRONMENT=production`. It refuses a missing secret, one under
  32 characters, and a placeholder (`insecure`, `change-me`, `changeme`, `dev-only`).
- It counts characters, and every character is at least one UTF-8 byte. So an accepted
  secret always has at least 32 bytes, the RFC 7518 §3.2 minimum for HS256.
- It's slightly stricter than a byte count: 16 × `é` (32 bytes) is refused.
- **Probe** (`Settings(...).require_api_secrets()`): 31 ASCII characters are refused, 32 are
  accepted, 31 × `é` (62 bytes) is refused, and 16 × `é` (32 bytes) is refused.
- **Pinned** by the new `tests/unit/test_jwt_secret_floor.py`:
  - 31 characters are refused and 32 accepted;
  - every accepted secret is at least 32 bytes.

## CI: proposed hardening (a proposal; `.github/workflows/**` belongs to the lead)

CI already audits both runtime trees on every push (`.github/workflows/ci.yml`), with two
gaps:

1. **The Python audit never fails the build.** It ends with
   `|| echo "::warning::pip-audit reported findings"`. A known runtime vulnerability shows
   only as a warning.
2. **No scheduled run.** New advisories land against unchanged lockfiles, and nothing
   audits until the next push.

Proposed, with the tool pinned for reproducible output. Each accepted finding gets an
`--ignore-vuln <ID>` that names its entry in this file.

```yaml
# python job: replace the existing "Dependency audit" step
      - name: Dependency audit (runtime, blocking)
        run: >-
          uvx pip-audit==2.10.1 --strict --progress-spinner off
          -r <(uv export --frozen --no-dev --no-hashes --no-emit-workspace)
      - name: Dependency audit (dev tools, report only)
        run: >-
          uvx pip-audit==2.10.1 --progress-spinner off
          -r <(uv export --frozen --all-groups --no-hashes --no-emit-workspace)
          || echo "::warning::pip-audit reported findings in dev tools"

# web job: keep the runtime audit blocking, at any severity; report dev tools only
      - run: npm audit --omit=dev --audit-level=low
      - name: npm audit (dev tools, report only)
        run: npm audit || echo "::warning::npm audit reported findings in dev tools (see docs/SECURITY_DEPENDENCIES.md)"

# workflow triggers: audit weekly even when nothing is pushed
on:
  schedule:
    - cron: "17 6 * * 1"
```

**Trade-off:** a blocking audit can fail CI when PyPI's or npm's advisory service is down.
Re-running the job is enough. Making it non-blocking would hide real findings.
