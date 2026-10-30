import { describe, expect, it, vi } from "vitest";
import {
  COMMSRX_ARTIFACT_NAMES, MlrIntegrationError, runCommsRxRoundtrip, verifyCamsSource,
  type CamsVerificationResponse, type CommsRxRoundtripResponse, type MlrClientOptions,
  type RunCommsRxRoundtripInput,
} from "./camsCommsRxClient";

const receipt = { claim_id: "claim-1", used_live_sdk: true, effective_model: "gemini-3.1-pro-preview", transport: "generate_content" };
const cams: CamsVerificationResponse = {
  job_id: "job-1", claim_id: "claim-1", coordinateSpace: "pdf_points", pageDimensions: { width: 612, height: 792 },
  anchor: { page: 2, boundingBox: { x: 54, y: 96, width: 200, height: 42 }, verdict: "supported",
    verifiedBy: "gemini-3.1-pro-preview+pymupdf", verifiedAt: "2026-10-30T12:00:00Z", verbatimQuote: "Supported quote." },
  telemetry_receipts: [receipt],
};
const input: RunCommsRxRoundtripInput = {
  bodyText: "Signal improved.", claims: [{ id: "external", text: "Signal improved." }],
  referencePdf: new Uint8Array([37, 80, 68, 70]),
  comments: [{ commentId: "medical", claimId: "external", reviewerName: "Reviewer", reviewerRole: "Medical",
    timestamp: "2026-10-30T12:00:00Z", anchoredPassage: "Signal improved.", commentText: "Use increased." }],
  acceptedEdits: [{ commentId: "medical", edits: [{ targetText: "improved", replacementText: "increased" }] }],
};
function roundtrip(): CommsRxRoundtripResponse {
  return {
    job_id: "job-1", state: "draft", requires_human_resolution: false,
    claims: [{ claim_id: "claim-1", text: "Signal increased.", verdict: "supported" }],
    artifacts: Object.fromEntries(COMMSRX_ARTIFACT_NAMES.map(name => [name, {
      filename: name, download_url: `/integrations/commsrx/jobs/job-1/artifacts/${name}`,
      media_type: name.endsWith(".pdf") ? "application/pdf" : "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      bytes: 4, sha256: "a".repeat(64),
    }])) as CommsRxRoundtripResponse["artifacts"], telemetry_receipts: [receipt],
  };
}
function setup(response: unknown) {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(response));
  const options: MlrClientOptions = { baseUrl: "https://mlr.example/api/", token: "signed-test-token", fetch: fetcher };
  return { fetcher, options };
}

describe("CAMS source-anchor contract", () => {
  it("posts multipart claims and PDF bytes with bearer auth and returns the exact per-source patch", async () => {
    const { options, fetcher } = setup(cams);
    const result = await verifyCamsSource(options, { claimText: "Supported quote.", referencePdf: new Uint8Array([1, 2, 3]),
      source: { sourceId: "source-1" }, requireLive: true });
    expect(result).toEqual(cams);
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toBe("https://mlr.example/api/integrations/cams/verify-source");
    expect(init?.headers).toEqual({ Authorization: "Bearer signed-test-token" });
    expect(String(url)).not.toContain("signed-test-token");
    const form = init?.body as FormData;
    expect(JSON.parse(form.get("payload") as string)).toEqual({ claimText: "Supported quote.", source: { sourceId: "source-1" }, requireLive: true });
    expect(await (form.get("reference_pdf") as Blob).arrayBuffer()).toEqual(new Uint8Array([1, 2, 3]).buffer);
    expect(init?.redirect).toBe("error");
  });

  it.each(["supported", "contradicted", "insufficient"] as const)("accepts the CAMS verdict %s", async verdict => {
    const { options } = setup({ ...cams, anchor: { ...cams.anchor, verdict } });
    expect((await verifyCamsSource(options, { claimText: "Claim", referencePdf: new Blob() })).anchor.verdict).toBe(verdict);
  });

  it("permits missing coordinates for insufficient evidence", async () => {
    const { options } = setup({ ...cams, anchor: { verdict: "insufficient", verifiedBy: "mlr-genie:offline+pymupdf", verifiedAt: cams.anchor.verifiedAt } });
    expect((await verifyCamsSource(options, { claimText: "Claim", referencePdf: new Blob() })).anchor.page).toBeUndefined();
  });

  it.each([
    { page: 0 }, { page: 1.5 }, { verdict: "partially_supported" }, { verifiedAt: "not a date" },
    { boundingBox: { x: 0, y: 0, width: 800, height: 42 } }, { verbatimQuote: "" },
  ])("rejects an invalid CAMS anchor %j", async patch => {
    const { options } = setup({ ...cams, anchor: { ...cams.anchor, ...patch } });
    await expect(verifyCamsSource(options, { claimText: "Claim", referencePdf: new Blob() })).rejects.toBeInstanceOf(MlrIntegrationError);
  });
});

