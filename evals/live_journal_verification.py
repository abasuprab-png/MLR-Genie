"""Verify a reworded claim against a real journal PDF with unmocked Gemini."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
from urllib.parse import urlsplit

import httpx
import pymupdf


def _public_url(value: str, *, https_only: bool) -> str:
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in (("https",) if https_only else ("http", "https"))
                 and parsed.hostname and not parsed.username and not parsed.password
                 and not parsed.fragment and (not https_only or not parsed.query))
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Use a public HTTP(S) source URL or an HTTPS service URL without credentials, query, or fragment.")
    return value.rstrip("/") if https_only else value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-pdf", required=True, type=Path)
    parser.add_argument("--claim", required=True, help="A reworded claim supported by the supplied journal PDF.")
    parser.add_argument("--source-url", required=True, help="Public bibliographic URL; recorded as attribution, never fetched.")
    parser.add_argument("--base-url", default=os.getenv("MLR_INTEGRATION_BASE_URL"),
                        help="Existing HTTPS service; otherwise start an isolated local hosted server.")
    parser.add_argument("--report-dir", type=Path, help="Output directory; defaults to a new temporary directory.")
    args = parser.parse_args(argv)
    try:
        args.source_url = _public_url(args.source_url, https_only=False)
        if args.base_url:
            args.base_url = _public_url(args.base_url, https_only=True)
            if not os.getenv("MLR_GENIE_BOOTSTRAP_SECRET", "").strip():
                parser.error("External evaluation requires the deployed MLR_GENIE_BOOTSTRAP_SECRET; no remote credentials are generated.")
    except ValueError as exc:
        parser.error(str(exc))
    if not args.claim.strip():
        parser.error("--claim must not be blank.")
    return args


def verify_pdf_anchor(pdf_bytes: bytes, result: dict) -> dict:
    """Re-extract the returned rectangle from the submitted PDF independently."""
    anchor = result.get("anchor", {})
    if anchor.get("verdict") != "supported" or result.get("coordinateSpace") != "pdf_points":
        raise ValueError("The claim did not receive a supported PDF-points anchor.")
    page_number, box, quote = anchor.get("page"), anchor.get("boundingBox"), anchor.get("verbatimQuote")
    if type(page_number) is not int or not isinstance(box, dict) or not isinstance(quote, str) or not quote.strip():
        raise ValueError("A page, bounding box, and verbatim quote are required.")
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        if not 1 <= page_number <= len(pdf):
            raise ValueError("The returned PDF page is outside the submitted document.")
        page = pdf[page_number - 1]
        try:
            values = [box[key] for key in ("x", "y", "width", "height")]
            if not all(type(value) in (int, float) and math.isfinite(value) for value in values):
                raise ValueError("Nonfinite rectangle")
            rect = pymupdf.Rect(box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("The returned bounding box is invalid.") from None
        if rect.is_empty or rect.is_infinite or rect not in page.rect:
            raise ValueError("The returned bounding box is outside the submitted page.")
        # The endpoint uses this one-point text extraction tolerance around its
        # verified rectangle; its coordinates themselves must stay on the page.
        extracted = page.get_text("text", clip=rect + (-1, -1, 1, 1)).strip()
        if not extracted or " ".join(extracted.split()) != " ".join(quote.split()):
            raise ValueError("The returned quote does not match the uploaded PDF rectangle.")
        dimensions = result.get("pageDimensions", {})
        if dimensions.get("width") != page.rect.width or dimensions.get("height") != page.rect.height:
            raise ValueError("The returned dimensions do not match the uploaded PDF page.")
        return {"page": page_number, "bounding_box_in_page": True,
                "quote_matches_pdf_rectangle": True, "page_count": len(pdf)}


def api_failure_summary(response: httpx.Response) -> str:
    """Report assessment failure categories without echoing document or credential data."""
    summary = f"HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return summary
    if not isinstance(body, dict) or body.get("assessment_failed") is not True:
        return summary
    detail = body.get("detail")
    if not isinstance(detail, str):
        return summary + " (assessment failed)"
    if "live Gemini assessment required but unavailable." in detail:
        reason = "live assessment unavailable"
    elif detail.endswith(": invalid structured Gemini assessment."):
        reason = "structured assessment invalid"
    elif detail.endswith(": cited span is absent from the supplied text."):
        reason = "assessment citation absent from supplied text"
    elif detail == "Integration verification requires successful live Gemini calls.":
        reason = "live claim-verification receipts missing"
    elif detail == "Live Gemini verification required but unavailable.":
        reason = "live client unavailable"
    else:
        reason = "assessment failed"
    status = re.search(r"\(provider HTTP ([1-5][0-9]{2})\)", detail)
    if status:
        reason += "; provider HTTP " + status.group(1)
    return summary + " (" + reason + ")"


@contextmanager
def isolated_hosted_server(run: Path):
    """Keep this process's test credentials and storage separate from deployment."""
    from evals.live_integrations import local_server

    isolated = {
        "MLR_GENIE_ARTIFACT_BACKEND": "local", "MLR_GENIE_REQUIRE_DURABLE_STORAGE": "false",
        "MLR_GENIE_DB_IAM_AUTH": "false", "MLR_GENIE_SESSION_SECRET": secrets.token_urlsafe(48),
        "MLR_GENIE_BOOTSTRAP_SECRET": secrets.token_urlsafe(48),
    }
    changed_names = {*isolated, "DATABASE_URL", "MLR_REQUIRE_SIGNED_BEARER_TOKEN",
                     "MLR_REQUIRE_LIVE_GEMINI", "MLR_GENIE_ENV", "MLR_GENIE_ALLOWED_ORIGINS"}
    previous = {name: os.environ.get(name) for name in changed_names}
    os.environ.update(isolated)
    try:
        with local_server(run, hosted=True) as server:
            yield server
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def run_evaluation(args) -> Path:
    pdf_bytes = args.reference_pdf.read_bytes()
    if not 1 <= len(pdf_bytes) <= 32 * 1024 * 1024:
        raise ValueError("The reference PDF must contain 1–33554432 bytes.")
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        if pdf.needs_pass or not len(pdf):
            raise ValueError("The journal PDF must be readable and unencrypted.")
        source_text = " ".join(page.get_text() for page in pdf)
    if " ".join(args.claim.split()) in " ".join(source_text.split()):
        raise ValueError("Use a reworded claim rather than a verbatim PDF sentence.")
    run = args.report_dir or Path(tempfile.mkdtemp(prefix="mlr-live-journal-"))
    run.mkdir(parents=True, exist_ok=True)
    context = nullcontext((args.base_url, None)) if args.base_url else isolated_hosted_server(run)
    with context as (base_url, api):
        credential_values = [os.getenv(name, "") for name in (
            "GEMINI_API_KEY", "MLR_GENIE_SESSION_SECRET", "MLR_GENIE_BOOTSTRAP_SECRET",
        )]
        scope = "external-https" if args.base_url else "isolated-loopback"
        with httpx.Client(base_url=base_url, timeout=300, follow_redirects=False,
                          trust_env=bool(args.base_url)) as client:
            health = client.get("/health")
            if health.status_code != 200:
                raise RuntimeError(f"Health endpoint returned HTTP {health.status_code}.")
            unauthenticated = client.post("/integrations/cams/verify-source")
            if unauthenticated.status_code != 401:
                raise RuntimeError(f"Tokenless CAMS returned HTTP {unauthenticated.status_code}, expected 401.")
            issued = client.post("/auth/token", headers={
                "X-MLR-Bootstrap-Secret": os.environ["MLR_GENIE_BOOTSTRAP_SECRET"],
            }, json={"tenant_id": "journal-software-evaluation", "actor_id": "journal-requester", "role": "requester"})
            if issued.status_code != 200:
                raise RuntimeError(f"Requester issuance returned HTTP {issued.status_code}.")
            token = issued.json()
            if token.get("role") != "requester" or not token.get("access_token"):
                raise ValueError("The issuer did not return a requester token.")
            credential_values.append(token["access_token"])
            verified = client.post("/integrations/cams/verify-source", headers={
                "Authorization": "Bearer " + token["access_token"],
            }, data={"payload": json.dumps({"claimText": args.claim, "requireLive": True,
                "source": {"sourceId": args.source_url, "url": args.source_url,
                           "title": args.reference_pdf.name}})},
                files={"reference_pdf": (args.reference_pdf.name, pdf_bytes, "application/pdf")})
            if verified.status_code != 200:
                raise RuntimeError(f"Journal verification returned {api_failure_summary(verified)}.")
            result = verified.json()
        anchor_check = verify_pdf_anchor(pdf_bytes, result)
        receipts = result.get("telemetry_receipts")
        if (not isinstance(receipts, list) or not receipts
                or not all(receipt.get("used_live_sdk") is True
                           and receipt.get("transport") in ("generate_content", "interactions")
                           for receipt in receipts)):
            raise ValueError("Journal verification did not return live model receipts.")
        report = {
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "execution": {"scope": scope, "entry_point": base_url, "synthetic_fixture": False,
                          "storage_scope": "existing deployed service" if args.base_url else "temporary SQLite/local artifacts",
                          "cloud_deployment_performed": False},
            "source": {"url": args.source_url, "filename": args.reference_pdf.name,
                       "sha256": hashlib.sha256(pdf_bytes).hexdigest(), "bytes": len(pdf_bytes)},
            "claim": args.claim, "claim_is_verbatim_pdf_span": False,
            "authentication": {"role": "requester", "health_status": health.status_code,
                               "tokenless_cams_status": unauthenticated.status_code},
            "pdf_validation": anchor_check, "result": result,
        }
        if api is not None:
            report["router_telemetry_log"] = [entry.model_dump() for entry in api.router.telemetry_log]
        repo = Path(__file__).resolve().parents[1]
        source_paths = [Path(__file__).resolve()]
        if api is not None:
            source_paths += sorted((repo / "backend").rglob("*.py"))
        report["source_sha256"] = {
            str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths
        }
        serialized = json.dumps(report, indent=2)
        for value in credential_values:
            if value:
                serialized = serialized.replace(value, "[REDACTED]")
        report_path = run / "result.json"
        report_path.write_text(serialized + "\n")
    return report_path


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    args = parse_args()
    report_path = run_evaluation(args)
    print(f"Passed real journal verification ({'external HTTPS' if args.base_url else 'isolated local hosted server'}); report: {report_path}")


if __name__ == "__main__":
    main()
