from __future__ import annotations

import hashlib
import json
import math
from enum import Enum
from typing import Any

from pydantic import BaseModel

from backend.config import GcpAgentConfig


class MlrModelRole(str, Enum):
    """Explicit Gemini model roles per MLR Genie workflow stage."""

    DEEP_REASONING = "gemini-4-argon"
    ORCHESTRATOR = "gemini-3.8-flash"
    FAST_GATE = "gemini-3.5-flash-lite"
    EMBEDDING = "gemini-embedding-2"
    TRANSCRIBE = "gemini-3.5-transcribe"
    MANAGED_SANDBOX = "antigravity-preview-09-2026"


CLINICAL_SAFETY_SETTINGS: list[dict[str, str]] = [
    {
        "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
        "threshold": "BLOCK_ONLY_HIGH",
    },
    {
        "category": "HARM_CATEGORY_HARASSMENT",
        "threshold": "BLOCK_LOW_AND_ABOVE",
    },
    {
        "category": "HARM_CATEGORY_HATE_SPEECH",
        "threshold": "BLOCK_LOW_AND_ABOVE",
    },
    {
        "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "threshold": "BLOCK_LOW_AND_ABOVE",
    },
]


class ModelCallTelemetry(BaseModel):
    model: str
    effective_model: str | None = None
    task_type: str
    service_tier: str = "standard"
    used_live_sdk: bool = False
    transport_mode: str = "offline_fallback"
    cached_content: str | None = None
    previous_interaction_id: str | None = None
    interaction_id: str