describe("CommsRx roundtrip contract", () => {
  it("submits claims, comments and accepted edits and downloads all four authenticated artifacts", async () => {
    const result = roundtrip();
    const { options, fetcher } = setup(result);
    fetcher.mockImplementation(async (url, init) => {
      if (init?.method === "POST") return Response.json(result);
      const artifact = Object.values(result.artifacts).find(a => String(url).endsWith(a.download_url))!;
      return new Response(new Uint8Array([1, 2, 3, 4]), { headers: { "Content-Type": artifact.media_type } });
    });
    const received = await runCommsRxRoundtrip(options, input);
    expect(Object.keys(received.files)).toEqual([...COMMSRX_ARTIFACT_NAMES]);
    expect(fetcher).toHaveBeenCalledTimes(5);
    const form = fetcher.mock.calls[0][1]?.body as FormData;
    const submitted = JSON.parse(form.get("payload") as string);
    expect(submitted.claims).toEqual(input.claims);
    expect(submitted.comments).toEqual(input.comments);
    expect(submitted.acceptedEdits).toEqual(input.acceptedEdits);
    for (const [, init] of fetcher.mock.calls) expect(init?.headers).toEqual({ Authorization: "Bearer signed-test-token" });
    for (const file of Object.values(received.files)) expect(file.size).toBe(4);
  });

  it("rejects absent audit PDFs instead of substituting the annotated reference", async () => {
    const result = roundtrip();
    const incomplete = { ...result.artifacts } as Record<string, unknown>;
    delete incomplete["audit_summary.pdf"];
    const { options, fetcher } = setup({ ...result, artifacts: incomplete });
    await expect(runCommsRxRoundtrip(options, input)).rejects.toBeInstanceOf(MlrIntegrationError);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("never forwards the session token to a foreign artifact URL", async () => {
    const result = roundtrip();
    result.artifacts["redline.docx"].download_url = "https://other.example/redline.docx";
    const { options, fetcher } = setup(result);
    await expect(runCommsRxRoundtrip(options, input)).rejects.toBeInstanceOf(MlrIntegrationError);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("rejects a truncated artifact download", async () => {
    const result = roundtrip();
    const { options, fetcher } = setup(result);
    fetcher.mockImplementation(async (_, init) => init?.method === "POST" ? Response.json(result) :
      new Response(new Uint8Array([1]), { headers: { "Content-Type": "application/pdf" } }));
    await expect(runCommsRxRoundtrip(options, input)).rejects.toBeInstanceOf(MlrIntegrationError);
  });

  it.each([401, 403, 409, 422, 502])("preserves HTTP %s details for the caller", async status => {
    const { options, fetcher } = setup(null);
    const detail = { detail: { job_id: "job-1", requires_human_resolution: true } };
    fetcher.mockResolvedValue(Response.json(detail, { status }));
    await expect(runCommsRxRoundtrip(options, input)).rejects.toMatchObject({ status, detail });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("propagates cancellation without retrying mutations", async () => {
    const { options, fetcher } = setup(null);
    const controller = new AbortController();
    controller.abort();
    fetcher.mockRejectedValue(new DOMException("Aborted", "AbortError"));
    await expect(runCommsRxRoundtrip({ ...options, signal: controller.signal }, input)).rejects.toMatchObject({ name: "AbortError" });
    expect(fetcher.mock.calls[0][1]?.signal).toBe(controller.signal);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
