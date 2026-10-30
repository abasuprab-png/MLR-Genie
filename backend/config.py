from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GcpAgentConfig:
    """Configuration for MLR Genie on Google Cloud Platform & Gemini Enterprise Agent Platform."""

    project_id: str = field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_PROJECT", "mlr-genie-enterprise-prod"))
    location: str = field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"))
    use_vertex_ai: bool = field(
        default_factory=lambda: os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "true").lower() in ("true", "1", "yes")
    )
    gemini_api_key: str | None = field(default_factory=lambda: os.getenv("GEMINI_API_KEY"))
    zero_data_retention: bool = True
    cmek_key_name: str = field(
        default_factory=lambda: os.getenv(
            "GCP_CMEK_KEY_NAME",
            "projects/mlr-genie-enterprise-prod/locations/us-central1/keyRings/mlr-ring/cryptoKeys/mlr-cmek",
        )
    )
    model_armor_template: str = field(
        default_factory=lambda: os.getenv(
            "GCP_MODEL_ARMOR_TEMPLATE",
            "projects/mlr-genie-enterprise-prod/locations/us-central1/templates/mlr-ipi-phi-guard",
        )
    )
    gcs_bucket: str = field(default_factory=lambda: os.getenv("MLR_GCS_BUCKET", "gs://mlr-genie-versioned-artifacts"))
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./mlr_genie_authoritative.db")
    )

    # Explicit Gemini Model Routing Identifiers (per gemini-api-dev specification)
    model_deep_reasoning: str = "gemini-4-argon"
    model_orchestrator: str = "gemini-3.8-flash"
    model_fast_gate: str = "gemini-3.5-flash-lite"
    model_embedding: str = "gemini-embedding-2"
    model_transcribe: str = "gemini-3.5-transcribe"
    managed_sandbox_agent: str = "antigravity-preview-09-2026"
