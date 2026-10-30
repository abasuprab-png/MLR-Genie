#!/usr/bin/env python3
"""Guarded, repeatable GCP production deployment; cloud access starts in main().

Run after GitHub OIDC authentication and the PostgreSQL tests. Credentials and
Terraform plans stay outside deployment-output; only sanitized evidence is saved.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import quote, urlsplit


class DeploymentError(RuntimeError):
    """A failed operation whose message is safe after sanitization."""


HOST_RE = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
SERVICES = (
    "serviceusage.googleapis.com", "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com", "iamcredentials.googleapis.com", "sts.googleapis.com",
    "artifactregistry.googleapis.com", "run.googleapis.com", "compute.googleapis.com",
    "sqladmin.googleapis.com", "secretmanager.googleapis.com", "storage.googleapis.com",
    "aiplatform.googleapis.com", "logging.googleapis.com", "monitoring.googleapis.com",
)
BOOTSTRAP_TARGETS = (
    "google_service_account.mlr_genie_runtime_sa",
    "google_project_service.vertex_ai", "google_project_iam_member.runtime_vertex_ai",
    "google_project_service.cloud_sql_admin", "google_sql_database_instance.mlr_genie",
    "google_sql_database.mlr_genie", "google_sql_user.runtime_iam",
    "google_project_iam_member.runtime_cloud_sql",
    "google_storage_bucket.mlr_artifacts_bucket",
    "google_storage_bucket_iam_member.runtime_artifact_objects",
    "google_secret_manager_secret.session_signing", "google_secret_manager_secret.bootstrap",
    "google_secret_manager_secret_iam_member.auth_runtime_access",
)
PAPER_URL = "https://journals.plos.org/plosmedicine/article/file?id=10.1371/journal.pmed.0020124&type=printable"
PAPER_SHA256 = "ffc1005680cb620eec4c913437dfabbf311b535cfe16cbaeb2faec1f92afc362"
PAPER_CLAIM = "Within the paper’s framework, research findings are less likely to be true in fields where studies are smaller."


@dataclass(frozen=True)
class Target:
    project_id: str
    project_number: str
    repo_full_name: str
    region: str = "us-central1"
    public_host: str = ""

    @classmethod
    def from_dict(cls, value: dict) -> "Target":
        target = cls(**{key: str(value[key]) for key in (
            "project_id", "project_number", "repo_full_name", "region", "public_host"
        ) if key in value})
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", target.project_id):
            raise ValueError("A concrete GCP project_id is required.")
        if not re.fullmatch(r"[1-9][0-9]{5,19}", target.project_number):
            raise ValueError("A concrete numeric project_number is required.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", target.repo_full_name):
            raise ValueError("repo_full_name must be an exact GitHub owner/repository.")
        if not re.fullmatch(r"[a-z]+-[a-z]+[0-9]", target.region):
            raise ValueError("region must be a GCP region such as us-central1.")
        if target.public_host and not HOST_RE.fullmatch(target.public_host):
            raise ValueError("public_host must be a lowercase DNS hostname without scheme or path.")
        return target

    @property
    def state_bucket(self) -> str:
        return f"mlr-genie-tfstate-{self.project_number}"

    @property
    def runtime_sa(self) -> str:
        return f"mlr-genie-adk-runtime@{self.project_id}.iam.gserviceaccount.com"

    @property
    def deployer_sa(self) -> str:
        return f"mlr-genie-deployer@{self.project_id}.iam.gserviceaccount.com"


def validate_project_identity(target: Target, actual: dict) -> None:
    if (actual.get("projectId") != target.project_id
            or str(actual.get("projectNumber", "")) != target.project_number
            or actual.get("lifecycleState") != "ACTIVE"):
        raise DeploymentError("Refusing deployment: project ID, number, and ACTIVE lifecycle must all match the target.")


def validate_plan(plan: dict, target: Target | None = None) -> list[dict]:
    """Reject deletes, either replacement order, unknown actions and cross-project writes."""
    summary = []
    for resource in plan.get("resource_changes", []):
        actions = resource.get("change", {}).get("actions", [])
        if not actions or any(action not in {"no-op", "create", "update", "read"} for action in actions):
            raise DeploymentError(f"Refusing destructive or unsupported Terraform actions at {resource.get('address', 'unknown')}.")
        after = resource.get("change", {}).get("after") or {}
        if target and after.get("project") not in (None, target.project_id, target.project_number):
            raise DeploymentError(f"Refusing cross-project Terraform resource {resource.get('address', 'unknown')}.")
        summary.append({"address": resource["address"], "actions": actions})
    return summary


def quote_identifier(value: str) -> str:
    if not value or "\x00" in value:
        raise ValueError("PostgreSQL identifiers must be nonempty and contain no NUL.")
    return '"' + value.replace('"', '""') + '"'


def bootstrap_sql(runtime_role: str) -> str:
    role = quote_identifier(runtime_role)
    return f"""DO $$ BEGIN
  IF current_database() <> 'mlr_genie' THEN
    RAISE EXCEPTION 'Refusing to bootstrap outside the mlr_genie database.';
  END IF;
