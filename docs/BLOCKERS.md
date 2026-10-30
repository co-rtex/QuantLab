# Blockers and external limitations

| Blocker | Impact | Mitigation in place |
|---|---|---|
| No market-data API key or licensed dataset | Cannot ingest real observations | Provider abstraction, resilient wrapper, resumable ingestion, CSV provider for user-supplied licensed data; deterministic synthetic provider, always labelled |
| No LLM API key | Assistant cannot call a hosted model | Planned: tool-calling boundary, deterministic grounded fallback, evaluation suite with a fake provider |
| No cloud credentials | No cloud deployment | Docker Compose deployment; IaC to be validated but not applied |
| Host lacks psql/redis/terraform binaries | None for development | Services run in Docker |
