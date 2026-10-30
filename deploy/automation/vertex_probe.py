"""Synthetic Vertex diagnostic, executable as source in a Cloud Run job.

The deployment driver embeds this source into the existing application image.
Only a sanitized result is written to an immutable private GCS object; this
script never reads application documents, bootstrap credentials, or job data.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

from backend.agent.models_router import GeminiModelRouter
from backend.config import GcpAgentConfig


PROJECT_ID = "commsrx-ai"
MAX_ERROR_LENGTH = 4000
PROBE_PROMPT = "This is a public synthetic software connectivity check. Return exactly {\"ok\": true}."
PROBE_SCHEMA = {
    "type": "object", "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"], "additionalProperties": False,
}


def sanitize_error(value: Any) -> str:
    message = str(value)
    message = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", message)
    message = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED]", message)
    message = re.sub(r"\bAIza[A-Za-z0-9_-]{20,}\b", "[REDACTED]", message)
    message = re.sub(r"\bya29\.[A-Za-z0-9._-]+", "[REDACTED]", message)
    message = re.sub(
        r'''(?i)(["']?(?:api[_-]?key|access_token|refresh_token|id_token|client_secret|authorization)["']?\s*[:=]\s*)["']?[^\s,"'}]+["']?''',
        r"\1[REDACTED]", message,
    )
    return message[:MAX_ERROR_LENGTH]


def validate_target(project_id: str, location: str, bucket: str, object_name: str) -> None:
    if project_id != PROJECT_ID:
        raise ValueError("Probe project must match the authorized deployment project.")
    if location not in {"us", "eu"} and not re.fullmatch(r"[a-z]+-[a-z]+[0-9]", location):
        raise ValueError("Probe requires us, eu, or a regional location; global inference is not permitted.")
    if bucket != f"{PROJECT_ID}-mlr-versioned-artifacts":
        raise ValueError("Probe destination must be the deployment's private artifact bucket.")
    if (not object_name.startswith("deployment-probes/") or len(object_name) > 1024
            or not re.fullmatch(r"[A-Za-z0-9_./-]+\.json", object_name)
            or ".." in object_name):
        raise ValueError("Probe destination must be a JSON object under deployment-probes/.")


def collect_probe(project_id: str, location: str, *, router_factory=GeminiModelRouter) -> dict:
    config = GcpAgentConfig(
        project_id=project_id, location=location, use_vertex_ai=True,
        gemini_api_key=None, zero_data_retention=True,
    )
    result = {
        "project_id": project_id, "location": location,
        "model": config.model_fast_gate, "effective_model": None,
        "used_live_sdk": False, "status": "probe_error", "provider_http_status": None,
        "error_message": "",
    }
    try:
        router = router_factory(config)
        payload, telemetry = router.create_interaction(
            task_type="cv08_pv_gate", prompt=PROBE_PROMPT,
            system_instruction="Perform this synthetic connectivity check only. Return the requested JSON.",
            response_schema=PROBE_SCHEMA, fallback_payload=None,
        )
        result["model"] = telemetry.model
        result["effective_model"] = telemetry.effective_model
        result["used_live_sdk"] = telemetry.used_live_sdk is True
        if isinstance(payload, dict) and payload.get("status") == "provider_error":
            result["status"] = "provider_error"
            status = payload.get("provider_http_status")
            if type(status) is int and 100 <= status <= 599:
                result["provider_http_status"] = status
            result["error_message"] = sanitize_error(payload.get("error", "Provider error without diagnostic."))
        elif not result["used_live_sdk"]:
            result["status"] = "client_unavailable"
            result["error_message"] = "The configured Vertex client did not execute a live SDK request."
        elif payload == {"ok": True}:
            result["status"] = "passed"
        else:
            result["status"] = "invalid_response"
            result["error_message"] = "The provider returned a response outside the synthetic probe schema."
    except Exception as exc:
        result["error_message"] = sanitize_error(exc)
        status = GeminiModelRouter._provider_http_status(exc)
        if status is not None:
            result["provider_http_status"] = status
    return result


def write_result(result: dict, bucket: str, object_name: str, *, storage_client=None) -> None:
    if storage_client is None:
        from google.cloud import storage
        storage_client = storage.Client(project=PROJECT_ID)
    storage_client.bucket(bucket).blob(object_name).upload_from_string(
        json.dumps(result, sort_keys=True), content_type="application/json", if_generation_match=0,
    )


def main() -> int:
    try:
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        location = os.environ.get("GOOGLE_CLOUD_LOCATION", "")
        bucket = os.environ.get("MLR_VERTEX_PROBE_BUCKET", "")
        object_name = os.environ.get("MLR_VERTEX_PROBE_OBJECT", "")
        validate_target(project_id, location, bucket, object_name)
    except Exception:
        print("Vertex probe target or private output configuration is invalid.", file=sys.stderr)
        return 1
    os.environ["GOOGLE_GENAI_LIVE_VERTEX"] = "true"
    result = collect_probe(project_id, location)
    try:
        write_result(result, bucket, object_name)
    except Exception:
        print("Vertex probe could not store its sanitized diagnostic.", file=sys.stderr)
        return 1
    print("Sanitized Vertex probe diagnostic stored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
