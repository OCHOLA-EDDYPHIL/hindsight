# Hindsight application stack

This stack is the sole lifecycle owner for the deployed demo application. It creates the static UI, CloudFront distribution, HTTP and WebSocket APIs, independently packaged Lambda functions, SQS run queue and dead-letter queue, expiring WebSocket registry, logs, throttles, and alarms.

CockroachDB Cloud and the four SecureString values are external dependencies. Terraform receives only their parameter names and does not create or destroy their values.

Build artifacts before planning:

```bash
make lambda-artifacts
terraform -chdir=infra/terraform/app init -backend-config=...
terraform -chdir=infra/terraform/app plan
```

After apply, run database migrations and configure the changefeed with the `changefeed_webhook_url` output. Before destroy, pause that changefeed. The GitHub workflows automate this ordering.
