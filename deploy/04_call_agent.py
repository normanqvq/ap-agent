"""Call the DEPLOYED AgentCore endpoint with plain boto3.

After deploy/02_deploy.py, this invokes the live HTTPS endpoint the same way
any client would -- no framework, just boto3 against the AgentCore runtime.
It reads the deployed agent's id from .bedrock_agentcore.yaml (written by
configure), so there is nothing to paste.

    python deploy/04_call_agent.py INV-V005-3018
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".bedrock_agentcore.yaml"


def main() -> None:
    invoice_id = sys.argv[1] if len(sys.argv) > 1 else "INV-V005-3018"
    if not CONFIG.exists():
        sys.exit("No .bedrock_agentcore.yaml -- run deploy/02_deploy.py first.")

    # The starter toolkit's Runtime reads .bedrock_agentcore.yaml from the
    # working directory (so run this from the repo root) and invokes the
    # deployed runtime over SigV4 — no ARN to paste.
    try:
        from bedrock_agentcore_starter_toolkit import Runtime
    except ImportError:
        sys.exit("Install the toolkit: pip install -e '.[deploy]'")

    response = Runtime().invoke({"invoice_id": invoice_id})
    print(json.dumps(response, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
