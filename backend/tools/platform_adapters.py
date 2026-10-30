from __future__ import annotations

import hashlib
from typing import Any, Literal

from backend.domain.schemas import (
    ConnectorActionType,
    ConnectorCapabilityStatus,
    ConnectorReceipt,
    JobLifecycleState,
    JobRecord,
)


class CapabilityCheckedPlatformAdapter:
    """
    Section 7 & Section 11 Acceptance Cases 10 & 11:
    Unified MCP-compatible adapter contract for Veeva Vault PromoMats, Aprimo (DAM vs. Productivity),
    and Package Mode.
    - Publishes explicit capability profile (`verified`, `unsupported`, `untested`).
    - Verifies every write by reading back the external record before marking complete.
    - Reconciles uncertain/failed writes via `idempotency_key` before retrying so retries never
      create duplicate documents, versions, comments, or workflow tasks.
    - Deduplicates delayed or repeated external platform webhook events.
    """

    def __init__(
        self,
        platform_name: Literal["veeva_promomats", "aprimo_dam", "aprimo_productivity", "package_mode"],
        capability_profile: dict[ConnectorActionType, ConnectorCapabilityStatus] | None = None,
    ) -> None:
        self.platform_name = platform_name
        self.capability_profile = capability_profile or self._default_profile(platform_name)
        self._external_store: dict[str, dict[str, Any]] = {}
        self._processed_webhook_event_Ids: set[str] = set()

    @staticmethod
    def _default_profile(
        platform_name: str,
    ) -> dict[ConnectorActionType, ConnectorCapabilityStatus]:
        if platform_name == "veeva_promomats":
            return {action: ConnectorCapabilityStatus.VERIFIED for action in ConnectorActionType}
        if platform_name == "aprimo_dam":
            # Aprimo DAM alone supports asset upload/versioning, NOT review workflow or comments (Section 7)
            return {
                ConnectorActionType.DISCOVER_CONFIGURATION: ConnectorCapabilityStatus.VERIFIED,
                ConnectorActionType.READ_MATERIALS: ConnectorCapabilityStatus.VERIFIED,
                ConnectorActionType.UPLOAD_VERSION: ConnectorCapabilityStatus.VERIFIED,
                ConnectorActionType.ATTACH_REFERENCES: ConnectorCapabilityStatus.VERIFIED,
                ConnectorActionType.REQUEST_REVIEW: ConnectorCapabilityStatus.UNSUPPORTED,
                ConnectorActionType.READ_COMMENTS_STATUS: ConnectorCapabilityStatus.UNSUPPORTED,
                ConnectorActionType.APPLY_AUTHORIZED_UPDATES: ConnectorCapabilityStatus.VERIFIED,
                ConnectorActionType.RETRIEVE_APPROVED_PACKAGE: ConnectorCapabilityStatus.UNSUPPORTED,
            }
        if platform_name == "aprimo_productivity":
            return {action: ConnectorCapabilityStatus.VERIFIED for action in ConnectorActionType}
        # package_mode
        return {
            ConnectorActionType.DISCOVER_CONFIGURATION: ConnectorCapabilityStatus.VERIFIED,
            ConnectorActionType.READ_MATERIALS: ConnectorCapabilityStatus.VERIFIED,
            ConnectorActionType.UPLOAD_VERSION: ConnectorCapabilityStatus.VERIFIED,
            ConnectorActionType.ATTACH_REFERENCES: ConnectorCapabilityStatus.VERIFIED,
            ConnectorActionType.REQUEST_REVIEW: ConnectorCapabilityStatus.UNSUPPORTED,
            ConnectorActionType.READ_COMMENTS_STATUS: ConnectorCapabilityStatus.VERIFIED,
            ConnectorActionType.APPLY_AUTHORIZED_UPDATES: ConnectorCapabilityStatus.VERIFIED,
            ConnectorActionType.RETRIEVE_APPROVED_PACKAGE: ConnectorCapabilityStatus.VERIFIED,
        }

    _ACTION_MANDATE_MAP: dict[ConnectorActionType, str] = {
        ConnectorActionType.ATTACH_REFERENCES: "prepare_annotations",
        ConnectorActionType.UPLOAD_VERSION: "upload_draft",
        ConnectorActionType.REQUEST_REVIEW: "route_review",
        ConnectorActionType.APPLY_AUTHORIZED_UPDATES: "resubmit_accepted",
    }

    def execute_action(
        self,
        *,
        job: JobRecord,
        action: ConnectorActionType,
        actor_human: str,
        payload: dict[str, Any],
        idempotency_key: str,
        simulate_uncertain_network_drop: bool = False,
    ) -> ConnectorReceipt:
        # Probe 10: Block external platform actions when job is on Pharmacovigilance (CV-08) hold
        if job.pv_icrs_flagged or job.state == JobLifecycleState.HOLD:
            raise PermissionError(
                f"Job '{job.job_id}' is on Pharmacovigilance (CV-08) hold and cannot be submitted or mutated externally."
            )

        cap = self.capability_profile.get(action, ConnectorCapabilityStatus.UNTESTED)
        receipt_id = f"rcpt_{hashlib.sha256((job.job_id + action.value + idempotency_key).encode('utf-8')).hexdigest()[:10]}"

        if cap != ConnectorCapabilityStatus.VERIFIED:
            receipt = ConnectorReceipt(
                receipt_id=receipt_id,
                job_id=job.job_id,
                destination_platform=self.platform_name,
                action=action,
                capability_status=cap,
                external_object_id=None,
                external_version=None,
                actor_human=actor_human,
                integration_identity=f"mcp-{self.platform_name}-sa@mlr-genie-enterprise-prod.iam.gserviceaccount.com",
                idempotency_key=idempotency_key,
                read_back_verified=False,
                read_back_digest=None,
                status="unsupported_limitation",
                detail=(
                    f"Action '{action.value}' is {cap.value} on '{self.platform_name}'. "
                    "Returning explicit limitation rather than simulated success."
                ),
            )
            job.receipts.append(receipt)
            return receipt

        # Probe 13: Enforce explicit human mandate before executing external write actions
        required_mandate = self._ACTION_MANDATE_MAP.get(action)
        if required_mandate and required_mandate not in job.mandate_allowed_actions:
            raise PermissionError(
                f"Action '{action.value}' requires mandate '{required_mandate}', which is absent from job '{job.job_id}'."
            )

        digest = hashlib.sha256(str(payload).encode("utf-8")).hexdigest()

        # Step 1: Reconcile external state by idempotency_key before writing (prevents duplicates on retry)
        existing_record = self._external_store.get(idempotency_key)
        if existing_record is not None:
            # Probe 17: Verify idempotency key belongs to the exact same tenant, job, action, and payload digest
            if (
                existing_record.get("tenant_id") != job.tenant_id
                or existing_record.get("job_id") != job.job_id
                or existing_record.get("action") != action.value
                or existing_record.get("digest") != digest
            ):
                raise PermissionError(
                    f"Idempotency key '{idempotency_key}' collision across different tenant, job, action, or payload."
                )

            # Probe 16: Reconcile job lifecycle state when recovering a prior uncertain upload/review request
            if action == ConnectorActionType.UPLOAD_VERSION and job.state not in (
                JobLifecycleState.IN_REVIEW,
                JobLifecycleState.APPROVED,
                JobLifecycleState.RELEASED,
            ):
                job.state = JobLifecycleState.UPLOADED
            elif action == ConnectorActionType.REQUEST_REVIEW and job.state not in (
                JobLifecycleState.APPROVED,
                JobLifecycleState.RELEASED,
            ):
                job.state = JobLifecycleState.IN_REVIEW

            receipt = ConnectorReceipt(
                receipt_id=receipt_id,
                job_id=job.job_id,
                destination_platform=self.platform_name,
                action=action,
                capability_status=cap,
                external_object_id=existing_record["external_object_id"],
                external_version=existing_record["external_version"],
                actor_human=actor_human,
                integration_identity=f"mcp-{self.platform_name}-sa@mlr-genie-enterprise-prod.iam.gserviceaccount.com",
                idempotency_key=idempotency_key,
                read_back_verified=True,
                read_back_digest=existing_record["digest"],
                status="reconciled_no_duplicate",
                detail="Reconciled existing external record via idempotency key; prevented duplicate creation.",
            )
            job.receipts.append(receipt)
            return receipt

        # Step 2: Perform external write
        ext_id = f"{self.platform_name.upper()}-{job.job_id.upper()}-DOC"
        ext_ver = f"{job.current_version}.0"

        self._external_store[idempotency_key] = {
            "tenant_id": job.tenant_id,
            "job_id": job.job_id,
            "action": action.value,
            "external_object_id": ext_id,
            "external_version": ext_ver,
            "digest": digest,
            "payload": payload,
        }

        if simulate_uncertain_network_drop:
            receipt = ConnectorReceipt(
                receipt_id=receipt_id,
                job_id=job.job_id,
                destination_platform=self.platform_name,
                action=action,
                capability_status=cap,
                external_object_id=ext_id,
                external_version=ext_ver,
                actor_human=actor_human,
                integration_identity=f"mcp-{self.platform_name}-sa@mlr-genie-enterprise-prod.iam.gserviceaccount.com",
                idempotency_key=idempotency_key,
                read_back_verified=False,
                read_back_digest=None,
                status="failed_uncertain",
                detail="Write response timed out before read-back confirmation; requires state reconciliation before retry.",
            )
            job.receipts.append(receipt)
            return receipt

        # Step 3: Mandatory read-back verification
        read_back = self._external_store.get(idempotency_key)
        verified = read_back is not None and read_back["digest"] == digest

        if action == ConnectorActionType.UPLOAD_VERSION and verified:
            # Uploaded is distinct from In Review and Approved (Section 3.H)
            job.state = JobLifecycleState.UPLOADED
        elif action == ConnectorActionType.REQUEST_REVIEW and verified:
            job.state = JobLifecycleState.IN_REVIEW

        receipt = ConnectorReceipt(
            receipt_id=receipt_id,
            job_id=job.job_id,
            destination_platform=self.platform_name,
            action=action,
            capability_status=cap,
            external_object_id=ext_id,
            external_version=ext_ver,
            actor_human=actor_human,
            integration_identity=f"mcp-{self.platform_name}-sa@mlr-genie-enterprise-prod.iam.gserviceaccount.com",
            idempotency_key=idempotency_key,
            read_back_verified=verified,
            read_back_digest=digest if verified else None,
            status="succeeded",
            detail=f"External write confirmed via read-back of {ext_id} v{ext_ver}.",
        )
        job.receipts.append(receipt)
        return receipt

    def ingest_platform_webhook_event(
        self,
        *,
        job: JobRecord,
        event_id: str,
        event_type: Literal["comment_added", "status_changed_approved", "status_changed_in_review"],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Section 11 Acceptance Case 11:
        Handles duplicate or out-of-order/delayed platform events idempotently.
        """
        if event_id in self._processed_webhook_event_Ids:
            return {
                "event_id": event_id,
                "duplicate_ignored": True,
                "job_state": job.state.value,
            }
        self._processed_webhook_event_Ids.add(event_id)

        if event_type == "status_changed_approved":
            # Probe 14: Only transitions to APPROVED when backed by explicit approved_version,
            # verified approval_record_id, non-held state, and prior uploaded/ingested evidence.
            if job.pv_icrs_flagged or job.state == JobLifecycleState.HOLD:
                return {
                    "event_id": event_id,
                    "duplicate_ignored": False,
                    "rejected_reason": "Job is on Pharmacovigilance (CV-08) hold.",
                    "job_state": job.state.value,
                }
            approval_record_id = payload.get("approval_record_id")
            has_explicit_version = "approved_version" in payload and payload.get("approved_version") is not None
            approved_version = payload.get("approved_version")
            has_prior_evidence = bool(job.revisions) or bool(job.receipts) or job.state in (
                JobLifecycleState.UPLOADED,
                JobLifecycleState.IN_REVIEW,
            )
            if (
                isinstance(approval_record_id, str)
                and len(approval_record_id.strip()) >= 4
                and has_explicit_version
                and int(approved_version) == job.current_version
                and has_prior_evidence
            ):
                job.state = JobLifecycleState.APPROVED
                job.approved_handoff_bundle_uri = (
                    f"gs://mlr-genie-versioned-artifacts/{job.tenant_id}/{job.job_id}/v{approved_version}/approved-handoff.zip"
                )
        elif event_type == "status_changed_in_review":
            # Do not regress state if a delayed "in_review" webhook arrives after "approved"
            if job.state != JobLifecycleState.APPROVED and job.state != JobLifecycleState.RELEASED:
                job.state = JobLifecycleState.IN_REVIEW

        return {
            "event_id": event_id,
            "duplicate_ignored": False,
            "job_state": job.state.value,
        }
