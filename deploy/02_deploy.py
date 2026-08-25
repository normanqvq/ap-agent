"""Deploy the AP agent to Amazon Bedrock AgentCore. Uses YOUR AWS account.

This wraps the starter toolkit's configure + launch. AgentCore builds the
container (no Dockerfile needed), pushes it to ECR, provisions the serverless
runtime, and waits for READY -- then the agent answers over an HTTPS
endpoint. Billable from here, but the demo's usage sits inside the AWS Free
Tier / hackathon credits.

Prerequisites (yours, not written into this repo):
  - AWS credentials configured (aws configure, or env vars / SSO)
  - Bedrock model access enabled for Claude in your region
  - pip install -e ".[deploy]"

    python deploy/02_deploy.py            # configure + launch, region from env
    AWS_REGION=us-east-1 python deploy/02_deploy.py

Then call it with deploy/04_call_agent.py, and tear it down with
deploy/03_teardown.py so the runtime stops billing.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "deploy" / "agent.py"
REGION = os.getenv("AWS_REGION", "ap-southeast-1")


def _run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)  # noqa: S603


def main() -> None:
    # The starter toolkit ships the `agentcore` CLI. configure writes
    # .bedrock_agentcore.yaml (the IAM role and S3 bucket), launch builds and
    # provisions and waits for READY.
    try:
        _run(["agentcore", "configure", "--entrypoint", str(ENTRYPOINT), "--region", REGION])
        _run(["agentcore", "launch"])
    except FileNotFoundError:
        sys.exit("`agentcore` not found. Run: pip install -e '.[deploy]'")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"deploy failed ({exc.returncode}); check AWS creds and Bedrock model access")
    print("\nDeployed. Call it: python deploy/04_call_agent.py INV-V005-3018")
    print("Tear it down when done: python deploy/03_teardown.py")


if __name__ == "__main__":
    main()
