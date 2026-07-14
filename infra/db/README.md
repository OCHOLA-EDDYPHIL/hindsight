# Database Roles

`roles.sql` is a deployment template for separating database permissions:

- `hindsight_agent_writer` writes agent state, provenance, incidents, and memories against an already-active embedding profile; its read grants also cover foreign-key validation for decision updates.
- `hindsight_memory_worker` activates embedding profiles and applies queued corrections and consolidation work.
- `hindsight_mcp_readonly` reads inspection data and writes only MCP audit/read-provenance rows.
- `hindsight_dashboard_reader` reads memory rows and rewind operations for the live dashboard.

Create role credentials through CockroachDB Cloud, SSO, or a secret manager, then store the resulting connection strings in SSM Parameter Store or Secrets Manager. The SQL template intentionally contains no passwords.

Activate the hosted embedding profile with the worker or deployment identity before routing restricted agent-writer traffic. The agent role cannot activate profiles, close versions, change trust state, or delete governed records.
