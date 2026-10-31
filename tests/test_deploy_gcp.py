"""Cloud-free tests for the deployment driver's irreversible-action guards."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


_DRIVER_PATH = Path(__file__).resolve().parents[1] / "deploy/automation/deploy_gcp.py"
_SPEC = importlib.util.spec_from_file_location("mlr_genie_deploy_gcp", _DRIVER_PATH)
assert _SPEC and _SPEC.loader
_driver = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _driver
_SPEC.loader.exec_module(_driver)


def target_data(**changes):
    return {
        "project_id": "commsrx-ai", "project_number": "333570013714",
        "repo_full_name": "example/MLR-Genie", **changes,
    }


def test_target_preserves_identity_and_uses_private_state_name():
    target = _driver.Target.from_dict(target_data())
    assert target.project_id == "commsrx-ai"
    assert str(target.project_number) == "333570013714"
    assert target.region == "us-central1"
    assert target.inference_location == "us-central1"
    assert target.allow_global_inference is False
    assert target.public_host == ""
    assert target.state_bucket == "mlr-genie-tfstate-333570013714"


@pytest.mark.parametrize("changes", [
    {"project_id": ""}, {"project_id": "../other-project"},
    {"project_number": ""}, {"project_number": "333570013714;echo"},
    {"repo_full_name": "example"}, {"repo_full_name": "../../wrong"},
    {"public_host": "https://mlr.example.com"},
    {"public_host": "mlr.example.com/path"},
    {"public_host": "mlr.example.com:443"},
])
def test_target_rejects_ambiguous_or_executable_identifiers(changes):
    with pytest.raises((ValueError, RuntimeError)):
        _driver.Target.from_dict(target_data(**changes))


@pytest.mark.parametrize("location", ["us", "eu", "us-central1", "europe-west1"])
def test_inference_location_is_separate_from_resource_region(location):
    target = _driver.Target.from_dict(target_data(vertex_ai_location=location))
    assert target.inference_location == location
    assert target.region == "us-central1"


@pytest.mark.parametrize("location", ["US", "us/other", "https://example.com", "other"])
def test_inference_location_rejects_global_and_non_location_values(location):
    with pytest.raises(ValueError, match="vertex_ai_location must"):
        _driver.Target.from_dict(target_data(vertex_ai_location=location))


def test_global_inference_requires_explicit_approval_and_preserves_resource_region(tmp_path):
    with pytest.raises(ValueError, match="requires explicit allow_global_inference=true"):
        _driver.Target.from_dict(target_data(vertex_ai_location="global"))
    target = _driver.Target.from_dict(target_data(vertex_ai_location="global", allow_global_inference=True))
    assert target.inference_location == "global" and target.region == "us-central1"
    current = _driver.Deployment(target, tmp_path, tmp_path / "evidence", runner=FakeRunner(lambda *args: None))
    assert current.report["allow_global_inference"] is True
    assert current.report["vertex_ai_location"] == "global"


@pytest.mark.parametrize("value", [None, "true", "false", 0, 1, [], {}])
def test_global_inference_approval_requires_a_strict_json_boolean(value):
    with pytest.raises(ValueError, match="must be a JSON boolean"):
        _driver.Target.from_dict(target_data(vertex_ai_location="global", allow_global_inference=value))


def test_project_guard_requires_exact_id_number_and_active_state():
    target = _driver.Target.from_dict(target_data())
    actual = {"projectId": "commsrx-ai", "projectNumber": "333570013714", "lifecycleState": "ACTIVE"}
    _driver.validate_project_identity(target, actual)
    for changed in (
        {"projectId": "another-project"},
        {"projectNumber": "333570013715"},
        {"lifecycleState": "DELETE_REQUESTED"},
        {"lifecycleState": ""},
    ):
        with pytest.raises((ValueError, RuntimeError)):
            _driver.validate_project_identity(target, {**actual, **changed})


def test_project_guard_rejects_missing_identity():
    with pytest.raises((ValueError, RuntimeError)):
        _driver.validate_project_identity(_driver.Target.from_dict(target_data()), {})


def plan(*actions):
    return {"resource_changes": [
        {"address": f"google_storage_bucket.sample_{index}", "change": {"actions": change}}
        for index, change in enumerate(actions)
    ]}


def test_plan_allows_nondestructive_changes():
    _driver.validate_plan(plan(["create"], ["update"], ["no-op"], ["read"]))


@pytest.mark.parametrize("actions", [["delete"], ["delete", "create"], ["create", "delete"]])
def test_plan_rejects_destroy_and_both_replacement_orders(actions):
    with pytest.raises((ValueError, RuntimeError)):
        _driver.validate_plan(plan(["update"], actions))


def test_sql_identifier_quotes_embedded_quotes_and_rejects_nul():
    assert _driver.quote_identifier('runtime"role') == '"runtime""role"'
    with pytest.raises((ValueError, RuntimeError)):
        _driver.quote_identifier("runtime\x00role")


def test_bootstrap_is_plain_sql_scoped_to_mlr_genie_and_quoted_iam_role():
    sql = _driver.bootstrap_sql('runtime"role@commsrx-ai.iam')
    assert "\\set" not in sql and "\\if" not in sql
    assert "current_database()" in sql and "mlr_genie" in sql
    assert '"runtime""role@commsrx-ai.iam"' in sql
    assert "CREATE SCHEMA IF NOT EXISTS mlr_genie;" in sql
    assert "AUTHORIZATION" not in sql
    assert "ALTER SCHEMA" not in sql
    assert "SET ROLE" not in sql
    assert "GRANT CONNECT ON DATABASE mlr_genie" in sql
    assert "GRANT USAGE, CREATE ON SCHEMA mlr_genie" in sql
    assert "PASSWORD" not in sql.upper()


def test_recursive_sanitizer_removes_credentials_from_values_and_sensitive_fields():
    sanitizer = _driver.Sanitizer()
    sanitizer.add_secret("secret-value-that-must-not-leak")
    dirty = {
        "error": "failed with secret-value-that-must-not-leak",
        "nested": [{"access_token": "unknown-access-token", "bootstrap_secret": "unknown-bootstrap"}],
        "public_url": "https://mlr.example.com",
    }
    clean = sanitizer.clean(dirty)
    serialized = json.dumps(clean)
    assert "secret-value-that-must-not-leak" not in serialized
    assert "unknown-access-token" not in serialized
    assert "unknown-bootstrap" not in serialized
    assert clean["public_url"] == "https://mlr.example.com"
    assert dirty["error"].endswith("secret-value-that-must-not-leak")


class FakeRunner:
    """A bounded command script: every unanticipated command fails the test."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        result = self.handler(args, kwargs)
        if isinstance(result, Exception):
            raise result
        return result


