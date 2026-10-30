/**
 * Behavioral guidance and system prompt constants for the MLR Genie Agent
 * running on Google ADK (google-adk) and the Google GenAI SDK (google-genai >= 2.30.1).
 *
 * Invariant 10: Heuristics Advise, Humans Decide (No Blocking Regexes).
 * All communication strategy guidance lives here in prompt.ts, in tool schema
 * descriptions, or as severity: "note" advisory findings — never as a blocking regex.
 */

export interface GeminiModelRouteDescriptor {
  taskCategory: string;
  modelId:
    | "gemini-4-argon"
    | "gemini-3.8-flash"
    | "gemini-3.5-flash-lite"
    | "gemini-embedding-2"
    | "gemini-3.5-transcribe"
    | "antigravity-preview-09-2026";
  thinkingLevel: "off" | "minimal" | "low" | "medium" | "high";
  serviceTier: "standard" | "priority";
  rationale: string;
}

export const GEMINI_MODEL_ROUTING_MATRIX: Record<string, GeminiModelRouteDescriptor> = {
  orchestration_and_intake: {
    taskCategory: "Genie Orchestration, Intake & Context Assembly",
    modelId: "gemini-3.8-flash",
    thinkingLevel: "low",
    serviceTier: "standard",
    rationale: "1M context window, sub-second tool dispatch, configurable thinking levels."
  },
  multimodal_pdf_anchoring: {
    taskCategory: "Multimodal PDF Coordinate Anchoring & Chart Verification",
    modelId: "gemini-3.8-flash",
    thinkingLevel: "medium",
    serviceTier: "standard",
    rationale: "Native 2D bounding-box detection ([ymin, xmin, ymax, xmax]) paired with PyMuPDF."
  },
  deep_claim_and_conflict_audit: {
    taskCategory: "Claim-to-Evidence Verification & Reviewer Conflict Reconciliation",
    modelId: "gemini-4-argon",
    thinkingLevel: "high",
    serviceTier: "standard",
    rationale: "Frontier scientific & regulatory reasoning across 1M context window."
  },
  approval_day_label_delta: {
    taskCategory: "Approval-Day Final Label / Clearance Delta Engine",
    modelId: "gemini-4-argon",
    thinkingLevel: "high",
    serviceTier: "priority",
    rationale: "Vertex AI Priority Inference guarantees low-latency SLA on FDA/EMA approval day."
  },
  pv_safety_screening_gate: {
    taskCategory: "24-Hour Pharmacovigilance (ICSR) Screening Gate (CV-08)",
    modelId: "gemini-3.5-flash-lite",
    thinkingLevel: "minimal",
    serviceTier: "standard",
    rationale: "Ultra-fast, deterministic high-throughput screening before any workflow step."
  },
  statement_reuse_embeddings: {
    taskCategory: "4-Dimension Reviewed Statement & Decision Library",
    modelId: "gemini-embedding-2",
    thinkingLevel: "off",
    serviceTier: "standard",
    rationale: "Multimodal text, table & PDF snippet embeddings in AlloyDB / Cloud SQL pgvector."
  },
  multimedia_transcript_ingestion: {
    taskCategory: "Multimedia Broadcast & Earnings Call Transcription",
    modelId: "gemini-3.5-transcribe",
    thinkingLevel: "off",
    serviceTier: "standard",
    rationale: "Time-coded transcript segments for broadcast spots, earnings calls, and booths."
  },
  remote_package_assembly: {
    taskCategory: "Sandboxed Submission Package Assembly & Multi-File Diffs",
    modelId: "antigravity-preview-09-2026",
    thinkingLevel: "medium",
    serviceTier: "standard",
    rationale: "Agentic harness for complex multi-file submission bundle verification."
  }
};

export const MLR_GENIE_SYSTEM_PROMPT = `
You are MLR Genie, an enterprise agent built on the Google Agent Development Kit (ADK)
and Google Cloud Platform (Vertex AI, Cloud Run, AlloyDB pgvector, Cloud Spanner, Model Armor)
for Pharmaceutical, Biotech, and MedTech communications teams and MLR coordinators.

Core Operating Rules:
1. One Genie in Front, Bounded Specialist Sub-Agents Behind It:
   - Never ask the user to choose a model, configure prompts, or manage orchestration graphs.
2. Evidence-First, Never Invented:
   - Every factual, clinical, scientific, regulatory, or device performance statement must link
     to an exact page, section, table, figure, or bounding-box coordinate in an uploaded source
     document. If evidence is missing or ambiguous, stop and create a human decision item.
3. Strict Human vs. Model Provenance (Invariant 1):
   - Never classify model-generated text (AssetDrafted) as customer-supplied fact (AssetEditRecorded / leaderSupplied).
   - Never attach hidden model-suggested fields to a human confirmation click unless every word is visibly rendered first.
4. Heuristics Advise, Humans Decide (Invariant 10):
   - Advisory heuristics surface severity: "note" findings using Pharma Comms vocabulary:
     - Within Approved Meaning (Ready for MLR Review)
     - Needs a Closer Read
     - Uses Ruled-Out Phrasing
     - Missing Locked Clinical Data
   - Never block tool execution with a regex or stem matcher.
5. Zero Silent Mutation on Apply:
   - Applying accepted changes or keeping source text is 100% atomic (all-or-nothing per comment)
     and never silently strips periods or rewrites punctuation.
6. Explicit Mandate & Release Gate:
   - Preparing a package is separate from releasing or uploading it to Veeva PromoMats or Aprimo.
`.trim();
