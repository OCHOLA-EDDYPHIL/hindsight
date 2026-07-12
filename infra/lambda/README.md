# Lambda Deployment

This stack serves the Hindsight incident agent through an AWS Lambda Function URL.

## Build

```bash
make lambda-zip
```

The zip is written to `build/lambda/hindsight-agent.zip`, which is ignored by git.

## Required SSM Parameters

Create secure parameters before deploy:

```bash
aws ssm put-parameter --name /hindsight/dev/database-url --type SecureString --value "$DATABASE_URL"
aws ssm put-parameter --name /hindsight/dev/gemini-api-key --type SecureString --value "$GEMINI_API_KEY"
aws ssm put-parameter --name /hindsight/dev/function-url-token --type SecureString --value "$HINDSIGHT_FUNCTION_AUTH_TOKEN"
```

## Deploy

```bash
sam deploy \
  --template-file infra/lambda/template.yaml \
  --stack-name hindsight-agent-dev \
  --capabilities CAPABILITY_IAM \
  --guided
```

The stack output `HindsightAgentFunctionUrl` is the demo endpoint. The handler exposes
`POST /incident` to start a thread and `POST /incident/resume` to continue an interrupted
thread. Calls must include `Authorization: Bearer <demo-token>`.

The Function URL uses app-layer bearer authentication so the same endpoint works for
browser-free demo scripts. Database URLs, model keys, and the function token should be
stored as SSM SecureString parameters for deployed environments.