def completed(args, *, data=None, stdout="", stderr="", code=0):
    return _driver.subprocess.CompletedProcess(
        args, code, stdout=json.dumps(data) if data is not None else stdout, stderr=stderr,
    )


def deployment(tmp_path, runner, **target_changes):
    return _driver.Deployment(
        _driver.Target.from_dict(target_data(**target_changes)),
        tmp_path, tmp_path / "evidence", runner=runner,
    )


def test_runner_passes_argv_stdin_environment_and_bounded_timeout_without_shell(monkeypatch, tmp_path, capsys):
    captured = {}

    def fake_subprocess(args, **kwargs):
        captured.update(args=args, **kwargs)
        return completed(args, stdout="ok")

    monkeypatch.setattr(_driver.subprocess, "run", fake_subprocess)
    sanitizer = _driver.Sanitizer()
    sanitizer.add_secret("secret-in-stdin")
    runner = _driver.Runner(tmp_path, sanitizer)
    args = ["gcloud", "secrets", "versions", "add", "bootstrap", "--data-file=-"]
    result = runner.run(args, input_text="secret-in-stdin", env={"SAFE_SELECTOR": "adc-path"},
                        sensitive=True, timeout=47)
    assert result.stdout == "ok"
    assert captured["args"] == args
    assert captured["shell"] is False
    assert captured["input"] == "secret-in-stdin"
    assert captured["env"] == {"SAFE_SELECTOR": "adc-path"}
    assert captured["timeout"] == 47
    assert captured["capture_output"] is True
    assert captured["check"] is False
    assert "secret-in-stdin" not in " ".join(captured["args"])
    assert "secret-in-stdin" not in capsys.readouterr().out
    with pytest.raises(TypeError):
        runner.run("gcloud secrets list")


def test_runner_redacts_failure_and_withholds_sensitive_output(monkeypatch, tmp_path):
    sanitizer = _driver.Sanitizer()
    sanitizer.add_secret("known-private-value")
    runner = _driver.Runner(tmp_path, sanitizer)
    monkeypatch.setattr(_driver.subprocess, "run", lambda args, **kwargs: completed(
        args, code=1, stderr="known-private-value and Bearer unknown-private-value",
    ))
    with pytest.raises(_driver.DeploymentError) as failure:
        runner.run(["gcloud", "describe"])
    assert "known-private-value" not in str(failure.value)
    assert "unknown-private-value" not in str(failure.value)
    assert "[REDACTED]" in str(failure.value)
    with pytest.raises(_driver.DeploymentError) as failure:
        runner.run(["gcloud", "secrets", "versions", "access", "latest"], sensitive=True)
    assert "Sensitive command output withheld" in str(failure.value)
    assert "known-private-value" not in str(failure.value)


def test_runner_timeout_does_not_repeat_arguments_or_child_output(monkeypatch, tmp_path):
    def timeout(args, **kwargs):
        raise _driver.subprocess.TimeoutExpired(args, 8, output="secret-output", stderr="secret-stderr")

    monkeypatch.setattr(_driver.subprocess, "run", timeout)
    with pytest.raises(_driver.DeploymentError) as failure:
        _driver.Runner(tmp_path, _driver.Sanitizer()).run(["gcloud", "secret-argument"], timeout=8)
    message = str(failure.value)
    assert "8s" in message
    assert "secret-output" not in message and "secret-stderr" not in message and "secret-argument" not in message


@pytest.mark.parametrize("actions", [["delete"], ["create", "delete"], ["update"]])
def test_plan_apply_never_applies_destructive_or_cross_project_plans(tmp_path, actions):
    def handler(args, kwargs):
        if "plan" in args:
            return completed(args)
        if "show" in args:
            return completed(args, data={"resource_changes": [{
                "address": "google_storage_bucket.existing",
                "change": {"actions": actions, "after": {"project": "unrelated-project"}},
            }]})
        raise AssertionError(f"Unexpected command, including forbidden apply: {args}")

    runner = FakeRunner(handler)
    current = deployment(tmp_path, runner)
    variables = tmp_path / "variables.json"
    variables.write_text("{}")
    with pytest.raises(_driver.DeploymentError):
        current.plan_apply("production", tmp_path, variables)
    assert not any("apply" in args for args, kwargs in runner.calls)


