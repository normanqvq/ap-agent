"""Call the DEPLOYED AgentCore endpoint via the agentcore CLI.

After deploy/02_deploy.py, this invokes the live runtime. It uses the same
`agentcore` CLI that 02/03 drive -- the CLI loads .bedrock_agentcore.yaml from
the working directory (so run this from the repo root) and invokes the
deployed runtime, so there is nothing to paste. (The in-process
`Runtime().invoke` does not read the cwd config, which is why we shell out.)

    python deploy/04_call_agent.py INV-V005-3018
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".bedrock_agentcore.yaml"


def main() -> None:
    invoice_id = sys.argv[1] if len(sys.argv) > 1 else "INV-V005-3018"
    if not CONFIG.exists():
        sys.exit("No .bedrock_agentcore.yaml -- run deploy/02_deploy.py first.")

    payload = json.dumps({"invoice_id": invoice_id})
    try:
        subprocess.run(["agentcore", "invoke", payload], check=True)  # noqa: S603, S607
    except FileNotFoundError:
        sys.exit("`agentcore` not found. Run: pip install -e '.[deploy]'")
    except subprocess.CalledProcessError as exc:
        sys.exit(
            f"invoke failed ({exc.returncode}); is the runtime deployed? (deploy/02_deploy.py)"
        )


if __name__ == "__main__":
    main()
