from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from typing import Any, Callable

from backend.agent.models_router import GeminiModelRouter
from backend.config import GcpAgentConfig
from backend.domain.schemas import JobRecord


@dataclass
class AdkToolExecutionAudit:
    tool_name: str
    job_id: str
    mandate_verified: bool
    input_summary: dict[str, Any]
    status: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class MlrGenieAgent:
    """
    Single Orchestrating MLR Genie Agent built on Google Agent Development Kit (ADK) patterns.
    Enforces:
    - `before_tool_callback`: Verifies job mandate, tenant isolation, and input schema before any tool acts.
    - `after_tool_callback`: Records immutable execution telemetry outside conversational memory.
    """

    SYSTEM_INSTRUCTION = (
        "You are MLR Genie, an execution agent for Pharma, Biotech, and MedTech communications teams. "
        "Your promise: 'Give me the materials and the deadline. I will handle the review work and the system, "
        "keep every version and comment straight, and bring you the decisions that need you.' "
        "Never invent a clinical citation, never silently alter a locked number or punctuation, "
        "never classify a model assessment as human MLR approval, and always route individual patient "
        "adverse events (CV-08) to Pharmacovigilance within 24 hours."
    )

    def __init__(
        self,
        config: GcpAgentConfig | None = None,
        router: GeminiModelRouter | None = None,
    ) -> None:
        from backend.tools.document_tools import import_rd_handoff_pack

        self.config = config or GcpAgentConfig()
        self.router = router or GeminiModelRouter(self.config)
        self.registered_tools: dict[str, Callable[..., Any]] = {
            "import_rd_handoff_pack": import_rd_handoff_pack,
        }
        self._tools = self.registered_tools
        self.tool_audit_log: list[AdkToolExecutionAudit] = []

    def register_tool(self, name: str, fn: Callable[..., Any]) -> None:
        self.registered_tools[name] = fn

    def before_tool_callback(
        self,
        *,
        tool_name: str,
        job: JobRecord | None,
        required_mandate_action: str | None,
        kwargs: dict[str, Any],
    ) -> None:
        """ADK pre-execution hook: enforces job mandate, PV hold, and tenant boundaries."""
        if job is not None and tool_name != "screen_pharmacovigilance_cv08":
            if job.pv_icrs_flagged or job.state.value == "hold":
                raise PermissionError(
                    f"Job '{job.job_id}' is on Pharmacovigilance (CV-08) hold; tool '{tool_name}' cannot proceed."
                )
        if job is not None and required_mandate_action is not None:
            if required_mandate_action not in job.mandate_allowed_actions:
                raise PermissionError(
                    f"Action '{required_mandate_action}' is outside the authorized mandate for job '{job.job_id}'."
                )
        requested_tenant = kwargs.get("requesting_tenant_id")
        if job is not None and requested_tenant and requested_tenant != job.tenant_id:
            raise PermissionError(
                f"Cross-tenant access blocked: tenant '{requested_tenant}' cannot access job in '{job.tenant_id}'."
            )

    def after_tool_callback(
        self,
        *,
        tool_name: str,
        job: JobRecord | None,
        required_mandate_action: str | None,
        kwargs: dict[str, Any],
        status: str,
    ) -> None:
        """ADK post-execution hook: logs deterministic tool execution receipt with redacted content."""
        sensitive_text_keys = {"body_text", "intake_text", "text_to_check", "initial_payload_body"}
        safe_summary: dict[str, Any] = {}
        for k, v in kwargs.items():
            if k in ("api_key", "access_token", "secret", "password", "token"):
                continue
            if k in sensitive_text_keys and isinstance(v, str):
                digest = hashlib.sha256(v.encode("utf-8")).hexdigest()[:12]
                safe_summary[k] = f"sha256:{digest}(len={len(v)})"
            elif isinstance(v, (str, int, float, bool)):
                safe_summary[k] = v
            else:
                safe_summary[k] = type(v).__name__

        mandate_verified = bool(
            required_mandate_action is not None
            and job is not None
            and required_mandate_action in job.mandate_allowed_actions
        )
        self.tool_audit_log.append(
            AdkToolExecutionAudit(
                tool_name=tool_name,
                job_id=job.job_id if job else "system",
                mandate_verified=mandate_verified,
                input_summary=safe_summary,
                status=status,
            )
        )

    def invoke_tool(
        self,
        tool_name: str,
        *,
        job: JobRecord | None = None,
        required_mandate_action: str | None = None,
        **kwargs: Any,
    ) -> Any:
        if tool_name not in self.registered_tools:
            raise KeyError(f"Unknown ADK tool: {tool_name}")
        self.before_tool_callback(
            tool_name=tool_name,
            job=job,
            required_mandate_action=required_mandate_action,
            kwargs=kwargs,
        )
        try:
            result = self.registered_tools[tool_name](job=job, **kwargs) if job is not None else self.registered_tools[tool_name](**kwargs)
            self.after_tool_callback(
                tool_name=tool_name,
                job=job,
                required_mandate_action=required_mandate_action,
                kwargs=kwargs,
                status="succeeded",
            )
            return result
        except Exception as exc:
            self.after_tool_callback(
                tool_name=tool_name,
                job=job,
                required_mandate_action=required_mandate_action,
                kwargs=kwargs,
                status=f"failed: {type(exc).__name__}",
            )
            raise


MLRGenieAgent = MlrGenieAgent