@pytest.mark.parametrize("project", ["another-project", "333570013715"])
def test_plan_rejects_other_project_id_or_number(project):
    with pytest.raises(_driver.DeploymentError, match="cross-project"):
        _driver.validate_plan({"resource_changes": [{
            "address": "google_project_iam_member.runtime",
            "change": {"actions": ["create"], "after": {"project": project}},
        }]}, _driver.Target.from_dict(target_data()))


def test_state_bucket_from_another_project_is_not_updated(tmp_path):
    def handler(args, kwargs):
        if args[1:4] == ["storage", "buckets", "describe"]:
            return completed(args, data={"projectNumber": "333570013715"})
        raise AssertionError(f"Foreign bucket must not be changed: {args}")

    runner = FakeRunner(handler)
    with pytest.raises(_driver.DeploymentError, match="project number"):
        deployment(tmp_path, runner).ensure_state_bucket()
    assert len(runner.calls) == 1


@pytest.mark.parametrize("diagnostic", ["403 Permission denied", "503 Service unavailable", "connection reset"])
def test_state_bucket_lookup_failure_never_implies_absence_or_creates_bucket(tmp_path, diagnostic):
    def handler(args, kwargs):
        assert args[1:4] == ["storage", "buckets", "describe"]
        return completed(args, code=1, stderr=diagnostic)

    runner = FakeRunner(handler)
    with pytest.raises(_driver.DeploymentError, match="no bucket creation attempted"):
        deployment(tmp_path, runner).ensure_state_bucket()
    assert len(runner.calls) == 1


def test_state_bucket_is_created_only_after_not_found_and_reverified(tmp_path):
    metadata = {"projectNumber": "333570013714", "versioning": {"enabled": True},
                "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True},
                                     "publicAccessPrevention": "enforced"}}
    describes = 0

    def handler(args, kwargs):
        nonlocal describes
        if args[1:4] == ["storage", "buckets", "describe"]:
            describes += 1
            if describes == 1:
                return completed(args, code=1, stderr="404 Not Found")
            return completed(args, data=metadata)
        if args[1:4] in (["storage", "buckets", "create"], ["storage", "buckets", "update"]):
            assert "--uniform-bucket-level-access" in args
            assert "--public-access-prevention" in args
            return completed(args)
        raise AssertionError(f"Unexpected command: {args}")

    runner = FakeRunner(handler)
    current = deployment(tmp_path, runner)
    current.ensure_state_bucket()
    assert describes == 3
    assert current.report["state_bucket"] == "mlr-genie-tfstate-333570013714"
    assert all("--project=commsrx-ai" in args for args, kwargs in runner.calls)


