# Blockers and external limitations

| Blocker | Impact | Mitigation in place |
|---|---|---|
| No market-data API key or licensed dataset | Cannot ingest real observations | Provider abstraction, resilient wrapper, resumable ingestion, CSV provider for user-supplied licensed data; deterministic synthetic provider, always labelled |
| Vendor and licence not chosen (user decision, #11) | The licensed-vendor dataset job (`dataset.vendor`) is inert | Vendor-neutral registry (Massive is the only adapter), bearer-auth HTTP client, settings that keep the key a secret, a job tested only against a scripted fake. **Vendor and licence are chosen by the user; until then this path is inert.** Nothing offers it in the API or UI |
| No LLM API key | Assistant cannot call a hosted model; hosted-model eval unmeasured | Provider boundary implemented (`AnthropicProvider`), deterministic grounded engine, numeric verifier and eval suite with scripted providers (docs/AI_EVAL.md) |
| No cloud credentials | No cloud deployment | Docker Compose deployment; IaC to be validated but not applied |
| Host lacks psql/redis/terraform binaries | None for development | Services run in Docker |
