"""Run the fixed public journal check against the existing US deployment."""
from pathlib import Path
import json
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from deploy.automation.deploy_gcp import Deployment, Target


def main():
    root = Path(__file__).resolve().parents[2]
    target = Target.from_dict(json.loads((root / "deploy/automation/target.json").read_text()))
    if (target.project_id != "commsrx-ai" or target.project_number != "333570013714"
            or target.public_host != "mlr-genie.signalfidelitygroup.com"
            or target.inference_location != "us" or target.allow_global_inference):
        raise ValueError("Journal acceptance requires the exact authorized US target.")
    deployment = Deployment(target, root, root / "deployment-output")
    try:
        deployment.verify_target()
        deployment.resolve_host()
        import httpx
        health = httpx.get(deployment.report["public_url"] + "/health", timeout=30)
        health.raise_for_status()
        if health.json().get("location") != "us" or health.json().get("project_id") != target.project_id:
            raise ValueError("The live endpoint must report the authorized US project.")
        bootstrap = deployment.read_bootstrap_secret()
        with tempfile.TemporaryDirectory(prefix="mlr-journal-acceptance-") as temporary:
            workspace = Path(temporary)
            try:
                deployment.journal_eval(bootstrap, workspace)
            finally:
                diagnostic = workspace / "journal-evaluation/diagnostic.json"
                if diagnostic.exists():
                    deployment.write("journal/diagnostic.json", json.loads(diagnostic.read_text()))
        deployment.report["status"] = "passed"
        deployment.report["scope"] = "journal acceptance of existing deployment"
        return 0
    except Exception as exc:
        deployment.report.update(status="failed", failure=deployment.sanitizer.clean(str(exc)))
        print(deployment.report["failure"], file=sys.stderr)
        return 1
    finally:
        deployment.flush()


if __name__ == "__main__":
    raise SystemExit(main())