def test_missing_host_and_public_zone_fails_before_any_paid_resources(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    monkeypatch.delenv("GITHUB_SHA", raising=False)

    def handler(args, kwargs):
        if args[:3] == ["gcloud", "projects", "describe"]:
            return completed(args, data={"projectId": "commsrx-ai", "projectNumber": "333570013714", "lifecycleState": "ACTIVE"})
        if args == ["git", "rev-parse", "HEAD"]:
            return completed(args, stdout="a" * 40 + "\n")
        if args[:3] == ["gcloud", "auth", "list"]:
            return completed(args, stdout="mlr-genie-deployer@commsrx-ai.iam.gserviceaccount.com\n")
        if args[:4] == ["gcloud", "dns", "managed-zones", "list"]:
            return completed(args, data=[])
        raise AssertionError(f"No resource-changing command is allowed before hostname resolution: {args}")

    runner = FakeRunner(handler)
    current = deployment(tmp_path, runner)
    with pytest.raises(_driver.DeploymentError, match="controlled public_host"):
        current.run()
    report = json.loads((tmp_path / "evidence/report.json").read_text())
    assert report["status"] == "failed"
    assert report["failed_stage"] == "verify exact project and public hostname"
    assert "No paid deployment resources created" in report["dns_required"]
    assert not any(args[:3] == ["gcloud", "services", "enable"] for args, kwargs in runner.calls)


def test_sql_bootstrap_removes_uploaded_object_when_generation_lookup_fails(tmp_path):
    uploaded_uri = None

    def handler(args, kwargs):
        nonlocal uploaded_uri
        if "terraform" == args[0] and "output" in args:
            return completed(args, data={"database_iam_role": {
                "value": "mlr-genie-adk-runtime@commsrx-ai.iam",
            }})
        if args[:4] == ["gcloud", "sql", "instances", "describe"]:
            return completed(args, data={"project": "commsrx-ai",
                "serviceAccountEmailAddress": "p333570013714-abcd@gcp-sa-cloud-sql.iam.gserviceaccount.com"})
        if args[:3] == ["gcloud", "storage", "cp"]:
            uploaded_uri = args[4]
            assert uploaded_uri.startswith("gs://mlr-genie-tfstate-333570013714/bootstrap/")
            assert "--if-generation-match=0" in args
            return completed(args)
        if args[:4] == ["gcloud", "storage", "objects", "describe"]:
            raise _driver.DeploymentError("Object metadata unavailable")
        if args[:3] == ["gcloud", "storage", "rm"]:
            assert uploaded_uri in args
            return completed(args)
        raise AssertionError(f"Unexpected command during failed SQL upload: {args}")

    runner = FakeRunner(handler)
    current = deployment(tmp_path, runner)
    current.report["commit"] = "b" * 40
    with pytest.raises(_driver.DeploymentError, match="Object metadata unavailable"):
        current.bootstrap_database(tmp_path)
    assert uploaded_uri is not None
    assert any(args[:3] == ["gcloud", "storage", "rm"] and uploaded_uri in args for args, kwargs in runner.calls)
    assert not any(args[:3] == ["gcloud", "sql", "import"] for args, kwargs in runner.calls)


def test_secret_creation_uses_private_stdin_only_when_no_version_exists(tmp_path):
    added = []

    def handler(args, kwargs):
        if args[:4] == ["gcloud", "secrets", "versions", "list"]:
            return completed(args, data=[])
        if args[:4] == ["gcloud", "secrets", "versions", "add"]:
            value = kwargs.get("input_text")
            assert value and len(value) >= 64
            assert value not in " ".join(args)
            assert "--data-file=-" in args
            assert kwargs.get("sensitive") is True
            added.append(value)
            return completed(args)
        raise AssertionError(f"Unexpected secret command: {args}")

    runner = FakeRunner(handler)
    current = deployment(tmp_path, runner)
    current.ensure_secret("mlr-genie-bootstrap")
    assert len(added) == 1
    assert added[0] not in current.sanitizer.clean("value=" + added[0])


@pytest.mark.parametrize("versions,latest_state,should_fail", [
    ([{"state": "ENABLED"}], "ENABLED", False),
    ([{"state": "DISABLED"}, {"state": "ENABLED"}], "ENABLED", False),
    ([{"state": "DISABLED"}], "DISABLED", True),
    ([{"state": "DESTROYED"}], "DESTROYED", True),
    ([{"name": "versions/1", "state": "ENABLED"},
      {"name": "versions/2", "state": "DISABLED"}], "DISABLED", True),
])
def test_secret_versions_are_never_automatically_rotated(tmp_path, versions, latest_state, should_fail):
    def handler(args, kwargs):
        if args[:4] == ["gcloud", "secrets", "versions", "list"]:
            return completed(args, data=versions)
        assert args[:5] == ["gcloud", "secrets", "versions", "describe", "latest"]
        assert "--secret=mlr-genie-session-signing" in args
        return completed(args, data={"state": latest_state})

    runner = FakeRunner(handler)
    current = deployment(tmp_path, runner)
    if should_fail:
        with pytest.raises(_driver.DeploymentError, match="refusing automatic rotation"):
            current.ensure_secret("mlr-genie-session-signing")
    else:
        current.ensure_secret("mlr-genie-session-signing")
    assert len(runner.calls) == 2
    assert not any(args[:4] == ["gcloud", "secrets", "versions", "add"] for args, kwargs in runner.calls)


def test_report_writer_redacts_registered_secrets_and_token_fields(tmp_path):
    runner = FakeRunner(lambda args, kwargs: (_ for _ in ()).throw(AssertionError("No commands expected")))
    current = deployment(tmp_path, runner)
    current.sanitizer.add_secret("private-bootstrap-secret")
    current.report.update({"failure": "oops private-bootstrap-secret", "access_token": "unregistered-token"})
    current.flush()
    written = (tmp_path / "evidence/report.json").read_text()
    assert "private-bootstrap-secret" not in written and "unregistered-token" not in written
    report = json.loads(written)
    assert report["project_id"] == "commsrx-ai"
    assert report["failure"] == "oops [REDACTED]"


@pytest.mark.parametrize("change,should_fail", [
    ({}, False),
    ({"image": "us-central1-docker.pkg.dev/unrelated-project/mlr-genie/app"}, True),
    ({"tag": "projects/commsrx-ai/locations/us-central1/repositories/mlr-genie/packages/app/tags/other-commit"}, True),
    ({"version": "projects/unrelated-project/locations/us-central1/repositories/mlr-genie/packages/app/versions/sha256:" + "c" * 64}, True),
    ({"version": "projects/commsrx-ai/locations/us-central1/repositories/mlr-genie/packages/other-app/versions/sha256:" + "c" * 64}, True),
    ({"version": "projects/commsrx-ai/locations/us-central1/repositories/mlr-genie/packages/app/versions/sha256:not-a-digest"}, True),
])
def test_build_resolves_only_exact_project_package_and_sha_tag_to_immutable_digest(tmp_path, change, should_fail):
    commit = "d" * 40
    repository = "projects/commsrx-ai/locations/us-central1/repositories/mlr-genie"
    package = repository + "/packages/app"
    image_path = "us-central1-docker.pkg.dev/commsrx-ai/mlr-genie/app"
    digest = "sha256:" + "c" * 64
    tag = {"tag": package + "/tags/" + commit,
           "image": image_path, "version": package + "/versions/" + digest, **change}

    def handler(args, kwargs):
        if args[:4] == ["gcloud", "artifacts", "repositories", "list"]:
            assert "--location=us-central1" in args
            return completed(args, data=[{"name": repository, "format": "DOCKER"}])
        if args[:3] == ["gcloud", "auth", "configure-docker"]:
            assert args[3] == "us-central1-docker.pkg.dev"
            return completed(args)
        if args[:2] == ["docker", "build"]:
            assert "--platform=linux/amd64" in args
            assert args[args.index("--tag") + 1] == image_path + ":" + commit
            assert "org.opencontainers.image.revision=" + commit in args
            assert kwargs["timeout"] == 1800
            return completed(args)
        if args[:2] == ["docker", "push"]:
            assert args[2] == image_path + ":" + commit
            return completed(args)
        if args[:5] == ["gcloud", "artifacts", "docker", "tags", "list"]:
            assert args[5] == image_path
            return completed(args, data=[tag])
        raise AssertionError(f"Unexpected image command, including forbidden describe/create: {args}")

    runner = FakeRunner(handler)
    current = deployment(tmp_path, runner)
    current.report["commit"] = commit
    if should_fail:
        with pytest.raises(_driver.DeploymentError, match="immutable manifest digest"):
            current.build_image()
        assert "image" not in current.report and "image_digest" not in current.report
    else:
        assert current.build_image() == image_path + "@" + digest
        assert current.report["image"] == image_path + "@" + digest
        assert current.report["image_digest"] == digest
    assert len(runner.calls) == 5


def public_ip_deployment(tmp_path, monkeypatch, *, host="mlr-genie.example.com", token="github-private-token"):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    if token:
        monkeypatch.setenv("GITHUB_TOKEN", token)
    else:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    runner = FakeRunner(lambda args, kwargs: completed(args, data={"public_ip": {"value": "34.98.18.15"}}))
    current = deployment(tmp_path, runner, public_host=host)
    current.report.update({"commit": "e" * 40, "public_ip": "34.98.18.15"})
    return current


class PrivateStatusResponse:
    """Bodies and headers must never be read or retained by publication code."""

    def __init__(self, status_code):
        self.status_code = status_code

    @property
    def text(self):
        raise AssertionError("Do not inspect an HTTP failure body")

    @property
    def headers(self):
        raise AssertionError("Do not inspect or record HTTP headers")


def mock_status_client(monkeypatch, *, status=201, error=None):
    import httpx
    calls = []
    settings = []

    class FakeClient:
        def __init__(self, **kwargs):
            settings.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, endpoint, **kwargs):
            calls.append((endpoint, kwargs))
            if error:
                raise error
            return PrivateStatusResponse(status)

    monkeypatch.setattr(httpx, "Client", FakeClient)
    return settings, calls


