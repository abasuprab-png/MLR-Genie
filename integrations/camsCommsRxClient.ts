/** Standalone browser/Node 18+ client. No imports, SDKs, or application dependencies. */
export interface MlrClientOptions {
  /** Absolute API base URL, optionally including a reverse-proxy prefix. */
  baseUrl: string;
  /** An already-issued signed session token. Never pass a bootstrap secret here. */
  token: string;
  signal?: AbortSignal;
  fetch?: typeof fetch;
}

export type ReferencePdf = Blob | ArrayBuffer | Uint8Array;
export type CamsVerdict = "supported" | "contradicted" | "insufficient";
export interface SourceMetadata { sourceId?: string; version?: string; title?: string; url?: string }
export interface CamsSourceAnchor {
  /** One-based page; omitted if no quote could be verified. */
  page?: number;
  /** PDF points (1/72 inch), origin at top left of the unrotated page. */
  boundingBox?: { x: number; y: number; width: number; height: number };
  verdict: CamsVerdict;
  verifiedBy: string;
  verifiedAt: string;
  verbatimQuote?: string;
}
export interface VerificationReceipt {
  claim_id: string;
  used_live_sdk: boolean;
  effective_model: string;
  transport: string;
}
export interface VerifyCamsSourceInput {
  claimText: string;
  referencePdf: ReferencePdf;
  source?: SourceMetadata;
  requireLive?: boolean;
}
export interface CamsVerificationResponse {
  job_id: string;
  claim_id: string;
  /** Spread this object into the CAMS per-source reference, not the whole claim. */
  anchor: CamsSourceAnchor;
  coordinateSpace: "pdf_points";
  pageDimensions?: { width: number; height: number };
  telemetry_receipts: VerificationReceipt[];
}
export interface WordingEdit { targetText: string; replacementText: string }
export interface ReviewerComment {
  commentId: string;
  /** External ID from the input claims list. */
  claimId?: string;
  reviewerName: string;
  reviewerRole: "Medical" | "Legal" | "Regulatory" | "MLR Operations";
  timestamp: string;
  anchoredPassage: string;
  commentText: string;
  proposedEdits?: WordingEdit[];
  decision?: "pending" | "accepted" | "rejected";
}
export interface RunCommsRxRoundtripInput {
  title?: string;
  bodyText: string;
  claims: Array<{ id: string; text: string }>;
  referencePdf: ReferencePdf;
  comments?: ReviewerComment[];
  /** Explicit human acceptance; each entry must reference a submitted comment. */
  acceptedEdits?: Array<{ commentId: string; edits: WordingEdit[] }>;
  source?: SourceMetadata;
  sector?: "pharma_preapproval" | "pharma_approved" | "biotech" | "medtech" | "corporate";
  requireLive?: boolean;
}
export const COMMSRX_ARTIFACT_NAMES = [
  "redline.docx", "clean_revised.docx", "audit_summary.pdf", "annotated_source.pdf",
] as const;
export type CommsRxArtifactName = typeof COMMSRX_ARTIFACT_NAMES[number];
export interface ArtifactDescriptor {
  filename: CommsRxArtifactName;
  download_url: string;
  media_type: string;
  bytes: number;
  sha256: string;
}
export interface CommsRxRoundtripResponse {
  job_id: string;
  state: string;
  requires_human_resolution: false;
  claims: Array<{ claim_id: string; text: string; verdict: CamsVerdict }>;
  artifacts: Record<CommsRxArtifactName, ArtifactDescriptor>;
  telemetry_receipts: VerificationReceipt[];
}
export interface CommsRxRoundtripResult extends CommsRxRoundtripResponse {
  /** Authenticated downloads; use these Blobs for object URLs or server-side storage. */
  files: Record<CommsRxArtifactName, Blob>;
}

export class MlrIntegrationError extends Error {
  constructor(public readonly status: number, public readonly detail: unknown) {
    super(status ? `MLR Genie request failed (${status})` : "Invalid MLR Genie integration response");
    this.name = "MlrIntegrationError";
  }
}