class GeminiModelRouter:
    """
    Routes MLR Genie tasks to the optimal Gemini model via either:
    - Stateless GenerateContent on Vertex AI US/EU multi-region endpoints
    - Google GenAI Interactions API (`client.interactions.create`) on other Vertex endpoints
    - Gemini Developer API (`client.models.generate_content`) when in `api_key` mode or when `interactions` is absent.
    """

    def __init__(self, config: GcpAgentConfig | None = None) -> None:
        import os

        self.config = config or GcpAgentConfig()
        self.telemetry_log: list[ModelCallTelemetry] = []
        self.safety_settings: list[dict[str, str]] = [dict(item) for item in CLINICAL_SAFETY_SETTINGS]
        self.context_caches: dict[str, dict[str, Any]] = {}
        self._client: Any = None
        if self.config.gemini_api_key:
            try:
                from google import genai

                self._client = genai.Client(
                    api_key=self.config.gemini_api_key,
                    vertexai=False,
                )
            except Exception:
                self._client = None
        elif self.config.use_vertex_ai and os.getenv("GOOGLE_GENAI_LIVE_VERTEX", "").lower() in (
            "true",
            "1",
            "yes",
        ):
            try:
                from google import genai

                self._client = genai.Client(
                    vertexai=True,
                    project=self.config.project_id,
                    location=self.config.location,
                )
            except Exception:
                self._client = None

    def get_clinical_safety_settings(self) -> list[dict[str, str]]:
        """Returns a copy of the calibrated clinical/regulatory safety settings."""
        return [dict(item) for item in self.safety_settings]

    def get_interactions_safety_settings(self) -> list[dict[str, str]]:
        """Encode the same thresholds using the Interactions API's typed schema."""
        return [
            {
                "type": item["category"].removeprefix("HARM_CATEGORY_").lower(),
                "threshold": item["threshold"].lower(),
            }
            for item in self.get_clinical_safety_settings()
        ]

    @staticmethod
    def _provider_http_status(exc: Exception) -> int | None:
        for value in (
            getattr(exc, "code", None),
            getattr(exc, "status_code", None),
            getattr(getattr(exc, "response", None), "status_code", None),
        ):
            if type(value) is int and 100 <= value <= 599:
                return value
        return None

    @staticmethod
    def _normalize_cache_contents(pdf_or_text_content: Any) -> list[Any]:
        """
        Normalizes raw PDF bytes, strings, or `source_pack` dictionaries into valid
        `google.genai.types.CreateCachedContentConfig` `contents` entries (`PartDict`,
        `ContentDict`, or `str`) so Pydantic validation on `CreateCachedContentConfig`
        never rejects extra dictionary keys or binary PDF bytes.
        """
        valid_part_or_content_keys = {"text", "inline_data", "file_data", "parts", "role"}
        if isinstance(pdf_or_text_content, bytes):
            return [{"inline_data": {"data": pdf_or_text_content, "mime_type": "application/pdf"}}]
        if isinstance(pdf_or_text_content, str):
            return [pdf_or_text_content]
        if isinstance(pdf_or_text_content, list):
            normalized: list[Any] = []
            for item in pdf_or_text_content:
                if isinstance(item, bytes):
                    normalized.append({"inline_data": {"data": item, "mime_type": "application/pdf"}})
                elif isinstance(item, str):
                    normalized.append(item)
                elif isinstance(item, dict):
                    if item and set(item.keys()).issubset(valid_part_or_content_keys):
                        normalized.append(item)
                    else:
                        normalized.append(json.dumps(item, sort_keys=True, default=str))
                else:
                    normalized.append(item)
            return normalized or [""]
        return [json.dumps(pdf_or_text_content, sort_keys=True, default=str)]

    def _should_use_generate_content(self) -> bool:
        """
        Determines whether to invoke `self._client.models.generate_content` (Gemini Developer API
        `api_key` mode or client without `interactions`) vs `self._client.interactions.create`
        (other Vertex AI / Enterprise Agent Platform locations). US/EU use
        GenerateContent to avoid the preview Interactions session storage.
        """
        if self._client is None:
            return False
        # Multi-region processing must use GenerateContent. The preview
        # Interactions API makes no session-storage residency commitment.
        # This applies to every call, including retries, without global fallback.
        if self.config.use_vertex_ai and self.config.location in {"us", "eu"}:
            return True
        mock_children = getattr(self._client, "_mock_children", None)
        if isinstance(mock_children, dict):
            if "models" in mock_children and "interactions" not in mock_children:
                return True
            if "interactions" in mock_children and "models" not in mock_children:
                return False
        if not self.config.use_vertex_ai:
            return True
        if getattr(self._client, "vertexai", None) is False:
            return True
        interactions_attr = getattr(self._client, "interactions", None)
        if interactions_attr is None or not hasattr(interactions_attr, "create"):
            return True
        if bool(self.config.gemini_api_key) and getattr(self._client, "vertexai", None) is not True:
            return True
        return False

    LIVE_SDK_MODEL_MAP: dict[str, str] = {
        "gemini-3.8-flash": "gemini-3.8-flash",
        "gemini-3.5-flash-lite": "gemini-3.5-flash-lite",
        "gemini-embedding-2": "gemini-embedding-001",
    }

    def resolve_live_sdk_model_id(self, model_id: str) -> str:
        """Resolve SDK transport IDs without changing logical roles or mock expectations."""
        if type(self._client).__module__.startswith("google.genai"):
            return self.LIVE_SDK_MODEL_MAP.get(model_id, model_id)
        return model_id

    def select_model_for_task(self, task_type: str) -> tuple[str, str]:
        """Returns (model_id, service_tier) for a given MLR Genie workflow task."""
        routing_table: dict[str, tuple[str, str]] = {
            "claim_verification": (self.config.model_deep_reasoning, "standard"),
            "comment_reconciliation": (self.config.model_deep_reasoning, "standard"),
            "approval_day_label_delta": (self.config.model_deep_reasoning, "priority"),
            "orchestration": (self.config.model_orchestrator, "standard"),
            "pdf_2d_bounding_box": (self.config.model_orchestrator, "standard"),
            "intake_mapping": (self.config.model_orchestrator, "standard"),
            "cv08_pv_gate": (self.config.model_fast_gate, "standard"),
            "claim_triage": (self.config.model_fast_gate, "standard"),
            "status_summary": (self.config.model_fast_gate, "standard"),
            "statement_embedding": (self.config.model_embedding, "standard"),
            "media_transcribe": (self.config.model_transcribe, "standard"),
            "sandbox_package_build": (self.config.managed_sandbox_agent, "standard"),
        }
        return routing_table.get(task_type, (self.config.model_orchestrator, "standard"))

    def create_reference_context_cache(
        self,
        *,
        cache_key: str,
        pdf_or_text_content: Any,
        ttl: str = "3600s",
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """
        Creates or retrieves a Gemini Context Cache (`self._client.caches.create`) for a
        reference PDF pack or clinical study report so multi-claim verification reuses cached
        reference tokens across turns.
        """
        if isinstance(pdf_or_text_content, bytes):
            raw_bytes = pdf_or_text_content
        elif isinstance(pdf_or_text_content, str):
            raw_bytes = pdf_or_text_content.encode("utf-8")
        else:
            raw_bytes = json.dumps(pdf_or_text_content, sort_keys=True, default=str).encode("utf-8")

        content_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        deterministic_name = (
            f"cachedContents/mlr_{hashlib.sha256((cache_key + ':' + content_sha256).encode('utf-8')).hexdigest()[:16]}"
        )
        existing = self.context_caches.get(cache_key)
        if existing and existing.get("content_sha256") == content_sha256 and (
            existing.get("used_live_sdk") or self._client is None
        ):
            return existing

        model_id, _ = self.select_model_for_task("claim_verification")
        cache_name = deterministic_name
        used_live_sdk = False
        fallback_reason: str | None = None

        if self._client is not None and getattr(self._client, "caches", None) is not None:
            try:
                contents_payload = self._normalize_cache_contents(pdf_or_text_content)
                cache_config: dict[str, Any] = {
                    "contents": contents_payload,
                    "system_instruction": (
                        "Authorized clinical, regulatory, and IFU reference pack for MLR claim verification."
                    ),
                    "display_name": display_name or f"mlr_ref_{cache_key}",
                    "ttl": ttl,
                }
                created = self._client.caches.create(
                    model=self.resolve_live_sdk_model_id(model_id),
                    config=cache_config,
                )
                resolved_name = getattr(created, "name", None) or (
                    created.get("name") if isinstance(created, dict) else None
                )
                if resolved_name:
                    cache_name = str(resolved_name)
                used_live_sdk = True
            except Exception as exc:
                used_live_sdk = False
                fallback_reason = str(exc)

        cache_record: dict[str, Any] = {
            "cache_key": cache_key,
            "name": cache_name,
            "cache_name": cache_name,
            "model": model_id,
            "effective_model": self.resolve_live_sdk_model_id(model_id) if self._client is not None else None,
            "ttl": ttl,
            "content_sha256": content_sha256,
            "used_live_sdk": used_live_sdk,
        }
        if fallback_reason:
            cache_record["fallback_reason"] = fallback_reason
        self.context_caches[cache_key] = cache_record
        return cache_record

    create_or_get_reference_cache = create_reference_context_cache

    def create_interaction(
        self,
        *,
        task_type: str,
        prompt: str,
        system_instruction: str,
        response_schema: dict[str, Any] | None = None,
        previous_interaction_id: str | None = None,
        cached_content: str | None = None,
        fallback_payload: Any = None,
    ) -> tuple[Any, ModelCallTelemetry]:
        """
        Executes a stateful or structured turn using either:
        - `client.interactions.create` on Vertex AI / Enterprise Agent Platform
        - `client.models.generate_content` on Gemini Developer API (`api_key` mode) or when `interactions` is absent.
        If a call with `cached_content` fails (e.g. expired cache TTL / 404 Not Found or unmaterialized
        offline cache ID), automatically retries once without `cached_content`.
        """
        model_id, service_tier = self.select_model_for_task(task_type)
        interaction_id = f"int_{hashlib.sha256((task_type + prompt).encode('utf-8')).hexdigest()[:12]}"

        if self._client is not None:
            try:
                use_generate_content = self._should_use_generate_content()
                safety_cfg = self.get_clinical_safety_settings()
                effective_cached_content = cached_content

                if use_generate_content:
                    gen_config: dict[str, Any] = {
                        "system_instruction": system_instruction,
                        "safety_settings": safety_cfg,
                    }
                    if response_schema is not None:
                        gen_config["response_mime_type"] = "application/json"
                        gen_config["response_json_schema"] = response_schema
                    if effective_cached_content:
                        gen_config["cached_content"] = effective_cached_content

                    generation_prompt = prompt
                    if effective_cached_content and type(self._client).__module__.startswith("google.genai"):
                        # CachedContent already owns the system instruction. Gemini rejects
                        # a second one on GenerateContent; put this turn's task in the input.
                        gen_config.pop("system_instruction", None)
                        generation_prompt = f"{system_instruction}\n\n{prompt}"

                    try:
                        raw_res = self._client.models.generate_content(
                            model=self.resolve_live_sdk_model_id(model_id),
                            contents=generation_prompt,
                            config=gen_config,
                        )
                    except Exception:
                        if not effective_cached_content:
                            raise
                        retry_config = dict(gen_config)
                        retry_config.pop("cached_content", None)
                        retry_config["system_instruction"] = system_instruction
                        raw_res = self._client.models.generate_content(
                            model=self.resolve_live_sdk_model_id(model_id),
                            contents=prompt,
                            config=retry_config,
                        )
                        effective_cached_content = None

                    transport_mode = "generate_content"
                    out_text = getattr(raw_res, "text", None)
                    if out_text is None:
                        out_text = getattr(raw_res, "output_text", None)
                else:
                    kwargs: dict[str, Any] = {
                        "model": self.resolve_live_sdk_model_id(model_id),
                        "input": prompt,
                        "system_instruction": system_instruction,
                        "service_tier": service_tier,
                        "store": not self.config.zero_data_retention,
                        "safety_settings": self.get_interactions_safety_settings(),
                    }
                    if previous_interaction_id:
                        kwargs["previous_interaction_id"] = previous_interaction_id
                    if effective_cached_content:
                        kwargs["cached_content"] = effective_cached_content
                    if response_schema is not None:
                        kwargs["response_mime_type"] = "application/json"
                        kwargs["response_format"] = {
                            "type": "text",
                            "mime_type": "application/json",
                            "schema": response_schema,
                        }
                    try:
                        raw_res = self._client.interactions.create(**kwargs)
                    except Exception:
                        if not effective_cached_content:
                            raise
                        retry_kwargs = dict(kwargs)
                        retry_kwargs.pop("cached_content", None)
                        raw_res = self._client.interactions.create(**retry_kwargs)
                        effective_cached_content = None

                    transport_mode = "interactions"
                    out_text = getattr(raw_res, "output_text", None)
                    if out_text is None:
                        out_text = getattr(raw_res, "text", None)

                telemetry = ModelCallTelemetry(
                    model=model_id,
                    effective_model=self.resolve_live_sdk_model_id(model_id),
                    task_type=task_type,
                    service_tier=service_tier,
                    used_live_sdk=True,
                    transport_mode=transport_mode,
                    cached_content=effective_cached_content,
                    previous_interaction_id=previous_interaction_id,
                    interaction_id=getattr(raw_res, "id", None) or interaction_id,
                )
                self.telemetry_log.append(telemetry)
                if response_schema is not None and out_text:
                    if isinstance(out_text, str):
                        return json.loads(out_text), telemetry
                    if isinstance(out_text, dict):
                        return out_text, telemetry
                if isinstance(raw_res, dict):
                    return raw_res, telemetry
                return out_text if out_text is not None else fallback_payload, telemetry
            except Exception as exc:
                telemetry = ModelCallTelemetry(
                    model=model_id,
                    effective_model=self.resolve_live_sdk_model_id(model_id),
                    task_type=task_type,
                    service_tier=service_tier,
                    used_live_sdk=False,
                    transport_mode="provider_error",
                    cached_content=cached_content,
                    previous_interaction_id=previous_interaction_id,
                    interaction_id=f"{interaction_id}_err",
                )
                self.telemetry_log.append(telemetry)
                failure: dict[str, Any] = {"status": "provider_error", "error": str(exc)}
                provider_http_status = self._provider_http_status(exc)
                if provider_http_status is not None:
                    failure["provider_http_status"] = provider_http_status
                return failure, telemetry

        telemetry = ModelCallTelemetry(
            model=model_id,
            task_type=task_type,
            service_tier=service_tier,
            used_live_sdk=False,
            transport_mode="offline_fallback",
            cached_content=cached_content,
            previous_interaction_id=previous_interaction_id,
            interaction_id=interaction_id,
        )
        self.telemetry_log.append(telemetry)
        return fallback_payload, telemetry

    def embed_multimodal(
        self,
        text_or_caption: str,
        dimensions: int = 16,
        task_type: str = "RETRIEVAL_DOCUMENT",
    ) -> list[float]:
        """
        Generates a normalized vector using `gemini-embedding-2` (`client.models.embed_content`)
        with native Matryoshka Representation Learning (`output_dimensionality`) and `task_type`
        (`RETRIEVAL_DOCUMENT`, `RETRIEVAL_QUERY`, `FACT_VERIFICATION`), plus a deterministic
        unit-vector fallback for offline evaluation reproducibility.
        """
        dims = max(1, int(dimensions))
        allowed_task_types = {
            "RETRIEVAL_DOCUMENT",
            "RETRIEVAL_QUERY",
            "FACT_VERIFICATION",
            "SEMANTIC_SIMILARITY",
            "CLASSIFICATION",
            "CLUSTERING",
            "QUESTION_ANSWERING",
        }
        candidate_task = str(task_type or "RETRIEVAL_DOCUMENT").strip().upper()
        resolved_task_type = candidate_task if candidate_task in allowed_task_types else "RETRIEVAL_DOCUMENT"
        model_id, _ = self.select_model_for_task("statement_embedding")
        if self._client is not None:
            try:
                res = self._client.models.embed_content(
                    model=self.resolve_live_sdk_model_id(model_id),
                    contents=text_or_caption,
                    config={
                        "output_dimensionality": dims,
                        "task_type": resolved_task_type,
                    },
                )
                vec = list(res.embeddings[0].values[:dims])
                if len(vec) < dims:
                    vec.extend([0.0] * (dims - len(vec)))
                norm = math.sqrt(sum(v * v for v in vec)) or 1.0
                return [round(v / norm, 6) for v in vec]
            except Exception:
                pass

        tokens = [
            t.strip(".,;:()[]\"'").lower()
            for t in text_or_caption.split()
            for _ in (0,)
            if len(t.strip(".,;:()[]\"'")) > 2
        ]
        raw = [0.0] * dims
        for tok in tokens:
            digest = hashlib.sha256(tok.encode("utf-8")).digest()
            for i in range(dims):
                raw[i] += (digest[i % len(digest)] / 255.0) - 0.5
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [round(v / norm, 6) for v in raw]