END $$;
BEGIN;
GRANT CONNECT ON DATABASE mlr_genie TO {role};
CREATE SCHEMA IF NOT EXISTS mlr_genie;
GRANT USAGE, CREATE ON SCHEMA mlr_genie TO {role};
COMMIT;
"""


class Sanitizer:
    def __init__(self):
        self._secrets: set[str] = set()

    def add_secret(self, value: str) -> None:
        if value:
            self._secrets.add(value)

    def clean(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): "[REDACTED]" if re.search(
                r"(^|_)(access_token|refresh_token|id_token|authorization|password|bootstrap_secret|session_secret|private_key|client_secret)$",
                str(key), re.I,
            ) else self.clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in value]
        if isinstance(value, str):
            for secret in sorted(self._secrets, key=len, reverse=True):
                value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", value)
            value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED]", value)
        return value


class Runner:
    def __init__(self, root: Path, sanitizer: Sanitizer):
        self.root, self.sanitizer = root, sanitizer

    def run(self, args: list[str], *, input_text: str | None = None,
            env: dict | None = None, timeout: int = 300,
            sensitive: bool = False, check: bool = True) -> subprocess.CompletedProcess:
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise TypeError("Commands must use argument lists.")
        try:
            result = subprocess.run(args, cwd=self.root, input=input_text, env=env,
                                    text=True, capture_output=True, shell=False,
                                    timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise DeploymentError(f"{Path(args[0]).name} exceeded its {timeout}s command deadline.") from None
        if check and result.returncode:
            detail = "Sensitive command output withheld." if sensitive else self.sanitizer.clean(
                (result.stderr or result.stdout or "No diagnostic output.")[-6000:])
            raise DeploymentError(f"{Path(args[0]).name} exited {result.returncode}: {detail}")
        return result


class Deployment:
    def __init__(self, target: Target, root: Path, output: Path,
                 runner: Runner | None = None, *, readiness_timeout: int = 7200):
        self.target, self.root, self.output = target, root.resolve(), output.resolve()
        self.sanitizer = Sanitizer()
        self.sanitizer.add_secret(os.getenv("GITHUB_TOKEN", ""))
        self.runner = runner or Runner(self.root, self.sanitizer)
        self.readiness_timeout = readiness_timeout
        self.stage = "initializing"
        self.output.mkdir(parents=True, exist_ok=True)
        self.report: dict = {
            "started_at": self.now(), "status": "running", "project_id": target.project_id,
            "project_number": target.project_number, "region": target.region,
            "repo_full_name": target.repo_full_name, "checks": {}, "stages": [],
        }
        self.tf = ["terraform", f"-chdir={self.root / 'deploy/terraform'}"]
        self.plan_summaries: dict = {}

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def write(self, name: str, data: Any) -> None:
        path = self.output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.sanitizer.clean(data), indent=2) + "\n")
        temporary.replace(path)

    def flush(self) -> None:
        self.write("report.json", self.report)

    @contextmanager
    def phase(self, name: str):
        self.stage = name
        print(f"[{self.now()}] {name}", flush=True)
        entry = {"name": name, "started_at": self.now(), "status": "running"}
        self.report["stages"].append(entry)
        self.flush()
        try:
            yield
        except Exception:
            entry["status"] = "failed"
            raise
        else:
            entry["status"] = "passed"
        finally:
            entry["finished_at"] = self.now()
            self.flush()

    def gcloud(self, *args: str, **kwargs) -> subprocess.CompletedProcess:
        return self.runner.run(["gcloud", *args, f"--project={self.target.project_id}", "--quiet"], **kwargs)

    def cloud_json(self, *args: str, **kwargs) -> Any:
        return json.loads(self.gcloud(*args, "--format=json", **kwargs).stdout)

    def verify_target(self) -> None:
        validate_project_identity(self.target, self.cloud_json("projects", "describe", self.target.project_id))
        if os.getenv("GITHUB_ACTIONS") == "true":
            if os.getenv("GITHUB_REPOSITORY") != self.target.repo_full_name:
                raise DeploymentError("GitHub repository does not match the authorized target.")
            if os.getenv("GITHUB_REF") != "refs/heads/deploy/production":
                raise DeploymentError("Only refs/heads/deploy/production may deploy this target.")
        commit = self.runner.run(["git", "rev-parse", "HEAD"]).stdout.strip()
        if not re.fullmatch(r"[a-f0-9]{40}", commit) or (os.getenv("GITHUB_SHA") and os.getenv("GITHUB_SHA") != commit):
            raise DeploymentError("Checked-out commit does not match the workflow commit.")
        self.report["commit"] = commit
        account = self.gcloud("auth", "list", "--filter=status:ACTIVE", "--format=value(account)").stdout.strip()
        if account != self.target.deployer_sa:
            raise DeploymentError("Active gcloud identity must be the target project's mlr-genie-deployer service account.")
        self.report["deployment_identity"] = account
        self.report["checks"]["exact_active_project"] = True

    def public_zones(self) -> list[dict]:
        result = self.gcloud("dns", "managed-zones", "list", "--format=json", check=False)
        if result.returncode:
            return []
        return [zone for zone in json.loads(result.stdout) if zone.get("visibility", "public").lower() == "public"]

    def resolve_host(self) -> None:
        if not self.target.public_host:
            zones = self.public_zones()
            domains = {zone.get("dnsName", "").rstrip(".") for zone in zones}
            domains.discard("")
            if len(domains) != 1:
                self.report["dns_required"] = "Supply a controlled public_host in target.json; no single public Cloud DNS zone was found. No paid deployment resources created."
                raise DeploymentError(self.report["dns_required"])
            self.target = replace(self.target, public_host="mlr-genie." + domains.pop())
            if not HOST_RE.fullmatch(self.target.public_host):
                raise DeploymentError("Cloud DNS did not yield a valid public hostname.")
        self.report["public_host"] = self.target.public_host
        self.report["public_url"] = "https://" + self.target.public_host

    def ensure_state_bucket(self) -> None:
        uri = "gs://" + self.target.state_bucket
        result = self.gcloud("storage", "buckets", "describe", uri, "--raw", "--format=json", check=False)
        if result.returncode:
            # Fail closed on permission and transport errors instead of assuming absence.
            if not re.search(r"(?i)(\b404\b|not found|does not exist)", result.stderr):
                raise DeploymentError("Cannot verify the Terraform state bucket; no bucket creation attempted.")
            self.gcloud("storage", "buckets", "create", uri, f"--location={self.target.region}",
                        "--uniform-bucket-level-access", "--public-access-prevention")
            metadata = self.cloud_json("storage", "buckets", "describe", uri, "--raw")
        else:
            metadata = json.loads(result.stdout)
        if str(metadata.get("projectNumber", "")) != self.target.project_number:
            raise DeploymentError("Refusing state bucket outside the verified project number.")
        self.gcloud("storage", "buckets", "update", uri, "--versioning",
                    "--uniform-bucket-level-access", "--public-access-prevention")
        metadata = self.cloud_json("storage", "buckets", "describe", uri, "--raw")
        iam = metadata.get("iamConfiguration", {})
        if (str(metadata.get("projectNumber")) != self.target.project_number
                or not metadata.get("versioning", {}).get("enabled")
                or not iam.get("uniformBucketLevelAccess", {}).get("enabled")
                or iam.get("publicAccessPrevention") != "enforced"):
            raise DeploymentError("State bucket private/versioned verification failed.")
        self.report["state_bucket"] = self.target.state_bucket

    def build_image(self) -> str:
        repositories = self.cloud_json("artifacts", "repositories", "list", f"--location={self.target.region}")
        name = f"projects/{self.target.project_id}/locations/{self.target.region}/repositories/mlr-genie"
        matches = [item for item in repositories if item.get("name") == name]
        if matches and matches[0].get("format") != "DOCKER":
            raise DeploymentError("Existing mlr-genie Artifact Registry repository is not a Docker repository.")
        if not matches:
            self.gcloud("artifacts", "repositories", "create", "mlr-genie", "--repository-format=docker",
                        f"--location={self.target.region}", "--description=MLR Genie production images")
        host = f"{self.target.region}-docker.pkg.dev"
        self.gcloud("auth", "configure-docker", host)
        image_path = f"{host}/{self.target.project_id}/mlr-genie/app"
        image = image_path + ":" + self.report["commit"]
        self.runner.run(["docker", "build", "--platform=linux/amd64", "--tag", image,
                         "--label", "org.opencontainers.image.revision=" + self.report["commit"], "."], timeout=1800)
        self.runner.run(["docker", "push", image], timeout=1800)
        # Image `describe` also queries Container Analysis when its API is
        # enabled. Tags are an Artifact Registry-only route to the same digest.
        tags = self.cloud_json("artifacts", "docker", "tags", "list", image_path)
        package = name + "/packages/app"
        expected_tag = package + "/tags/" + self.report["commit"]
        matches = [item for item in tags if item.get("tag") == expected_tag and item.get("image") == image_path]
        version = matches[0].get("version", "") if len(matches) == 1 else ""
        digest = version.removeprefix(package + "/versions/")
        if not version.startswith(package + "/versions/") or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise DeploymentError("Artifact Registry did not return an immutable manifest digest.")
        immutable = image_path + "@" + digest
        self.report["image"] = immutable
        self.report["image_digest"] = digest
        return immutable

    def plan_apply(self, name: str, workspace: Path, variables: Path, targets: tuple[str, ...] = ()) -> None:
        plan_path = workspace / (name + ".tfplan")
        args = [*self.tf, "plan", "-input=false", "-lock-timeout=5m", "-no-color",
                f"-var-file={variables}", f"-out={plan_path}", *[f"-target={target}" for target in targets]]
        self.runner.run(args, timeout=1800)
        plan = json.loads(self.runner.run([*self.tf, "show", "-json", str(plan_path)]).stdout)
        self.plan_summaries[name] = validate_plan(plan, self.target)
        self.write("plan-summary.json", self.plan_summaries)
        self.runner.run([*self.tf, "apply", "-input=false", "-no-color", "-lock-timeout=5m", str(plan_path)], timeout=3600)

    def terraform_outputs(self) -> dict:
        values = json.loads(self.runner.run([*self.tf, "output", "-json"]).stdout)
        return {key: item["value"] for key, item in values.items()}

    def publish_ip(self) -> None:
        ip = self.terraform_outputs().get("public_ip", "")
        import ipaddress
        if not ipaddress.ip_address(ip).is_global:
            raise DeploymentError("Terraform public_ip is not a global public IP address.")
        self.report["public_ip"] = ip
        self.report["dns_required"] = {"type": "A", "name": self.target.public_host, "value": ip, "proxy": "DNS only"}
        self.flush()
        print(f"PUBLIC_IP={ip} PUBLIC_HOST={self.target.public_host}", flush=True)
        self._publish_public_ip_status()

    def _publish_public_ip_status(self, *, state: str = "pending") -> None:
        """Expose the early IP without waiting for GitHub's completed-job logs."""
        import httpx
        token = os.getenv("GITHUB_TOKEN", "")
        self.sanitizer.add_secret(token)
        if os.getenv("GITHUB_ACTIONS") != "true":
            return
        lookup = ("gcloud compute addresses describe mlr-genie-https --global "
                  f"--project={self.target.project_id} --format='value(address)'")
        reason = "GitHub commit-status credential unavailable."
        if token:
            commit = self.report.get("commit", "")
            if not re.fullmatch(r"[a-f0-9]{40}", commit) or state not in {"pending", "success"}:
                raise DeploymentError("Invalid public-IP commit-status identity or state.")
            ip = self.report["public_ip"]
            host = self.target.public_host
            limit = 140 - len(f"A  -> {ip}")
            if len(host) > limit:
                host = host[:limit - 3] + "..."
            payload = {"state": state, "context": "mlr-genie/public-ip",
                       "description": f"A {host} -> {ip}",
                       "target_url": "https://" + self.target.public_host}
            endpoint = f"https://api.github.com/repos/{self.target.repo_full_name}/statuses/{commit}"
            try:
                with httpx.Client(timeout=20, follow_redirects=False, verify=True) as client:
                    response = client.post(endpoint, headers={
                        "Authorization": "Bearer " + token,
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    }, json=payload)
                if response.status_code == 201:
                    self.report["public_ip_status"] = {"channel": "GitHub commit status", "state": state,
                                                       "context": payload["context"], "description": payload["description"]}
                    self.flush()
                    print("Published public IP through GitHub commit status mlr-genie/public-ip.", flush=True)
                    return
                reason = "GitHub commit-status publication returned an unsuccessful HTTP status."
            except httpx.HTTPError:
                # Neither exception text nor response bodies are safe evidence:
                # they can include request headers or reflect credentials.
                reason = "GitHub commit-status publication could not reach the API."
        self.report["public_ip_status"] = {"channel": "GitHub commit status", "state": "unavailable",
                                           "reason": reason, "cloud_shell_lookup": lookup}
        self.flush()
        print("Public-IP status channel unavailable. Cloud Shell fallback: " + lookup, flush=True)

    def ensure_secret(self, secret_id: str) -> None:
        versions = self.cloud_json("secrets", "versions", "list", secret_id)
        if not versions:
            value = secrets.token_urlsafe(64)
            self.sanitizer.add_secret(value)
            self.gcloud("secrets", "versions", "add", secret_id, "--data-file=-", input_text=value, sensitive=True)
        else:
            latest = self.cloud_json("secrets", "versions", "describe", "latest", "--secret=" + secret_id)
            if latest.get("state") != "ENABLED":
                raise DeploymentError(f"Secret {secret_id} exists but its latest version is not enabled; refusing automatic rotation.")

    def read_bootstrap_secret(self) -> str:
        value = self.gcloud("secrets", "versions", "access", "latest", "--secret=mlr-genie-bootstrap", sensitive=True).stdout
        if len(value) < 32:
            raise DeploymentError("The deployed bootstrap credential is unexpectedly short.")
        self.sanitizer.add_secret(value)
        if os.getenv("GITHUB_ACTIONS") == "true":
            escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print("::add-mask::" + escaped, flush=True)
        return value

    def bootstrap_database(self, workspace: Path) -> None:
        outputs = self.terraform_outputs()
        role = outputs.get("database_iam_role")
        if role != self.target.runtime_sa.removesuffix(".gserviceaccount.com"):
            raise DeploymentError("Terraform database IAM role does not match the target runtime service account.")
        instance = self.cloud_json("sql", "instances", "describe", "mlr-genie-postgres")
        if instance.get("project") != self.target.project_id:
            raise DeploymentError("Cloud SQL instance project does not match the verified target.")
        sql_sa = instance.get("serviceAccountEmailAddress", "")
        if not re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\.gserviceaccount\.com", sql_sa):
            raise DeploymentError("Cloud SQL did not report its storage import service account.")
        sql_path = workspace / "bootstrap.sql"
        sql_path.write_text(bootstrap_sql(role))
        object_name = f"bootstrap/{self.report['commit']}-{secrets.token_hex(8)}.sql"
        uri = f"gs://{self.target.state_bucket}/{object_name}"
        expiry = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        condition = {
            "title": "mlr-genie-bootstrap-" + secrets.token_hex(6),
            "description": "Temporary access to this deployment's exact SQL bootstrap object",
            "expression": f"resource.name == 'projects/_/buckets/{self.target.state_bucket}/objects/{object_name}' && request.time < timestamp('{expiry}')",
        }
        condition_path = workspace / "bootstrap-condition.json"
        condition_path.write_text(json.dumps(condition))
        iam_args = ["storage", "buckets", "add-iam-policy-binding", "gs://" + self.target.state_bucket,
                    "--member=serviceAccount:" + sql_sa, "--role=roles/storage.objectViewer",
                    "--condition-from-file=" + str(condition_path)]
        generation = None
        uploaded = False
        granted = False
        try:
            self.gcloud("storage", "cp", str(sql_path), uri, "--if-generation-match=0")
            uploaded = True
            generation = self.gcloud("storage", "objects", "describe", uri, "--format=value(generation)").stdout.strip()
            if not generation.isdigit():
                raise DeploymentError("Could not verify the temporary bootstrap object generation.")
            self.gcloud(*iam_args)
            granted = True
            # Let the narrowly scoped IAM binding propagate before Cloud SQL reads it.
            time.sleep(30)
            operation = self.cloud_json("sql", "import", "sql", "mlr-genie-postgres", uri,
                                        "--database=mlr_genie", "--user=postgres", "--async")
            operation_name = operation.get("name", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", operation_name):
                raise DeploymentError("Cloud SQL import did not return an operation ID.")
            deadline = time.monotonic() + 1800
            while True:
                status = self.cloud_json("sql", "operations", "describe", operation_name)
                if status.get("status") == "DONE":
                    if status.get("error"):
                        raise DeploymentError("Cloud SQL schema import failed: " + json.dumps(self.sanitizer.clean(status["error"])))
                    break
                if time.monotonic() >= deadline:
                    raise DeploymentError("Cloud SQL schema import did not finish within 30 minutes.")
                print("Waiting for Cloud SQL schema import.", flush=True)
                time.sleep(30)
        finally:
            failures = []
            if granted:
                remove = [*iam_args]
                remove[2] = "remove-iam-policy-binding"
                try:
                    self.gcloud(*remove)
                except DeploymentError:
                    failures.append("temporary Cloud SQL bucket IAM binding")
            if uploaded:
                try:
                    if generation and generation.isdigit():
                        self.gcloud("storage", "rm", uri + "#" + generation)
                    else:
                        # The random object name was created with a generation-zero
                        # precondition; this exact URI cannot contain prior data.
                        self.gcloud("storage", "rm", uri, "--all-versions")
                except DeploymentError:
                    failures.append("temporary SQL bootstrap object")
            if failures:
                self.report["cleanup_required"] = failures
                self.flush()
                raise DeploymentError("Bootstrap cleanup needs attention: " + ", ".join(failures))
        self.report["checks"]["database_schema_bootstrapped"] = True

    def configure_dns_if_authoritative(self) -> None:
        import httpx
        host = self.target.public_host + "."
        zones = [zone for zone in self.public_zones() if host.endswith("." + zone.get("dnsName", "")) or host == zone.get("dnsName")]
        for zone in sorted(zones, key=lambda item: len(item.get("dnsName", "")), reverse=True):
            try:
                with httpx.Client(timeout=20, follow_redirects=False) as client:
                    response = client.get("https://dns.google/resolve", params={"name": zone["dnsName"], "type": "NS"})
                    response.raise_for_status()
                    public_ns = {item["data"].rstrip(".").lower() for item in response.json().get("Answer", []) if item.get("type") == 2}
            except (httpx.HTTPError, ValueError, KeyError):
                continue
            zone_ns = {value.rstrip(".").lower() for value in zone.get("nameServers", [])}
            if not public_ns or public_ns != zone_ns:
                continue
            records = self.cloud_json("dns", "record-sets", "list", "--zone=" + zone["name"], "--name=" + host)
            address = self.report["public_ip"]
            relevant = [item for item in records if item.get("name") == host and item.get("type") in {"A", "AAAA", "CNAME"}]
            if relevant:
                if len(relevant) != 1 or relevant[0].get("type") != "A" or relevant[0].get("rrdatas") != [address]:
                    raise DeploymentError("Authoritative Cloud DNS already has a conflicting address/alias; refusing to overwrite it.")
            else:
                self.gcloud("dns", "record-sets", "create", host, "--zone=" + zone["name"],
                            "--type=A", "--ttl=300", "--rrdatas=" + address)
            self.report["dns"] = {"authority": "Cloud DNS", "zone": zone["name"], "status": "configured"}
            self.flush()
            return
        self.report["dns"] = {"status": "external DNS required", "instruction": self.report["dns_required"]}
        self.flush()
        print("External DNS: create the reported A record with proxy disabled; waiting for DNS and certificate.", flush=True)

    def wait_until_ready(self) -> None:
        import httpx
        deadline = time.monotonic() + self.readiness_timeout
        previous = None
        while time.monotonic() < deadline:
            certificate = self.cloud_json("compute", "ssl-certificates", "describe", "mlr-genie-https", "--global")
            status = certificate.get("managed", {}).get("status", "UNKNOWN")
            if status != previous:
                print("Managed certificate status: " + status, flush=True)
                previous = status
                self.report["certificate_status"] = status
                self.flush()
            if status == "ACTIVE":
                try:
                    with httpx.Client(base_url=self.report["public_url"], timeout=25, follow_redirects=False) as client:
                        healthy = client.get("/health").status_code
                        cams = client.post("/integrations/cams/verify-source").status_code
                    if healthy == 200 and cams == 401:
                        self.report["checks"].update({"managed_certificate_active": True, "public_health_status": healthy, "tokenless_cams_status": cams})
                        return
                except httpx.HTTPError:
                    pass
            time.sleep(30)
        raise DeploymentError("Public HTTPS readiness deadline expired; check DNS A/proxy settings, certificate status and Cloud Run startup.")

    def integration_eval(self, bootstrap: str) -> dict:
        env = {**os.environ, "MLR_GENIE_BOOTSTRAP_SECRET": bootstrap}
        result = self.runner.run([sys.executable, "evals/live_integrations.py", "--base-url", self.report["public_url"]], env=env, timeout=1800)
        match = re.search(r"report:\s*(.+/result\.json)\s*$", result.stdout)
        if not match:
            raise DeploymentError("Integration evaluation did not produce an evidence report.")
        evidence = json.loads(Path(match.group(1)).read_text())
        if evidence.get("execution", {}).get("entry_point") != self.report["public_url"] or evidence.get("health_status") != 200:
            raise DeploymentError("Integration evidence does not match this public deployment.")
        self.write("integrations.json", evidence)
        self.report["checks"]["external_typescript_integrations"] = True
        return evidence

    def journal_eval(self, bootstrap: str, workspace: Path) -> None:
        import httpx
        pdf_path = workspace / "ioannidis-2005.pdf"
        with httpx.Client(timeout=90, follow_redirects=True) as client:
            response = client.get(PAPER_URL)
            response.raise_for_status()
        data = response.content
        if hashlib.sha256(data).hexdigest() != PAPER_SHA256:
            raise DeploymentError("Publisher journal PDF hash differs from the independently verified source.")
        pdf_path.write_bytes(data)
        report_dir = workspace / "journal-evaluation"
        self.runner.run([sys.executable, "evals/live_journal_verification.py", "--base-url", self.report["public_url"],
                         "--reference-pdf", str(pdf_path), "--claim", PAPER_CLAIM,
                         "--source-url", "https://pubmed.ncbi.nlm.nih.gov/16060722/", "--report-dir", str(report_dir)],
                        env={**os.environ, "MLR_GENIE_BOOTSTRAP_SECRET": bootstrap}, timeout=900)
        self.write("journal/result.json", json.loads((report_dir / "result.json").read_text()))
        self.report["checks"]["real_journal_live_verification"] = True

    def service_token(self, client, bootstrap: str) -> str:
        response = client.post("/auth/token", headers={"X-MLR-Bootstrap-Secret": bootstrap},
                               json={"tenant_id": "synthetic-integrations", "actor_id": "deployment-persistence-check", "role": "service"})
        if response.status_code != 200:
            raise DeploymentError(f"Persistence service-token issuance returned HTTP {response.status_code}.")
        result = response.json()
        token = result.get("access_token")
        if not token or result.get("role") != "service":
            raise DeploymentError("Persistence check did not receive the requested service token.")
        self.sanitizer.add_secret(token)
        return token

    def snapshot(self, evidence: dict, bootstrap: str) -> dict:
        import httpx
        roundtrip = evidence["roundtrip"]
        job_id = roundtrip["job_id"]
        with httpx.Client(base_url=self.report["public_url"], timeout=120, follow_redirects=False) as client:
            token = self.service_token(client, bootstrap)
            headers = {"Authorization": "Bearer " + token}
            response = client.get("/jobs/" + quote(job_id, safe=""), headers=headers)
            if response.status_code != 200:
                raise DeploymentError(f"Persisted job lookup returned HTTP {response.status_code}.")
            job = response.json()["job"]
            provenance = job.get("metadata", {}).get("accepted_comment_authorizations", {})
            if (provenance.get("medical", {}).get("actor") != "synthetic-reviewer"
                    or provenance.get("medical", {}).get("role") != "reviewer"
                    or not any(comment.get("human_actor") == "synthetic-reviewer" for comment in job.get("comments", []))):
                raise DeploymentError("Persisted job lost individual reviewer acceptance provenance.")
            artifacts = {}
            expected = {"clean_revised.docx", "redline.docx", "audit_summary.pdf", "annotated_source.pdf"}
            if set(roundtrip["artifacts"]) != expected:
                raise DeploymentError("Persistence verification requires exactly all four integration artifacts.")
            for name, metadata in roundtrip["artifacts"].items():
                path = metadata.get("download_url", "")
                expected_path = f"/integrations/commsrx/jobs/{quote(job_id, safe='')}/artifacts/{name}"
                if path != expected_path or urlsplit(path).netloc:
                    raise DeploymentError("Refusing unexpected integration artifact download URL.")
                response = client.get(path, headers=headers)
                if response.status_code != 200:
                    raise DeploymentError(f"Persistence artifact download returned HTTP {response.status_code}.")
                digest = hashlib.sha256(response.content).hexdigest()
                if digest != metadata["sha256"]:
                    raise DeploymentError("Persisted integration artifact failed its SHA-256 check.")
                artifacts[name] = digest
        job_hash = hashlib.sha256(json.dumps(job, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"job_id": job_id, "job_sha256": job_hash, "artifact_sha256": artifacts, "acceptance_provenance": provenance}

    def revision_persistence(self, evidence: dict, bootstrap: str) -> None:
        before = self.snapshot(evidence, bootstrap)
        service = self.cloud_json("run", "services", "describe", "mlr-genie-adk-app", "--region=" + self.target.region)
        old_revision = service.get("status", {}).get("latestReadyRevisionName")
        before_spec = service.get("spec", {}).get("template", {}).get("spec")
        if not old_revision or not before_spec:
            raise DeploymentError("Cloud Run did not return the original serving revision and configuration.")
        suffix = "persist-" + self.report["commit"][:8] + "-" + secrets.token_hex(3)
        self.gcloud("run", "services", "update", "mlr-genie-adk-app", "--region=" + self.target.region,
                    "--image=" + self.report["image"], "--revision-suffix=" + suffix, timeout=1200)
        self.gcloud("run", "services", "update-traffic", "mlr-genie-adk-app", "--region=" + self.target.region, "--to-latest", timeout=300)
        service = self.cloud_json("run", "services", "describe", "mlr-genie-adk-app", "--region=" + self.target.region)
        new_revision = service.get("status", {}).get("latestReadyRevisionName")
        if new_revision == old_revision or service.get("spec", {}).get("template", {}).get("spec") != before_spec:
            raise DeploymentError("Persistence revision did not preserve the application's desired runtime configuration.")
        traffic = service.get("status", {}).get("traffic", [])
        if sum(item.get("percent", 0) for item in traffic if item.get("revisionName") == new_revision) != 100:
            raise DeploymentError("The new persistence-check revision does not receive 100 percent of traffic.")
        time.sleep(30)
        after = self.snapshot(evidence, bootstrap)
        if before != after:
            raise DeploymentError("Job, artifact hashes or reviewer provenance changed across Cloud Run revisions.")
        self.write("persistence.json", {"checked_at": self.now(), "old_revision": old_revision,
                                        "new_revision": new_revision, "service_role": "service", "passed": True, **after})
        self.report["checks"]["cross_revision_postgres_gcs_persistence"] = True
        self.report["serving_revision"] = new_revision

    def run(self) -> None:
        try:
            with self.phase("verify exact project and public hostname"):
                self.verify_target()
                self.resolve_host()
            with self.phase("enable required APIs and verify private Terraform state bucket"):
                self.gcloud("services", "enable", *SERVICES, timeout=900)
                self.ensure_state_bucket()
            with self.phase("build and push immutable production image"):
                image = self.build_image()
            with tempfile.TemporaryDirectory(prefix="mlr-genie-deploy-") as temporary:
                workspace = Path(temporary)
                variables = workspace / "production.tfvars.json"
                variables.write_text(json.dumps({"project_id": self.target.project_id, "region": self.target.region,
                                                 "public_host": self.target.public_host, "app_image": image}))
                with self.phase("initialize GCS Terraform backend and allocate public IP"):
                    self.runner.run([*self.tf, "init", "-input=false", "-no-color", "-reconfigure",
                                     "-backend-config=bucket=" + self.target.state_bucket,
                                     "-backend-config=prefix=mlr-genie/production"], timeout=600)
                    self.plan_apply("public-ip", workspace, variables, ("google_compute_global_address.mlr_genie_https",))
                    self.publish_ip()
                    self.configure_dns_if_authoritative()
                with self.phase("provision database, runtime identity, private storage and secret containers"):
                    self.plan_apply("bootstrap", workspace, variables, BOOTSTRAP_TARGETS)
                    self.ensure_secret("mlr-genie-session-signing")
                    self.ensure_secret("mlr-genie-bootstrap")
                    self.gcloud("iam", "service-accounts", "add-iam-policy-binding", self.target.runtime_sa,
                                "--member=serviceAccount:" + self.target.deployer_sa,
                                "--role=roles/iam.serviceAccountUser", "--condition=None")
                with self.phase("bootstrap PostgreSQL schema through temporary scoped SQL import"):
                    self.bootstrap_database(workspace)
                with self.phase("apply complete guarded production Terraform plan"):
                    self.plan_apply("production", workspace, variables)
                    self.publish_ip()
                with self.phase("wait for managed certificate and protected public HTTPS endpoint"):
                    self.wait_until_ready()
                bootstrap = self.read_bootstrap_secret()
                with self.phase("verify live TypeScript CAMS and CommsRx roundtrip"):
                    evidence = self.integration_eval(bootstrap)
                with self.phase("verify public journal PDF against live Vertex AI"):
                    self.journal_eval(bootstrap, workspace)
                with self.phase("verify durable job, four artifacts and reviewer provenance across revisions"):
                    self.revision_persistence(evidence, bootstrap)
            self.report["status"] = "passed"
            self.report["finished_at"] = self.now()
            self._publish_public_ip_status(state="success")
            self.flush()
            print("Production deployment and live verification passed: " + self.report["public_url"], flush=True)
        except Exception as exc:
            self.report.update({"status": "failed", "failed_stage": self.stage,
                                "failure": self.sanitizer.clean(str(exc)), "finished_at": self.now()})
            self.flush()
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("deploy/automation/target.json"))
    parser.add_argument("--readiness-timeout", type=int, default=7200, help="Seconds to wait for DNS/certificate/HTTPS (default: 7200).")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    output = root / "deployment-output"
    deployment = None
    try:
        if not 30 <= args.readiness_timeout <= 14400:
            raise ValueError("--readiness-timeout must be 30–14400 seconds.")
        config = args.config if args.config.is_absolute() else root / args.config
        target = Target.from_dict(json.loads(config.read_text()))
        deployment = Deployment(target, root, output, readiness_timeout=args.readiness_timeout)
        deployment.run()
    except Exception as exc:
        message = deployment.sanitizer.clean(str(exc)) if deployment else "Invalid or unavailable deployment target configuration."
        if deployment is None:
            output.mkdir(parents=True, exist_ok=True)
            (output / "report.json").write_text(json.dumps({"status": "failed", "failed_stage": "configuration", "failure": message}, indent=2) + "\n")
        print("Deployment failed: " + message, file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
