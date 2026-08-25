"""Tear down the deployed AgentCore runtime, so it stops billing.

`agentcore destroy` removes the runtime and its deployment resources. Note
(from the training): shared infrastructure -- the S3 bucket, the ECR
repository, CloudWatch log groups -- can survive depending on configuration,
so check the console if you want a completely clean account.

    python deploy/03_teardown.py
"""

import subprocess
import sys


def main() -> None:
    try:
        print("+ agentcore destroy")
        subprocess.run(["agentcore", "destroy"], check=True)  # noqa: S603
    except FileNotFoundError:
        sys.exit("`agentcore` not found. Run: pip install -e '.[deploy]'")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"teardown failed ({exc.returncode}); remove the runtime from the console")
    print("Runtime removed. Check S3 / ECR / CloudWatch in the console for leftovers.")


if __name__ == "__main__":
    main()
