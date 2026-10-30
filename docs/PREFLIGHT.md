# Environment Preflight

Recorded 2026-10-30 on the primary development machine. Re-run `scripts/preflight.sh`
to refresh; values below were observed, not assumed.

## Repository and GitHub

| Capability | Status | Evidence |
|---|---|---|
| Local repo at `~/Developer/QuantLab`, branch `main`, no commits | Verified | `git status` |
| Remote `origin` = `https://github.com/co-rtex/QuantLab.git` | Verified | `git remote -v` |
| Git identity | Verified | `user.name`/`user.email` configured (legitimate owner identity, unchanged) |
| GitHub CLI authenticated (`co-rtex`, scopes `repo`, `workflow`, `read:org`, `gist`) | Verified | `gh auth status` |
| Repo is private, empty, viewer permission `ADMIN` | Verified | `gh repo view co-rtex/QuantLab` |
| Push / branch creation | Verified on first push (see STATUS.md) | |
| GitHub Actions | Available (private repo; minutes subject to account plan) | |
| Branch protection | None configured at start (empty repo) | |

## Tooling

| Tool | Version | Status |
|---|---|---|
| Python | 3.12.16 (uv-managed), 3.14.2 system | Verified; project pins 3.12 |
| uv | 0.13.6 | Verified (package/env manager) |
| Node.js / npm | 22.22.2 / 10.9.7 | Verified |
| pnpm | via corepack (not pre-installed) | Available but not configured; npm is used |
| Docker / Compose | 28.5.2 / v2.40.3 | Verified (`docker run hello-world` succeeded; 8 CPU / ~4 GB VM) |
| PostgreSQL / Redis CLIs | not installed on host | Unavailable on host; provided via Docker Compose |
| Terraform | not installed | Unavailable; Terraform files are validated in CI / container instead |
| Playwright | not installed globally | Installed as a project dev dependency |
| Claude Code | 2.1.314 | Verified (`-p/--print`, `--resume`, `--permission-mode`, `--allowedTools`, `--output-format`) |
| C toolchain | Apple clang 17 | Verified |

Host: macOS 26.6.2, Apple Silicon, 8 CPU cores, 8 GB RAM, ~75 GB free disk.

## Network and credentials

| Item | Status |
|---|---|
| Internet (PyPI, npm registry) | Verified (HTTP 200) |
| LLM provider API key (e.g. `ANTHROPIC_API_KEY`) | **Unavailable** — assistant runs on its deterministic grounded engine; LLM path is implemented and tested with a fake provider |
| Market-data provider API key | **Unavailable** — deterministic, clearly-labelled synthetic data plus a CSV importer for user-supplied licensed data |
| Cloud credentials (AWS/GCP/Azure) | **Unavailable** — no cloud resources are provisioned; infrastructure is local Docker Compose plus documented IaC |

Secret values were never printed; only variable names were inspected.

## Classification

* **Available and verified:** Git, GitHub CLI + push, Python/uv, Node/npm, Docker/Compose, Claude Code CLI, internet.
* **Available but not configured:** pnpm (corepack), GitHub branch protection, GitHub Actions secrets.
* **Unavailable:** LLM API key, market-data API key, cloud credentials, host Postgres/Redis/Terraform binaries.
* **Not required for the current phase:** cloud deployment, MLflow server.
