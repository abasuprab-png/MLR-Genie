"""The cloud secret is never needed to test this transfer boundary."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile
from types import SimpleNamespace

import jwt
from unittest.mock import patch

from deploy.automation import export_cams_bootstrap as export


class BootstrapExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory()
        cls.private = Path(cls.workspace.name) / "receiver.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072",
                        "-out", str(cls.private)], capture_output=True, check=True)
        cls.public = subprocess.check_output(["openssl", "pkey", "-in", str(cls.private), "-pubout"])

    @classmethod
    def tearDownClass(cls):
        cls.workspace.cleanup()

    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.request = {
            "version": 1, "receiver_run": "12345678901", "receiver_commit": "a" * 40,
            "receiver_repository": export.RECEIVER_REPOSITORY,
            "source_project": "commsrx-ai", "source_secret": "mlr-genie-bootstrap",
            "destination_app": export.DESTINATION_APP, "destination_secret": "mlr-genie-bootstrap-secret",
            "expires_at": (self.now + timedelta(minutes=20)).isoformat(),
            "key_sha256": hashlib.sha256(self.public).hexdigest(),
        }
        self.payload = {**self.request, "public_key_base64": base64.b64encode(self.public).decode()}
        self.claims = {
            "run_id": "12345678901", "sha": "a" * 40, "ref": "refs/heads/main", "event_name": "workflow_dispatch",
            "actor_id": "227769156", "repository_owner_id": "227769156", "repository_id": "1423359597",
            "repository": export.RECEIVER_REPOSITORY,
            "workflow_ref": export.RECEIVER_REPOSITORY + "/.github/workflows/configure-mlr-genie.yml@refs/heads/main",
            "sub": "repo:" + export.RECEIVER_REPOSITORY + ":ref:refs/heads/main",
            "iat": int(self.now.timestamp()), "nbf": int(self.now.timestamp()) - 1,
            "exp": int(self.now.timestamp()) + 300, "iss": export.ISSUER,
            "aud": export.AUDIENCE_PREFIX + hashlib.sha256(export.canonical_label(self.request)).hexdigest(),
        }
        self.key_client = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=self.public))

    def bundle(self, *, request=None, token=None, extra=False):
        token = token or jwt.encode(self.claims, self.private.read_bytes(), algorithm="RS256", headers={"kid": "unit"})
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as z:
            z.writestr("request.json", json.dumps(self.request if request is None else request))
            z.writestr("public.pem", self.public)
            z.writestr("attestation.jwt", token)
            if extra:
                z.writestr("unexpected", "not part of the protocol")
        return base64.b64encode(data.getvalue()).decode()

    def test_only_the_exact_active_authorized_receiver_is_accepted(self):
        request, public = export.validate_receiver(self.payload, self.claims, self.now)
        self.assertEqual(request, self.request)
        self.assertEqual(public, self.public)
        for key, changed in {
            "ref": "refs/heads/untrusted", "sha": "b" * 40, "event_name": "pull_request",
            "workflow_ref": "other/workflow", "actor_id": "1", "repository": "other/repo",
        }.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                export.validate_receiver(self.payload, {**self.claims, key: changed}, self.now)

    def test_expired_key_fingerprint_and_destination_changes_are_refused(self):
        for key, changed in {
            "expires_at": (self.now - timedelta(seconds=1)).isoformat(),
            "key_sha256": "0" * 64, "destination_app": export.DESTINATION_APP + "-other",
            "destination_secret": "another-secret", "source_secret": "another-secret",
            "receiver_run": "../../other", "version": True,
        }.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                export.validate_receiver({**self.payload, key: changed}, self.claims, self.now)

    def test_ciphertext_roundtrip_binds_destination_and_run_inside_oaep(self):
        secret = b"unit-test-only-" + b"x" * 48
        sealed = export.seal_secret(secret, self.public, self.request)
        encoded = json.dumps(sealed)
        self.assertNotIn(secret.decode(), encoded)
        ciphertext = base64.b64decode(sealed["ciphertext_base64"], validate=True)
        self.assertEqual(len(ciphertext), 384)
        args = ["openssl", "pkeyutl", "-decrypt", "-inkey", str(self.private),
                "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256",
                "-pkeyopt", "rsa_mgf1_md:sha256", "-pkeyopt"]
        decrypted = subprocess.run(args + ["rsa_oaep_label:" + export.canonical_label(self.request).hex()],
                                   input=ciphertext, capture_output=True, check=True)
        self.assertEqual(decrypted.stdout, secret)
        changed = {**self.request, "receiver_run": "98765432101"}
        refused = subprocess.run(args + ["rsa_oaep_label:" + export.canonical_label(changed).hex()],
                                 input=ciphertext, capture_output=True, check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(refused.stdout, b"")

    def test_signed_bundle_authenticates_key_and_all_request_metadata(self):
        request, public = export.read_verified_bundle(self.bundle(), self.now, key_client=self.key_client)
        self.assertEqual(request, self.request)
        self.assertEqual(public, self.public)
        # A caller cannot substitute either a key fingerprint or a different destination/run.
        for key, value in {"key_sha256": "0" * 64, "receiver_run": "98765432101", "destination_secret": "other"}.items():
            with self.subTest(key=key), self.assertRaises(jwt.InvalidAudienceError):
                export.read_verified_bundle(self.bundle(request={**self.request, key: value}), self.now, key_client=self.key_client)

    def test_invalid_attestation_and_extra_archive_entries_fail_before_cloud_access(self):
        for changes in ({"iss": "https://untrusted.example"}, {"exp": int(self.now.timestamp()) - 120}, {"aud": "api://AzureADTokenExchange"}):
            token = jwt.encode({**self.claims, **changes}, self.private.read_bytes(), algorithm="RS256")
            with self.subTest(changes=changes), self.assertRaises(jwt.InvalidTokenError):
                export.read_verified_bundle(self.bundle(token=token), self.now, key_client=self.key_client)
        with self.assertRaises(ValueError):
            export.read_verified_bundle(self.bundle(extra=True), self.now, key_client=self.key_client)

    def test_command_errors_cannot_echo_credentials_or_arguments(self):
        private = b"private-bootstrap-secret"
        result = subprocess.CompletedProcess(["example", private.decode()], 1, stdout=private, stderr=private)
        with patch.object(export.subprocess, "run", return_value=result):
            with self.assertRaises(RuntimeError) as caught:
                export.safe_command(["example", private.decode()], "Authorized operation", input_bytes=private)
        self.assertNotIn(private.decode(), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