@pytest.mark.parametrize("state", ["pending", "success"])
@pytest.mark.parametrize("host", [
    "mlr-genie.example.com",
    "mlr-genie." + "a" * 60 + "." + "b" * 60 + "." + "c" * 30 + ".example.com",
])
def test_public_ip_commit_status_has_exact_identity_private_auth_and_https(tmp_path, monkeypatch, state, host):
    current = public_ip_deployment(tmp_path, monkeypatch, host=host)
    settings, calls = mock_status_client(monkeypatch)
    current._publish_public_ip_status(state=state)
    assert settings == [{"timeout": 20, "follow_redirects": False, "verify": True}]
    assert len(calls) == 1
    endpoint, request = calls[0]
    assert endpoint == "https://api.github.com/repos/example/MLR-Genie/statuses/" + "e" * 40
    assert request["headers"]["Authorization"] == "Bearer github-private-token"
    assert request["json"]["context"] == "mlr-genie/public-ip"
    assert request["json"]["state"] == state
    assert request["json"]["target_url"] == "https://" + host
    description = request["json"]["description"]
    assert description.startswith("A ") and description.endswith(" -> 34.98.18.15")
    assert len(description) <= 140
    assert ("..." in description) == (len(host) > 140 - len("A  -> 34.98.18.15"))
    report = json.loads((tmp_path / "evidence/report.json").read_text())
    assert report["public_ip_status"]["state"] == state
    assert "github-private-token" not in json.dumps(report)
    assert "Authorization" not in json.dumps(report)


@pytest.mark.parametrize("failure_kind", ["status", "network", "missing-token"])
def test_public_ip_status_failures_are_generic_and_print_exact_cloud_shell_fallback(tmp_path, monkeypatch, capsys, failure_kind):
    import httpx
    token = "github-private-token" if failure_kind != "missing-token" else ""
    current = public_ip_deployment(tmp_path, monkeypatch, token=token)
    error = httpx.ConnectError("reflected-private-detail github-private-token") if failure_kind == "network" else None
    settings, calls = mock_status_client(monkeypatch, status=403, error=error)
    current._publish_public_ip_status()
    report_text = (tmp_path / "evidence/report.json").read_text()
    report = json.loads(report_text)
    channel = report["public_ip_status"]
    assert channel["state"] == "unavailable"
    lookup = "gcloud compute addresses describe mlr-genie-https --global --project=commsrx-ai --format='value(address)'"
    assert channel["cloud_shell_lookup"] == lookup
    captured = capsys.readouterr()
    assert lookup in captured.out
    evidence = report_text + captured.out + captured.err
    assert "github-private-token" not in evidence and "reflected-private-detail" not in evidence
    assert "Authorization" not in evidence and "headers" not in evidence
    assert len(calls) == (0 if failure_kind == "missing-token" else 1)
    assert current.report["status"] == "running"


def test_github_token_is_registered_before_any_command_or_publication(tmp_path, monkeypatch):
    current = public_ip_deployment(tmp_path, monkeypatch)
    assert current.sanitizer.clean("github-private-token") == "[REDACTED]"
    assert not current.runner.calls


