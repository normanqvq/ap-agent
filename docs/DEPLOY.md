# Deploying the agent to Bedrock AgentCore

The whole pipeline runs as a Bedrock AgentCore agent with one decorator. The
decision logic does not change — AgentCore supplies the HTTP server, the
`POST /invocations` and `GET /ping` routes, the container build, and the
serverless runtime. `deploy/agent.py` is `decide_invoice` plus four lines.

`bedrock-agentcore` is an optional dependency: `pip install -e ".[deploy]"`.

## Run it locally first — no AWS resources

```bash
python deploy/01_run_local.py                 # the headline contract-flip case
python deploy/01_run_local.py INV-V006-3019   # any invoice id
```

This starts `deploy/agent.py` on `localhost:8080`, POSTs one invoice to
`/invocations`, prints the decision, and stops the server. It exercises the
exact handler AgentCore runs in the cloud, so a decision here means the
deployed agent behaves the same. This is the "Local first: POST to
localhost:8080" path — enough on its own, no deployment required.

## Deploy it for real

Needs an AWS account with Bedrock model access (credentials come from your
environment — `aws configure`, env vars, or SSO — never from this repo).

```bash
pip install -e ".[deploy]"
export LLM_PROVIDER=bedrock            # the deployed agent uses Claude on Bedrock
export AWS_REGION=ap-southeast-1

python deploy/02_deploy.py             # configure + launch: build, push to ECR, wait READY
python deploy/04_call_agent.py INV-V005-3018   # call the live HTTPS endpoint via boto3
python deploy/03_teardown.py           # destroy the runtime so it stops billing
```

`configure` writes `.bedrock_agentcore.yaml` (the IAM role and S3 bucket) and
`launch` builds the container — no Dockerfile — pushes it to ECR, and
provisions the serverless runtime. Cost sits inside the AWS Free Tier / the
hackathon's Bedrock credits for a demo's usage; `destroy` stops it. Shared
infrastructure (S3, ECR, CloudWatch) can survive a teardown, so check the
console if you want the account completely clean.

## What ships

The container includes the source and `data/synthetic`, so a deployed agent
decides the same invoices as the local one. The web console
(`apagent.api.app`) is separate — it is the reviewer's UI and can call either
the local service or, with boto3, the deployed endpoint.
