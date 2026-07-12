# Database Roles

`roles.sql` is a deployment template for separating database permissions:

- `hindsight_agent_writer` writes agent state, provenance, incidents, lessons, and rewind operations.
- `hindsight_mcp_readonly` reads inspection data and writes only MCP audit/read-provenance rows.
- `hindsight_dashboard_reader` reads memory rows and rewind operations for the live dashboard.

Create role credentials through CockroachDB Cloud, SSO, or a secret manager, then store the resulting connection strings in SSM Parameter Store or Secrets Manager. The SQL template intentionally contains no passwords.
