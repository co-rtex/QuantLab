# QuantLab on AWS (Terraform)

**Validated, never applied.** This configuration has never run `plan` or `apply` against
any account. No credentials, backend or secret values exist here. Applying it creates
paid resources, so it needs the user's approval and account (#19, ADR-0012).

## What it creates
| Need | Resource |
|---|---|
| TLS edge | Network Load Balancer: a TLS listener with an ACM certificate and an ELB TLS 1.2/1.3 policy, sending **PROXY protocol v2** to Caddy tasks (`edge/`) on :8080. Caddy honours PROXY headers only from the NLB's subnets (from any other source it rejects the header and serves the connection with its own peer address), and its security group admits only the NLB's group. Caddy overwrites `X-Forwarded-For` with the stated client (ADR-0008). Access logs go to the exports bucket under `nlb/`. |
| Containers | An ECS Fargate cluster (ARM64): `edge`, `web` (its own subnets, which are exactly the API's `QUANTLAB_TRUSTED_PROXIES`), `api`, `worker`, one `reaper` (replaced, never doubled, on deploy), and a one-off `migrate` task. Every container has a read-only root file system, with tmp volumes where it writes. Private DNS (Cloud Map) provides `api.` and `web.<env>.quantlab.internal`. |
| PostgreSQL | RDS PostgreSQL 17: Multi-AZ, encrypted with a customer-managed KMS key, TLS required (`rds.force_ssl`). **Point-in-time recovery** from 14 days of automated backups (7–35). Deletion protection and a final snapshot. The master password is managed by RDS in Secrets Manager. |
| Redis | ElastiCache Redis 7.1, two nodes with automatic failover. Encrypted at rest and in transit (TLS required, `rediss://`). **AUTH required**: Secrets Manager generates a 64-character token at apply time as an ephemeral value. Terraform passes it only to write-only arguments (`auth_token_wo`, and `secret_string_wo` for the `redis_url` secret), so it is in neither the plan nor the state. Reachable only from api, worker and reaper (ADR-0012). |
| `QUANTLAB_DATA_DIR` | **EFS**, not S3: the store needs atomic directory renames, hard links and `.trash` on the same file system as `datasets/` (#97). It is encrypted, mounted over TLS through one access point (uid 10001) with IAM authorization, and backed up daily by AWS Backup (35 days). |
| Object storage | S3 bucket for off-site exports (`pg_dump` archives, data snapshots) and NLB logs: versioned, SSE-KMS, public access blocked, TLS-only policy. |
| Secrets | Secrets Manager **containers only** (no versions): `jwt_secret`, `metrics_token`, `edge_secret`, `database_url`, plus optional `anthropic_key` and `market_data_key`. The one exception is `redis_url`, written write-only with the generated token. These are injected only when `llm_enabled` or `market_data_provider` is set, which they are not by default (#11). |
| Network | VPC with public, web, app and data subnets in 2–3 zones, NAT for outbound HTTPS, flow logs, and the default security group emptied. One group per tier: internet → NLB → edge → web → api → {RDS, Redis, EFS}. |

## Validation (how this was checked)
| Tool | Version | Result |
|---|---|---|
| Terraform | 1.17.1 (darwin_arm64; SHA-256 checked against HashiCorp's SHA256SUMS) | `terraform init -backend=false` and `terraform validate`: valid. `terraform fmt -check`: clean |
| hashicorp/aws provider | 6.71.0 (`.terraform.lock.hcl` hashes for linux/darwin × amd64/arm64) | |
| TFLint | 0.65.0, aws ruleset 0.50.0 | no issues (`preset = "all"`) |
| Checkov | 3.4.0 (via `uvx`) | 388 passed, 0 failed, 25 skipped. Each skip has an inline `#checkov:skip` reason (checkov not recognizing `auth_token_wo`, operator-rotated app secrets, the KMS key policy's `*`, security groups attached via `for_each`, exports-bucket replication/access logs/events, AZ pinning) |
| Caddy | — | **`edge/Caddyfile` NOT VALIDATED**: downloading Caddy to run `caddy adapt` was not permitted in this session. Run `caddy adapt --config edge/Caddyfile` (2.10.2) before building the edge image. |

```sh
terraform -chdir=infra/terraform init -backend=false
terraform -chdir=infra/terraform validate
tflint --chdir=infra/terraform --init && tflint --chdir=infra/terraform
uvx checkov -d infra/terraform --framework terraform --compact
```
CI does not run these yet; that waits on the user's approval of Actions minutes (#17).

## Before apply (all required)
- [ ] **Redis AUTH.** After the first apply, confirm that the replication group reports
      `AuthTokenEnabled` and that the `redis_url` secret holds a `rediss://:<token>@…`
      URL. ElastiCache RBAC with IAM authentication would avoid the shared token, but the
      app (redis-py) cannot generate IAM tokens yet.
- [ ] **Edge.** Run `caddy adapt --config infra/terraform/edge/Caddyfile` (Caddy 2.10.2;
      it has never been run), build the edge image, then run `infra/edge/verify.sh`
      against the public name through a real NLB (#39). It must show each client's own
      address, and a forged `X-Forwarded-For` must have no effect.
- [ ] **PostgreSQL TLS verification.** `database_url` must use `sslmode=verify-full`
      with the RDS CA bundle, which the api image already includes (pinned by checksum
      in services/api/Dockerfile). `sslmode=require` would encrypt without checking the
      server.
- [ ] **JWT secret only where it is needed.** Today every process requires
      `QUANTLAB_JWT_SECRET` in production (config.py), so worker, reaper and migrate get it
      too. #117 scopes it to the API.
- [ ] Real values for `acm_certificate_arn` and `image_tag`. Neither has a default, and the
      example's placeholders fail validation.
- [ ] The user's approval of the account, the plan and the cost.

## Rotating the Redis token
Increase `redis_auth_token_version` and apply. A new token is generated, set on the
replication group (the default `ROTATE` strategy keeps the old token valid alongside the
new one) and written to `redis_url`. Redeploy api, worker and reaper so they read the new
URL. Then apply once more with `auth_token_update_strategy = "SET"` if the old token must
stop working at once.

## Remote state (documented, not configured)
Before a first apply, create an S3 state bucket (versioned, SSE-KMS, public access blocked)
in the target account, then add a backend and run `terraform init -migrate-state`:
```hcl
terraform {
  backend "s3" {
    bucket       = "<account>-quantlab-tfstate"
    key          = "quantlab/<environment>/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true # S3-native locking (Terraform >= 1.10), no DynamoDB table
  }
}
```
State then holds resource ids and ARNs, never secret values. The Redis token and the
`redis_url` value pass only through ephemeral and write-only values, and the RDS password
is managed by RDS.

## First deployment (once approved)
1. Request an ACM certificate for the public host name and set `acm_certificate_arn`.
2. Configure remote state (above), then `terraform plan` and review it with the user. Run
   `apply` only with their approval.
3. Build and push `api` (services/api/Dockerfile), `web` (apps/web/Dockerfile) and `edge`
   (infra/terraform/edge/Dockerfile) for linux/arm64 to the `ecr_repositories` output,
   tagged `image_tag`. Tags are immutable.
4. Set the secrets (`secret_names` output). Use `openssl rand -hex 32` for `jwt_secret` and
   `edge_secret`, and 16+ random characters for `metrics_token`. For `database_url`,
   connect with the RDS master secret (`database_master_secret_arn`), create an
   application role that owns database `quantlab`, and store
   `postgresql+psycopg://<role>:<url-encoded password>@<database_endpoint>:5432/quantlab?sslmode=verify-full&sslrootcert=/etc/ssl/certs/rds-global-bundle.pem`.
5. Run the `migrate` task once (`aws ecs run-task` with the `app_subnet_ids` and
   `migrate_security_group_id` outputs), then let the services start. Run it again before
   every release that changes the schema.
6. Point DNS at `edge_dns_name`, then run `infra/edge/verify.sh` against the public name.
   It must show each client's real address (ADR-0008) before the deployment is announced.

## Disaster recovery
- PostgreSQL: point-in-time restore to a new instance (RDS console or
  `aws rds restore-db-instance-to-point-in-time`), then update `database_url`.
- Data directory: restore an AWS Backup recovery point. **Stop the reaper first**
  (`desired_count = 0`), because restored files keep their old modification times
  (docs/RUNBOOK.md). Cross-region copies of backups and exports are a per-deployment
  choice, not configured here.
