"""Seal the one authorized MLR bootstrap secret to a running CAMS receiver."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile


RECEIVER_REPOSITORY = "abasuprab-png/CAMS-Onramp-for-CommsRx-App"
DESTINATION_APP = "/subscriptions/85d4d146-1694-46a2-9830-43ec9f2c5ba2/resourceGroups/rg-commsrx-eval/providers/Microsoft.App/containerApps/cams"
REQUEST_KEYS = {
    "version", "receiver_run", "receiver_commit", "receiver_repository", "source_project",
    "source_secret", "destination_app", "destination_secret", "expires_at", "key_sha256",
}
ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE_PREFIX = "https://mlr-genie.signalfidelitygroup.com/cams-bootstrap-relay/"


def canonical_label(request: dict) -> bytes:
    return json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")


def validate_receiver(payload: dict, claims: dict, now: datetime) -> tuple[dict, bytes]:
    if not isinstance(payload, dict) or set(payload) != REQUEST_KEYS | {"public_key_base64"}:
        raise ValueError("Unexpected receiver request schema.")
    request = {key: payload[key] for key in REQUEST_KEYS}
    if (type(request["version"]) is not int or request["version"] != 1
            or request["receiver_repository"] != RECEIVER_REPOSITORY
            or request["source_project"] != "commsrx-ai"
            or request["source_secret"] != "mlr-genie-bootstrap"
            or request["destination_app"] != DESTINATION_APP
            or request["destination_secret"] != "mlr-genie-bootstrap-secret"):
        raise ValueError("The source or destination is outside the authorized transfer.")
    if (not isinstance(request["receiver_run"], str) or not re.fullmatch(r"[1-9][0-9]{1,19}", request["receiver_run"])
            or not isinstance(request["receiver_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", request["receiver_commit"])
            or not isinstance(request["key_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", request["key_sha256"])):
        raise ValueError("Invalid receiver identity.")
    expires = datetime.fromisoformat(request["expires_at"].replace("Z", "+00:00"))
    if (expires.tzinfo is None or not now < expires <= now + timedelta(minutes=21)
            or type(claims.get("iat")) is not int
            or not now.timestamp() - 1200 <= claims["iat"] <= now.timestamp() + 30):
        raise ValueError("The receiver request is expired or not fresh.")
    expected = {
        "run_id": request["receiver_run"], "sha": request["receiver_commit"], "ref": "refs/heads/main",
        "event_name": "workflow_dispatch", "actor_id": "227769156", "repository_owner_id": "227769156",
        "repository_id": "1423359597", "repository": RECEIVER_REPOSITORY,
        "workflow_ref": RECEIVER_REPOSITORY + "/.github/workflows/configure-mlr-genie.yml@refs/heads/main",
        "sub": "repo:abasuprab-png@227769156/CAMS-Onramp-for-CommsRx-App@1423359597:ref:refs/heads/main",
    }
    if any(claims.get(key) != value for key, value in expected.items()):
        raise ValueError("The signed GitHub identity does not match the authorized CAMS receiver.")
    public_key = base64.b64decode(payload["public_key_base64"], validate=True)
    if (not 500 <= len(public_key) <= 1500 or not public_key.startswith(b"-----BEGIN PUBLIC KEY-----\n")
            or hashlib.sha256(public_key).hexdigest() != request["key_sha256"]):
        raise ValueError("The receiver public key does not match its fingerprint.")
    return request, public_key


def read_verified_bundle(encoded: str, now: datetime, *, key_client=None) -> tuple[dict, bytes]:
    """Authenticate the public encryption key without sharing cross-repository credentials."""
    import jwt

    if not isinstance(encoded, str) or len(encoded) > 180000:
        raise ValueError("Invalid receiver bundle size.")
    archive = base64.b64decode(encoded, validate=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        entries = bundle.infolist()
        if (len(entries) != 3 or {item.filename for item in entries} != {"request.json", "public.pem", "attestation.jwt"}
                or any(item.file_size > 16000 or item.flag_bits & 1 for item in entries)):
            raise ValueError("The receiver bundle must contain only the bounded public request, key and attestation.")
        request = json.loads(bundle.read("request.json"))
        public_key = bundle.read("public.pem")
        attestation = bundle.read("attestation.jwt").decode().strip()
    if not isinstance(request, dict) or set(request) != REQUEST_KEYS:
        raise ValueError("Unexpected signed receiver request schema.")
    audience = AUDIENCE_PREFIX + hashlib.sha256(canonical_label(request)).hexdigest()
    key_client = key_client or jwt.PyJWKClient(ISSUER + "/.well-known/jwks", timeout=20)
    signing_key = key_client.get_signing_key_from_jwt(attestation)
    claims = jwt.decode(
        attestation, signing_key.key, algorithms=["RS256"], issuer=ISSUER, audience=audience, leeway=30,
        options={"require": ["exp", "iat", "nbf", "iss", "aud", "sub"], "strict_aud": True},
    )
    return validate_receiver({**request, "public_key_base64": base64.b64encode(public_key).decode()}, claims, now)


def safe_command(args: list[str], purpose: str, *, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(args, input=input_bytes, capture_output=True, check=False, timeout=90)
    if result.returncode:
        # Never stringify a command or provider diagnostic that could contain credentials.
        raise RuntimeError(purpose + " failed (exit " + str(result.returncode) + ").")
    return result.stdout


def seal_secret(secret: bytes, public_key: bytes, request: dict) -> dict:
    if not 32 <= len(secret) <= 318:
        raise ValueError("The authorized bootstrap secret has an unexpected size.")
    with tempfile.TemporaryDirectory(prefix="mlr-cams-seal-") as temporary:
        public_path = Path(temporary) / "public.pem"
        public_path.write_bytes(public_key)
        ciphertext = safe_command([
            "openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey", str(public_path),
            "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256",
            "-pkeyopt", "rsa_mgf1_md:sha256", "-pkeyopt", "rsa_oaep_label:" + canonical_label(request).hex(),
        ], "Sealing the bootstrap secret", input_bytes=secret)
    if len(ciphertext) != 384:
        raise ValueError("The receiver must use a 3072-bit RSA key.")
    return {"request": request, "ciphertext_base64": base64.b64encode(ciphertext).decode("ascii")}


def main() -> int:
    request, public_key = read_verified_bundle(os.environ["RECEIVER_BUNDLE_BASE64"], datetime.now(timezone.utc))
    run_id = request["receiver_run"]
    project = json.loads(safe_command([
        "gcloud", "projects", "describe", "commsrx-ai", "--project=commsrx-ai", "--format=json", "--quiet",
    ], "Verifying the source project"))
    if project.get("projectId") != "commsrx-ai" or str(project.get("projectNumber")) != "333570013714":
        raise ValueError("Unexpected source project.")
    account = safe_command([
        "gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)", "--quiet",
    ], "Verifying the source identity").decode().strip()
    if account != "mlr-genie-deployer@commsrx-ai.iam.gserviceaccount.com":
        raise ValueError("Unexpected source identity.")
    secret = safe_command([
        "gcloud", "secrets", "versions", "access", "latest", "--secret=mlr-genie-bootstrap",
        "--project=commsrx-ai", "--quiet",
    ], "Reading the authorized bootstrap secret")
    sealed = seal_secret(secret, public_key, request)
    destination = Path("bridge-transfer-output/sealed.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(sealed, indent=2) + "\n")
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write("receiver_run=" + run_id + "\n")
    print("The authorized bootstrap secret was encrypted for the active CAMS receiver.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # The operator can diagnose the fixed stage without any secret or command output.
        print("Encrypted CAMS bootstrap transfer failed; no plaintext output was published.")
        raise SystemExit(1)