type JsonObject = Record<string, unknown>;
function object(value: unknown): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail("Expected an object");
  return value as JsonObject;
}
function fail(detail: string): never { throw new MlrIntegrationError(0, detail); }
function string(value: unknown): value is string { return typeof value === "string" && value.trim().length > 0; }
function number(value: unknown): value is number { return typeof value === "number" && Number.isFinite(value); }
function verdict(value: unknown): value is CamsVerdict {
  return value === "supported" || value === "contradicted" || value === "insufficient";
}
function receipts(value: unknown): void {
  if (!Array.isArray(value) || !value.length) fail("Missing verification receipts");
  for (const receipt of value) {
    const r = object(receipt);
    if (!string(r.claim_id) || typeof r.used_live_sdk !== "boolean" || !string(r.effective_model) || !string(r.transport)) {
      fail("Invalid verification receipt");
    }
  }
}
function validateCams(value: unknown): CamsVerificationResponse {
  const r = object(value), a = object(r.anchor);
  if (!string(r.job_id) || !string(r.claim_id) || r.coordinateSpace !== "pdf_points" || !verdict(a.verdict)
      || !string(a.verifiedBy) || !string(a.verifiedAt) || !/^\d{4}-\d{2}-\d{2}T/.test(a.verifiedAt)
      || !Number.isFinite(Date.parse(a.verifiedAt))) fail("Invalid CAMS verification metadata");
  if (a.page !== undefined || a.boundingBox !== undefined || a.verbatimQuote !== undefined || a.verdict === "supported") {
    const box = object(a.boundingBox), dimensions = object(r.pageDimensions);
    if (!number(a.page) || !Number.isInteger(a.page) || a.page < 1 || !string(a.verbatimQuote)
        || !number(box.x) || box.x < 0 || !number(box.y) || box.y < 0
        || !number(box.width) || box.width <= 0 || !number(box.height) || box.height <= 0
        || !number(dimensions.width) || !number(dimensions.height)
        || box.x + box.width > dimensions.width || box.y + box.height > dimensions.height) {
      fail("Invalid CAMS PDF anchor");
    }
  }
  receipts(r.telemetry_receipts);
  return value as CamsVerificationResponse;
}
const DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
function artifactMedia(name: CommsRxArtifactName): string { return name.endsWith(".pdf") ? "application/pdf" : DOCX_MEDIA; }
function validateRoundtrip(value: unknown): CommsRxRoundtripResponse {
  const r = object(value), artifacts = object(r.artifacts);
  if (!string(r.job_id) || !string(r.state) || r.requires_human_resolution !== false || !Array.isArray(r.claims)) {
    fail("Invalid CommsRx roundtrip metadata");
  }
  for (const claim of r.claims) {
    const c = object(claim);
    if (!string(c.claim_id) || !string(c.text) || !verdict(c.verdict)) fail("Invalid claim result");
  }
  for (const name of COMMSRX_ARTIFACT_NAMES) {
    const a = object(artifacts[name]);
    const expectedPath = `/integrations/commsrx/jobs/${encodeURIComponent(r.job_id)}/artifacts/${name}`;
    if (a.filename !== name || a.download_url !== expectedPath || a.media_type !== artifactMedia(name)
        || !number(a.bytes) || !Number.isSafeInteger(a.bytes) || a.bytes <= 0
        || typeof a.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(a.sha256)) {
      fail(`Invalid artifact descriptor: ${name}`);
    }
  }
  receipts(r.telemetry_receipts);
  return value as CommsRxRoundtripResponse;
}
function url(options: MlrClientOptions, path: string): string {
  const base = new URL(options.baseUrl);
  if (!/^https?:$/.test(base.protocol) || base.search || base.hash || base.username || base.password || !options.token.trim()) {
    throw new Error("Use an HTTP(S) API base URL and a nonempty signed session token");
  }
  return base.href.replace(/\/$/, "") + path;
}
async function request(options: MlrClientOptions, path: string, body?: FormData): Promise<Response> {
  const response = await (options.fetch ?? globalThis.fetch)(url(options, path), {
    method: body ? "POST" : "GET", body, headers: { Authorization: `Bearer ${options.token}` },
    signal: options.signal, credentials: "omit", cache: "no-store", redirect: "error",
  });
  if (!response.ok) {
    const raw = await response.text();
    let detail: unknown = raw;
    try { detail = JSON.parse(raw); } catch { /* Preserve non-JSON error bodies. */ }
    throw new MlrIntegrationError(response.status, detail);
  }
  return response;
}
function multipart(payload: object, pdf: ReferencePdf): FormData {
  const body = new FormData();
  body.set("payload", JSON.stringify(payload));
  const blob = pdf instanceof Blob ? pdf : new Blob([new Uint8Array(pdf).buffer], { type: "application/pdf" });
  body.set("reference_pdf", blob, "reference.pdf");
  return body;
}
async function json(response: Response): Promise<unknown> {
  try { return await response.json(); } catch { return fail("Expected a JSON response"); }
}

export async function verifyCamsSource(options: MlrClientOptions, input: VerifyCamsSourceInput): Promise<CamsVerificationResponse> {
  const { referencePdf, ...payload } = input;
  return validateCams(await json(await request(options, "/integrations/cams/verify-source", multipart(payload, referencePdf))));
}

export async function runCommsRxRoundtrip(options: MlrClientOptions, input: RunCommsRxRoundtripInput): Promise<CommsRxRoundtripResult> {
  const { referencePdf, ...payload } = input;
  const result = validateRoundtrip(await json(await request(options, "/integrations/commsrx/roundtrip", multipart(payload, referencePdf))));
  const downloaded = await Promise.all(COMMSRX_ARTIFACT_NAMES.map(async name => {
    const artifact = result.artifacts[name];
    const response = await request(options, artifact.download_url);
    const blob = await response.blob();
    if (blob.size !== artifact.bytes || blob.type.split(";")[0] !== artifact.media_type) {
      fail(`Artifact download differs from its descriptor: ${name}`);
    }
    return [name, blob] as const;
  }));
  return { ...result, files: Object.fromEntries(downloaded) as Record<CommsRxArtifactName, Blob> };
}
