# Database Roles

`roles.sql` is a deployment template for separating database permissions:

- `hindsight_agent_writer` writes agent state, provenance, incidents, and memories against an already-active embedding profile. A semantic write also takes the immutable index fence and may insert a pending task for the current building profile, but it cannot change profile state, lease or update tasks, or activate an index.
- `hindsight_memory_worker` activates embedding profiles and applies queued corrections and consolidation work.
- `hindsight_mcp_readonly` reads inspection data and writes only MCP audit/read-provenance rows.
- `hindsight_dashboard_reader` reads memory rows and rewind operations for the live dashboard.

Create role credentials through CockroachDB Cloud, SSO, or a secret manager, then store the resulting connection strings in SSM Parameter Store or Secrets Manager. The SQL template intentionally contains no passwords.

Activate the hosted embedding profile with the worker or deployment identity before routing restricted agent-writer traffic. During later rotations, writers enqueue new memories for the building profile in the same transaction as the memory write. The agent role cannot activate profiles, process backfill tasks, close versions, change trust state, or delete governed records.