def test_public_ip_status_is_skipped_outside_github_actions(tmp_path, monkeypatch):
    current = public_ip_deployment(tmp_path, monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    settings, calls = mock_status_client(monkeypatch)
    current._publish_public_ip_status()
    assert not settings and not calls
    assert "public_ip_status" not in current.report


def test_every_public_ip_publication_also_publishes_commit_status(tmp_path, monkeypatch):
    current = public_ip_deployment(tmp_path, monkeypatch)
    settings, calls = mock_status_client(monkeypatch)
    current.publish_ip()
    current.publish_ip()
    assert len(calls) == 2
    assert all(request["json"]["state"] == "pending" for endpoint, request in calls)
    assert current.report["public_ip"] == "34.98.18.15"
    assert json.loads((tmp_path / "evidence/report.json").read_text())["public_ip_status"]["state"] == "pending"


def cloud_run_access_deployment(tmp_path, *, annotations=None, url="https://mlr-genie-abc-uc.a.run.app"):
    def handler(args, kwargs):
        assert args[:5] == ["gcloud", "run", "services", "describe", "mlr-genie-adk-app"]
        assert "--region=us-central1" in args and "--project=commsrx-ai" in args
        return completed(args, data={"metadata": {"annotations": annotations if annotations is not None else {
            "run.googleapis.com/ingress": "internal-and-cloud-load-balancing",
            "run.googleapis.com/invoker-iam-disabled": "true",
        }}, "status": {"url": url}})
    return deployment(tmp_path, FakeRunner(handler))


def mock_direct_cloud_run_client(monkeypatch, status=404, error=None):
    import httpx
    calls, settings = [], []
    class FakeClient:
        def __init__(self, **kwargs):
            settings.append(kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            if error:
                raise error
            return PrivateStatusResponse(status)
    monkeypatch.setattr(httpx, "Client", FakeClient)
    return settings, calls


@pytest.mark.parametrize("status", [403, 404])
def test_cloud_run_service_settings_and_direct_ingress_block_are_verified(tmp_path, monkeypatch, status):
    current = cloud_run_access_deployment(tmp_path)
    settings, calls = mock_direct_cloud_run_client(monkeypatch, status)
    current.verify_cloud_run_access()
    assert settings == [{"timeout": 25, "follow_redirects": False, "verify": True}]
    assert calls == [("https://mlr-genie-abc-uc.a.run.app/health", {})]
    report = json.loads((tmp_path / "evidence/report.json").read_text())
    assert report["checks"]["direct_cloud_run_ingress_blocked"] is True
    assert report["checks"]["direct_cloud_run_health_status"] == status
    assert report["cloud_run_access"]["invoker_iam_disabled"] is True


@pytest.mark.parametrize("url", [
    "https://mlr-genie-adk-app-333570013714.us-central1.run.app:443",
    "https://mlr-genie-abc-uc.a.run.app/",
])
def test_cloud_run_probe_accepts_both_service_url_formats_and_default_https_port(tmp_path, monkeypatch, url):
    current = cloud_run_access_deployment(tmp_path, url=url)
    settings, calls = mock_direct_cloud_run_client(monkeypatch)
    current.verify_cloud_run_access()
    assert calls == [(url.rstrip("/") + "/health", {})]


@pytest.mark.parametrize("status", [200, 301, 302, 401, 500])
def test_direct_cloud_run_success_redirect_or_unexpected_error_is_not_accepted(tmp_path, monkeypatch, status):
    current = cloud_run_access_deployment(tmp_path)
    mock_direct_cloud_run_client(monkeypatch, status)
    with pytest.raises(_driver.DeploymentError, match="must be blocked"):
        current.verify_cloud_run_access()
    report = json.loads((tmp_path / "evidence/report.json").read_text())
    assert report["cloud_run_access"]["direct_health_status"] == status
    assert "direct_cloud_run_ingress_blocked" not in report["checks"]


@pytest.mark.parametrize("annotations", [
    {}, {"run.googleapis.com/ingress": "all", "run.googleapis.com/invoker-iam-disabled": "true"},
    {"run.googleapis.com/ingress": "internal-and-cloud-load-balancing"},
    {"run.googleapis.com/ingress": "internal-and-cloud-load-balancing", "run.googleapis.com/invoker-iam-disabled": "false"},
])
def test_wrong_cloud_run_ingress_or_invoker_settings_fail_before_http(tmp_path, monkeypatch, annotations):
    current = cloud_run_access_deployment(tmp_path, annotations=annotations)
    settings, calls = mock_direct_cloud_run_client(monkeypatch)
    with pytest.raises(_driver.DeploymentError):
        current.verify_cloud_run_access()
    assert not settings and not calls


@pytest.mark.parametrize("url", [
    "http://service.run.app", "https://service.run.app:8443", "https://service.run.app:invalid",
    "https://user@service.run.app", "https://user:password@service.run.app", "https://@service.run.app",
    "https://service.run.app?x=1", "https://service.run.app?", "https://service.run.app#fragment",
    "https://service.run.app/other", "https://service.run.app.attacker.example", "https://run.app",
    "https://service.run.app ", "https://service.run.app:", "https://service.\nrun.app", "", None,
])
def test_cloud_run_probe_rejects_untrusted_or_noncanonical_service_urls(tmp_path, monkeypatch, url):
    current = cloud_run_access_deployment(tmp_path, url=url)
    settings, calls = mock_direct_cloud_run_client(monkeypatch)
    with pytest.raises(_driver.DeploymentError, match="valid HTTPS run.app"):
        current.verify_cloud_run_access()
    assert not settings and not calls


def test_direct_cloud_run_network_error_cannot_count_as_ingress_rejection(tmp_path, monkeypatch):
    import httpx
    current = cloud_run_access_deployment(tmp_path)
    mock_direct_cloud_run_client(monkeypatch, error=httpx.ConnectError("private network diagnostic"))
    with pytest.raises(_driver.DeploymentError, match="Could not verify") as error:
        current.verify_cloud_run_access()
    assert "private network diagnostic" not in str(error.value)
    assert "direct_cloud_run_ingress_blocked" not in current.report["checks"]


def runtime_probe_deployment(tmp_path, *, result_changes=None, identity=None, execution_error=False, cleanup_error=False,
                             inference_location=None, allow_global_inference=False):
    observed = {}
    probe_result = {
        "project_id": "commsrx-ai", "location": inference_location or "us-central1", "model": "gemini-3.5-flash-lite",
        "effective_model": "gemini-3.5-flash-lite", "status": "passed", "used_live_sdk": True,
        "provider_http_status": None, "error_message": "", **(result_changes or {}),
    }
    runtime = "mlr-genie-adk-runtime@commsrx-ai.iam.gserviceaccount.com"

    def handler(args, kwargs):
        if args[1:5] == ["run", "services", "describe", "mlr-genie-adk-app"]:
            return completed(args, data={"spec": {"template": {"spec": {
                "serviceAccountName": identity or runtime,
                "containers": [{"env": [
                    {"name": "GOOGLE_CLOUD_PROJECT", "value": "commsrx-ai"},
                    {"name": "GOOGLE_CLOUD_LOCATION", "value": inference_location or "us-central1"},
                    {"name": "MLR_GENIE_BOOTSTRAP_SECRET", "value": "must-not-reach-probe"},
                ]}],
            }}}})
        if args[1:4] == ["run", "jobs", "create"]:
            flags_path = next(item.removeprefix("--flags-file=") for item in args if item.startswith("--flags-file="))
            observed["flags"] = json.loads(Path(flags_path).read_text())
            observed["env"] = json.loads(Path(observed["flags"]["--env-vars-file"]).read_text())
            observed["job"] = args[4]
            return completed(args)
        if args[1:4] == ["run", "jobs", "execute"]:
            if execution_error:
                raise _driver.DeploymentError("Probe execution failed")
            return completed(args)
        if args[1:3] == ["storage", "cat"]:
            assert kwargs["sensitive"] is True
            observed["uri"] = args[3]
            return completed(args, data=probe_result)
        if args[1:4] == ["run", "jobs", "delete"]:
            observed["deleted_job"] = args[4]
            if cleanup_error:
                raise _driver.DeploymentError("Probe cleanup denied")
            return completed(args)
        if args[1:4] == ["storage", "objects", "describe"]:
            return completed(args, stdout="42\n")
        if args[1:3] == ["storage", "rm"]:
            observed["deleted_object"] = args[3]
            return completed(args)
        raise AssertionError(args)

    current = deployment(tmp_path, FakeRunner(handler), vertex_ai_location=inference_location or "",
                         allow_global_inference=allow_global_inference)
    current.report.update(commit="e" * 40, image="us-central1-docker.pkg.dev/commsrx-ai/mlr-genie/app@sha256:" + "a" * 64)
    source = tmp_path / "deploy/automation/vertex_probe.py"
    source.parent.mkdir(parents=True)
    source.write_text("print('fixed synthetic probe')\n")
    workspace = tmp_path / "private-workspace"
    workspace.mkdir()
    return current, workspace, observed


def test_runtime_probe_uses_app_identity_exact_image_and_only_public_probe_environment(tmp_path):
    current, workspace, observed = runtime_probe_deployment(tmp_path)
    current.vertex_probe(workspace)
    flags, env = observed["flags"], observed["env"]
    assert flags["--service-account"] == current.target.runtime_sa
    assert flags["--image"] == current.report["image"]
    assert flags["--command"] == ["python"] and flags["--args"] == ["-c", "print('fixed synthetic probe')\n"]
    assert flags["--max-retries"] == 0 and flags["--task-timeout"] == "180s"
    assert set(env) == {"GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_GENAI_USE_VERTEXAI",
                        "GOOGLE_GENAI_LIVE_VERTEX", "MLR_VERTEX_PROBE_BUCKET", "MLR_VERTEX_PROBE_OBJECT"}
    assert "must-not-reach-probe" not in json.dumps(flags) + json.dumps(env)
    assert observed["deleted_job"] == observed["job"]
    assert observed["uri"].startswith("gs://commsrx-ai-mlr-versioned-artifacts/deployment-probes/")
    assert observed["deleted_object"] == observed["uri"] + "#42"
    assert current.report["checks"]["runtime_identity_live_vertex_probe"] is True


def test_us_geography_probe_keeps_job_and_storage_in_deployment_region(tmp_path):
    current, workspace, observed = runtime_probe_deployment(tmp_path, inference_location="us")
    current.vertex_probe(workspace)
    assert observed["flags"]["--region"] == "us-central1"
    assert observed["env"]["GOOGLE_CLOUD_LOCATION"] == "us"
    assert observed["uri"].startswith("gs://commsrx-ai-mlr-versioned-artifacts/deployment-probes/")
    assert current.report["region"] == "us-central1"
    assert current.report["vertex_ai_location"] == "us"
    assert current.report["vertex_probe"]["location"] == "us"


def test_explicit_global_approval_is_forwarded_only_to_global_probe(tmp_path):
    current, workspace, observed = runtime_probe_deployment(
        tmp_path, inference_location="global", allow_global_inference=True,
    )
    current.vertex_probe(workspace)
    assert observed["env"]["GOOGLE_CLOUD_LOCATION"] == "global"
    assert observed["env"]["MLR_VERTEX_PROBE_ALLOW_GLOBAL"] == "true"
    assert observed["flags"]["--region"] == "us-central1"


def test_global_approval_does_not_enable_global_probe_for_us_inference(tmp_path):
    current, workspace, observed = runtime_probe_deployment(
        tmp_path, inference_location="us", allow_global_inference=True,
    )
    current.vertex_probe(workspace)
    assert "MLR_VERTEX_PROBE_ALLOW_GLOBAL" not in observed["env"]


def test_provider_probe_failure_preserves_diagnostic_and_still_removes_temporary_resources(tmp_path):
    current, workspace, observed = runtime_probe_deployment(tmp_path, result_changes={
        "status": "provider_error", "used_live_sdk": False, "provider_http_status": 400,
        "error_message": "Regional provider rejected this synthetic request.",
    })
    with pytest.raises(_driver.DeploymentError, match="Regional provider rejected"):
        current.vertex_probe(workspace)
    evidence = json.loads((tmp_path / "evidence/vertex-probe.json").read_text())
    assert evidence["provider_http_status"] == 400
    assert observed["deleted_job"] == observed["job"]
    assert observed["deleted_object"].endswith("#42")
    assert "runtime_identity_live_vertex_probe" not in current.report["checks"]


def test_failed_probe_execution_still_cleans_up_exact_job_and_output_generation(tmp_path):
    current, workspace, observed = runtime_probe_deployment(tmp_path, execution_error=True)
    with pytest.raises(_driver.DeploymentError, match="Probe execution failed"):
        current.vertex_probe(workspace)
    assert observed["deleted_job"] == observed["job"]
    assert observed["deleted_object"].endswith("#42")


def test_probe_refuses_wrong_serving_identity_before_creating_resources(tmp_path):
    current, workspace, observed = runtime_probe_deployment(tmp_path, identity="other@commsrx-ai.iam.gserviceaccount.com")
    with pytest.raises(_driver.DeploymentError, match="runtime identity"):
        current.vertex_probe(workspace)
    assert "job" not in observed


def test_probe_rejects_unexpected_output_fields_and_reports_cleanup_failure(tmp_path):
    current, workspace, observed = runtime_probe_deployment(tmp_path, result_changes={"unexpected": "do not retain"})
    with pytest.raises(_driver.DeploymentError, match="evidence does not match"):
        current.vertex_probe(workspace)
    assert not (tmp_path / "evidence/vertex-probe.json").exists()
    assert observed["deleted_job"] == observed["job"]


def test_probe_cleanup_failure_blocks_success_and_records_owned_job_name(tmp_path):
    current, workspace, observed = runtime_probe_deployment(tmp_path, cleanup_error=True)
    with pytest.raises(_driver.DeploymentError, match="cleanup needs attention"):
        current.vertex_probe(workspace)
    assert observed["job"] in current.report["cleanup_required"][0]


def test_journal_probe_keeps_the_deep_model_evidence_and_uses_only_the_fixed_case(tmp_path):
    current, workspace, observed = runtime_probe_deployment(tmp_path)
    current.vertex_probe(workspace)
    original = (tmp_path / "evidence/vertex-probe.json").read_text()
    current.vertex_probe(workspace, case="journal_intake")
    assert observed["env"]["MLR_VERTEX_PROBE_CASE"] == "journal_intake"
    assert (tmp_path / "evidence/vertex-probe.json").read_text() == original
    assert (tmp_path / "evidence/journal-intake-probe.json").exists()
    assert current.report["checks"]["runtime_identity_journal_intake_probe"] is True
    assert "vertex_probe" in current.report and "journal_intake_probe" in current.report
    creates = [args[4] for args, _ in current.runner.calls if args[1:4] == ["run", "jobs", "create"]]
    deletes = [args[4] for args, _ in current.runner.calls if args[1:4] == ["run", "jobs", "delete"]]
    assert creates == deletes and len(set(creates)) == 2


def test_unknown_probe_case_is_refused_before_cloud_access(tmp_path):
    current, workspace, _ = runtime_probe_deployment(tmp_path)
    with pytest.raises(_driver.DeploymentError, match="Only fixed public"):
        current.vertex_probe(workspace, case="../../private")
    assert not current.runner.calls


def test_journal_failure_retains_public_diagnostic_and_redacts_credentials(tmp_path, monkeypatch):
    import hashlib
    import httpx
    data = b"fixed-public-fixture"
    monkeypatch.setattr(_driver, "PAPER_SHA256", hashlib.sha256(data).hexdigest())
    original_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data)),
    ))

    def handler(args, kwargs):
        assert args[1] == "evals/live_journal_verification.py"
        report_dir = Path(args[args.index("--report-dir") + 1])
        report_dir.mkdir(parents=True)
        (report_dir / "diagnostic.json").write_text(json.dumps({"verdict": "insufficient", "diagnosis": "test-secret"}))
        raise _driver.DeploymentError("Anchor validation failed")

    current = deployment(tmp_path, FakeRunner(handler))
    current.report["public_url"] = "https://mlr.example.com"
    current.sanitizer.add_secret("test-secret")
    with pytest.raises(_driver.DeploymentError, match="Anchor validation failed"):
        current.journal_eval("test-secret", tmp_path)
    diagnostic = (tmp_path / "evidence/journal/diagnostic.json").read_text()
    assert '"verdict": "insufficient"' in diagnostic
    assert "test-secret" not in diagnostic and "[REDACTED]" in diagnostic


@pytest.mark.parametrize("commit,state", [("not-a-sha", "pending"), ("e" * 40, "error")])
def test_public_ip_status_rejects_invalid_commit_or_state_before_network(tmp_path, monkeypatch, commit, state):
    current = public_ip_deployment(tmp_path, monkeypatch)
    current.report["commit"] = commit
    settings, calls = mock_status_client(monkeypatch)
    with pytest.raises(_driver.DeploymentError, match="commit-status identity or state"):
        current._publish_public_ip_status(state=state)
    assert not settings and not calls
