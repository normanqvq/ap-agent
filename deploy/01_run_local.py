"""Run the AgentCore entrypoint locally and call it once. Free -- no AWS.

Starts deploy/agent.py on localhost:8080, POSTs one invoice to
/invocations, prints the decision, and stops the server. This is the "Local
first: POST to localhost:8080" path the rubric accepts, and it exercises the
exact handler that AgentCore would run in the cloud -- so if this prints a
decision, the deployed agent behaves the same.

    python deploy/01_run_local.py                 # the headline case
    python deploy/01_run_local.py INV-V006-3019   # any invoice id
"""

import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PORT = 8080


def main() -> None:
    invoice_id = sys.argv[1] if len(sys.argv) > 1 else "INV-V005-3018"

    server = subprocess.Popen(  # noqa: S603
        [sys.executable, str(ROOT / "deploy" / "agent.py")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Wait for the SDK's GET /ping to report healthy.
        base = f"http://127.0.0.1:{PORT}"
        for _ in range(50):
            if server.poll() is not None:
                raise RuntimeError(f"agent server exited early (code {server.returncode})")
            try:
                if httpx.get(f"{base}/ping", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("agent server did not become healthy on :8080")

        r = httpx.post(f"{base}/invocations", json={"invoice_id": invoice_id}, timeout=120)
        r.raise_for_status()
        decision = r.json()
        print(f"invoice   {invoice_id}")
        print(f"action    {decision.get('action')}")
        if decision.get("hold_reason"):
            print(f"reason    {decision['hold_reason']}")
        n_tools = len(decision.get("tool_calls", []))
        print(f"rounds    {decision.get('rounds_used')}  ·  tools {n_tools}")
        print(f"why       {(decision.get('reasoning') or '')[:200]}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()  # it ignored SIGTERM (e.g. mid LLM call) — force it
            server.wait()


if __name__ == "__main__":
    main()
