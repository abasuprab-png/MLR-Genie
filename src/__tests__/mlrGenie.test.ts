import { describe, expect, it } from "vitest";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  apiCreateOrSyncJobIntake,
  apiExecuteFileRoundtrip,
  apiGetJob,
  apiImportRdHandoffPack,
  apiReconcileComments,
  apiSubmitToPlatform,
  apiUploadReferencePdf,
  apiUploadWorkingDocx,
  applyAcceptedChanges,
  applyWordingSpan,
  AssetRecord,
  buildArtifactDownloadUrl,
  buildDefaultArtifactsCatalog,
  confirmHumanDecision,
  createInitialWorkspaceState,
  derivePyMuPdfPreviewsFromJob,
  extractHumanResolvedFacts,
  keepSource,
  mapFrontendCommentsToBackendPayload,
  mapFrontendJobToBackendIntakePayload,
  proposeStrategy,
  queryStatementReuseLibrary,
  ReviewerCommentItem,
  ROUNDTRIP_ARTIFACT_FILENAMES,
  runSection11AcceptanceSuite,
  splitMedicalSentences,
  validateStrategy
} from "../domain/mlrGenieEngine";
import { GEMINI_MODEL_ROUTING_MATRIX } from "../prompt";
import { App } from "../../client/src/App";

describe("MLR Genie TypeScript Domain & Mandatory PR Gatekeeper Invariants", () => {
  it("passes all 12 Section 11 Acceptance Cases", () => {
    const results = runSection11AcceptanceSuite();
    const keys = Object.keys(results);
    expect(keys).toHaveLength(12);
    for (const [caseName, outcome] of Object.entries(results)) {
      expect(outcome.passed, `Expected ${caseName} to pass`).toBe(true);
    }
  });

  it("Invariant 10: validateStrategy and proposeStrategy emit severity: 'note' advisory findings and never block or throw", () => {
    const res = proposeStrategy(
      "Aurenix cures 100% of patients overnight with zero side effects.",
      {
        ruledOutPhrases: ["cures 100%", "zero side effects"],
        lockedClinicalFigures: ["p=0.0012", "95% CI: 0.54-0.82"]
      }
    );

    expect(res.validation.advisoryFindings.length).toBeGreaterThanOrEqual(3);
    for (const finding of res.validation.advisoryFindings) {
      expect(finding.severity).toBe("note");
    }

    const cleanRes = validateStrategy(
      "In AUR-301, Aurenix demonstrated p=0.0012 (95% CI: 0.54-0.82).",
      {
        ruledOutPhrases: ["cures 100%"],
        lockedClinicalFigures: ["p=0.0012", "95% CI: 0.54-0.82"]
      }
    );
    expect(cleanRes.advisoryFindings[0].band).toBe(
      "Within Approved Meaning (Ready for MLR Review)"
    );
    expect(cleanRes.advisoryFindings[0].severity).toBe("note");
  });

  it("Invariant 1: extractHumanResolvedFacts compares ONLY asset.payload.body against AssetEditRecorded revisions, never AssetDrafted", () => {
    const asset: AssetRecord = {
      assetId: "asset-test-1",
      title: "Launch Press Release",
      payload: {
        body: "Baseline sentence one. Baseline sentence two."
      },
      revisions: [
        {
          revisionId: "rev-model-draft",
          eventType: "AssetDrafted",
          actorId: "agent:gemini-3.8-flash",
          body: "Baseline sentence one. Model generated sentence that must never be classified as human fact.",
          timestampIso: "2026-10-28T10:00:00Z"
        },
        {
          revisionId: "rev-human-edit",
          eventType: "AssetEditRecorded",
          actorId: "human:vp.comms@pharma.com",
          body: "Baseline sentence one. Model generated sentence that must never be classified as human fact. Human verified hepatic monitoring every 8 weeks.",
          timestampIso: "2026-10-28T10:05:00Z"
        }
      ]
    };

    const facts = extractHumanResolvedFacts(asset);
    expect(facts).toHaveLength(1);
    expect(facts[0].revisionId).toBe("rev-human-edit");
    expect(facts[0].provenanceClassification).toBe("leaderSupplied");
    expect(facts[0].addedSentences).toEqual([
      "Human verified hepatic monitoring every 8 weeks."
    ]);
    expect(
      facts.some((f) => f.revisionId === "rev-model-draft")
    ).toBe(false);
  });

  it("Invariant 1: confirmHumanDecision rejects unrendered companion edits", () => {
    const state = createInitialWorkspaceState();
    const openDecision = state.jobs[0].decisions[0];

    expect(() =>
      confirmHumanDecision(openDecision, {
        selectedOption: openDecision.options[0],
        actorId: "human:vp.comms",
        companionEditText: "Hidden unrendered model edit",
        renderedInUiBeforeClick: false
      })
    ).toThrow(/Invariant 1 Violation/);

    const confirmed = confirmHumanDecision(openDecision, {
      selectedOption: openDecision.options[0],
      actorId: "human:vp.comms",
      companionEditText: "Visibly rendered companion edit summary",
      renderedInUiBeforeClick: true
    });
    expect(confirmed.status).toBe("confirmed");
  });

  it("Zero Silent Mutation on Apply: applyWordingSpan preserves exact periods/punctuation and applyAcceptedChanges rolls back atomically on partial failure", () => {
    const sourceBody =
      "Aurenix 100mg reduced events by 34% (p=0.0012). Monitor transaminases every 12 weeks.";

    // Single exact span preserves periods
    const singleSpanRes = applyWordingSpan(sourceBody, {
      spanId: "sp-1",
      targetText: "every 12 weeks.",
      replacementText: "every 8 weeks during the first 6 months."
    });
    expect(singleSpanRes.ok).toBe(true);
    expect(singleSpanRes.updatedBody).toBe(
      "Aurenix 100mg reduced events by 34% (p=0.0012). Monitor transaminases every 8 weeks during the first 6 months."
    );

    // Multi-span comment where span 1 matches and span 2 fails -> atomic all-or-nothing rollback
    const multiPartComment: ReviewerCommentItem = {
      commentId: "cmt-multi-rollback",
      reviewerRole: "Regulatory",
      reviewerName: "M. Chen",
      originalCommentText: "Update both sentences atomically.",
      proposedSpans: [
        {
          spanId: "sp-valid-1",
          targetText: "Aurenix 100mg",
          replacementText: "AURENIX (100 mg)"
        },
        {
          spanId: "sp-missing-2",
          targetText: "Sentence that was reflowed and no longer exists.",
          replacementText: "Replacement."
        }
      ],
      status: "pending"
    };

    const rollbackRes = applyAcceptedChanges(sourceBody, multiPartComment);
    expect(rollbackRes.appliedAtomically).toBe(false);
    expect(rollbackRes.updatedBody).toBe(sourceBody);
    expect(rollbackRes.updatedComment.status).toBe("unplaced");

    // keepSource preserves sourceBody 100% untouched
    const kept = keepSource(sourceBody, multiPartComment);
    expect(kept.updatedBody).toBe(sourceBody);
    expect(kept.updatedComment.status).toBe("kept_source");
  });

  it("enforces 4-dimension Statement Reuse Library matching and Gemini 4 Argon Priority Inference routing", () => {
    const state = createInitialWorkspaceState();
    const validMatches = queryStatementReuseLibrary(state.reuseLibrary, {
      requestingTenantId: "tenant_aurenix",
      jurisdiction: "US",
      audience: "HCP & National Trade Media",
      channel: "Approval-Day Launch Pack",
      productVersion: "Aurenix-100mg"
    });
    expect(validMatches).toHaveLength(2);

    const crossTenantMatches = queryStatementReuseLibrary(state.reuseLibrary, {
      requestingTenantId: "tenant_other_company",
      jurisdiction: "US",
      audience: "HCP & National Trade Media",
      channel: "Approval-Day Launch Pack",
      productVersion: "Aurenix-100mg"
    });
    expect(crossTenantMatches).toHaveLength(0);

    expect(GEMINI_MODEL_ROUTING_MATRIX.approval_day_label_delta.modelId).toBe(
      "gemini-4-argon"
    );
    expect(GEMINI_MODEL_ROUTING_MATRIX.approval_day_label_delta.serviceTier).toBe(
      "priority"
    );
  });

  it("preserves U.S., vs., and Dr. abbreviations in splitMedicalSentences during Phase 2 -> Phase 3 boilerplate checks", () => {
    const boilerplate =
      "Hidradenitis suppurativa affects approximately 3.4 million U.S. adults and was evaluated by Dr. Vance vs. placebo. Phase 3 enrollment is underway.";
    const sentences = splitMedicalSentences(boilerplate);
    expect(sentences).toHaveLength(2);
    expect(sentences[0]).toBe(
      "Hidradenitis suppurativa affects approximately 3.4 million U.S. adults and was evaluated by Dr. Vance vs. placebo."
    );
    expect(sentences[1]).toBe("Phase 3 enrollment is underway.");
  });

  it("Phase 2 Full-Stack API client sends X-Tenant-Id and X-Actor-Id headers across all endpoints and falls back cleanly when offline", async () => {
    const state = createInitialWorkspaceState();
    const job = state.jobs[0];
    const actorId = "human:vp.comms@aurenix.com";
    const recordedCalls: Array<{
      url: string;
      method: string;
      headers: Record<string, string>;
    }> = [];

    const mockFetch: typeof fetch = async (input, init) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      const headers = (init?.headers ?? {}) as Record<string, string>;
      recordedCalls.push({ url, method, headers });

      return new Response(
        JSON.stringify({
          job_id: job.jobId,
          tenant_id: job.tenantId,
          job_state: "uploaded",
          original_filename: "uploaded_file.docx",
          parsed_docx: {
            docx_path: "01_working_draft_v1.docx",
            paragraphs: [job.asset.payload.body],
            table_cells: [],
            body_text: job.asset.payload.body,
            file_sha256: "sha256_docx_123"
          },
          reference_pdf: {
            page_count: 1,
            file_sha256: "sha256_pdf_123"
          },
          annotated_pdf: {
            annotated_pdf_path: `artifacts/file_roundtrip/${job.jobId}/03_annotated_reference.pdf`,
            annotation_count: 2,
            sha256: "sha256_annot_pdf_123",
            page_count: 1
          },
          receipt: {
            receipt_id: "VEEVA-SUB-0001",
            status: "succeeded",
            external_object_id: "VV-PM-2026-0091"
          }
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    };

    const opts = {
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl: mockFetch
    };

    const intakeRes = await apiCreateOrSyncJobIntake(job, actorId, opts);
    expect(intakeRes.mode).toBe("online");

    const getRes = await apiGetJob(job.jobId, job.tenantId, actorId, opts);
    expect(getRes.mode).toBe("online");

    const commentsRes = await apiReconcileComments(job, actorId, opts);
    expect(commentsRes.mode).toBe("online");

    const docxBlob = new Blob(["dummy docx"], {
      type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    });
    const uploadDocxRes = await apiUploadWorkingDocx(
      job,
      docxBlob,
      "draft.docx",
      actorId,
      opts
    );
    expect(uploadDocxRes.mode).toBe("online");
    expect(uploadDocxRes.fileSha256).toBe("sha256_docx_123");

    const pdfBlob = new Blob(["dummy pdf"], {
      type: "application/pdf"
    });
    const uploadPdfRes = await apiUploadReferencePdf(
      job,
      pdfBlob,
      "ref.pdf",
      actorId,
      { ...opts, sourceId: "SRC-USPI-2026-10", version: "3.0" }
    );
    expect(uploadPdfRes.mode).toBe("online");
    expect(uploadPdfRes.fileSha256).toBe("sha256_pdf_123");

    const roundtripRes = await apiExecuteFileRoundtrip(job, actorId, opts);
    expect(roundtripRes.mode).toBe("online");
    expect(Object.keys(roundtripRes.artifacts)).toHaveLength(6);

    const submitRes = await apiSubmitToPlatform(
      job,
      "veeva_promomats",
      actorId,
      "IDEMP-1",
      opts
    );
    expect(submitRes.mode).toBe("online");
    expect(submitRes.status).toBe("succeeded");
    expect(submitRes.receiptId).toBe("VEEVA-SUB-0001");

    expect(recordedCalls.length).toBe(7);
    for (const call of recordedCalls) {
      expect(call.headers["X-Tenant-Id"]).toBe(job.tenantId);
      expect(call.headers["X-Actor-Id"]).toBe(actorId);
    }

    // Verify graceful offline fallback when backend is unreachable
    const offlineFetch: typeof fetch = async () => {
      throw new Error("ECONNREFUSED");
    };
    const offlineRes = await apiExecuteFileRoundtrip(job, actorId, {
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl: offlineFetch
    });
    expect(offlineRes.mode).toBe("offline_fallback");
    expect(Object.keys(offlineRes.artifacts)).toHaveLength(6);
  });

  it("Phase 2 builds all 6 real artifact download URLs, derives PyMuPDF coordinate previews, and renders all 4 UI areas", () => {
    const state = createInitialWorkspaceState();
    const job = state.jobs[0];
    const actorId = "human:vp.comms@aurenix.com";

    expect(ROUNDTRIP_ARTIFACT_FILENAMES).toEqual([
      "01_working_draft_v1.docx",
      "02_reference_source.pdf",
      "03_annotated_reference.pdf",
      "04_revised_clean_v2.docx",
      "05_tracked_changes_redline_v1_to_v2.docx",
      "06_resubmission_package.zip"
    ]);

    const catalog = buildDefaultArtifactsCatalog(
      job.jobId,
      job.tenantId,
      actorId,
      "http://127.0.0.1:8000"
    );
    expect(Object.keys(catalog)).toHaveLength(6);
    for (const fname of ROUNDTRIP_ARTIFACT_FILENAMES) {
      const url = buildArtifactDownloadUrl({
        baseUrl: "http://127.0.0.1:8000",
        jobId: job.jobId,
        artifactName: fname,
        tenantId: job.tenantId,
        actorId
      });
      expect(url).toContain(`/jobs/${job.jobId}/artifacts/${fname}?tenant_id=${job.tenantId}`);
      expect(catalog[fname].download_url).toBe(url);
    }

    const previews = derivePyMuPdfPreviewsFromJob(job);
    expect(previews.annotations.length).toBeGreaterThanOrEqual(2);
    expect(previews.pages.length).toBeGreaterThanOrEqual(1);
    expect(previews.annotations[0].x1).toBeGreaterThan(previews.annotations[0].x0);
    expect(previews.annotations[0].y1).toBeGreaterThan(previews.annotations[0].y0);

    // Render full React UI and verify all 4 areas + Phase 2 controls are present
    const html = renderToStaticMarkup(React.createElement(App));
    expect(html).toContain("My work");
    expect(html).toContain("Materials and references");
    expect(html).toContain("Decisions needed");
    expect(html).toContain("Activity");
    expect(html).toContain("Sync Backend Intake");
    expect(html).toContain("X-Tenant-Id:");
    expect(html).toContain("X-Actor-Id:");
    expect(html).toContain("01_working_draft_v1.docx");
    expect(html).toContain("06_resubmission_package.zip");

    const materialsHtml = renderToStaticMarkup(
      React.createElement(App, { initialArea: "materials" })
    );
    expect(materialsHtml).toContain("PDF Page Selector");
    expect(materialsHtml).toContain("03_annotated_reference.pdf");
  });

  it("reconstructs pre-comment v1 initial_body on intake sync and sends pending placement_status for applied comments", () => {
    const state = createInitialWorkspaceState();
    const job = state.jobs[0];
    const actorId = "human:vp.comms@aurenix.com";
    const originalBody = job.asset.payload.body;
    const targetComment = job.reviewerComments[0];

    const applied = applyAcceptedChanges(originalBody, targetComment);
    expect(applied.appliedAtomically).toBe(true);
    expect(applied.updatedBody).not.toBe(originalBody);

    const updatedJob = {
      ...job,
      asset: {
        ...job.asset,
        payload: { body: applied.updatedBody }
      },
      reviewerComments: job.reviewerComments.map((c) =>
        c.commentId === targetComment.commentId ? applied.updatedComment : c
      )
    };

    const intakePayload = mapFrontendJobToBackendIntakePayload(updatedJob, actorId);
    expect(intakePayload.initial_body).toBe(originalBody);

    const commentsPayload = mapFrontendCommentsToBackendPayload(
      updatedJob.reviewerComments,
      updatedJob.asset.payload.body
    );
    const medComment = commentsPayload.find((c) => c.comment_id === targetComment.commentId);
    expect(medComment).toBeDefined();
    expect(medComment?.placement_status).toBe("pending");
  });

  it("Phase 3 imports Phase 2 R&D Handoff Pack ('About the Disease' & Epidemiology) online and offline and seeds HumanConfirmed reuse records", async () => {
    const state = createInitialWorkspaceState();
    const job = state.jobs[0];
    const actorId = "human:vp.comms@aurenix.com";

    const mockOnlineFetch: typeof fetch = async () =>
      new Response(
        JSON.stringify({
          job_id: job.jobId,
          rd_handoff: {
            imported_sources_count: 2,
            rd_handoff_boilerplate:
              "About the Disease & Epidemiology Boilerplate (EGFR+ NSCLC):\n- Disease Burden & Epidemiology: Chronic Moderate-to-Severe Inflammatory Disease affects approximately 2.4 million adults in the United States."
          },
          rd_handoff_boilerplate:
            "About the Disease & Epidemiology Boilerplate (EGFR+ NSCLC):\n- Disease Burden & Epidemiology: Chronic Moderate-to-Severe Inflammatory Disease affects approximately 2.4 million adults in the United States.",
          seeded_claims: [
            {
              claim_id: "clm_rd_custom_1",
              sentence_text: "Non-small cell lung cancer (NSCLC) accounts for 85% of lung cancer diagnoses.",
              plain_english_diagnosis: "Within Approved Meaning — Pre-anchored via Phase 2 R&D Handoff Pack.",
              anchors: [
                {
                  source_document_id: "SRC-PH2-IB-EPIDEMIOLOGY-2026",
                  page_number: 4,
                  x0: 54.0,
                  y0: 110.0,
                  x1: 558.0,
                  y1: 168.0,
                  verbatim_quote: "Non-small cell lung cancer (NSCLC) accounts for 85% of lung cancer diagnoses."
                }
              ]
            },
            {
              claim_id: "clm_rd_custom_2",
              sentence_text: "In Phase 2 AURORA-202 (N=184), Aurenix achieved 61.4% ORR.",
              plain_english_diagnosis: "Within Approved Meaning — Pre-anchored via Phase 2 R&D Handoff Pack.",
              anchors: [
                {
                  source_document_id: "SRC-PH2-CSR-AURORA202",
                  page_number: 12,
                  x0: 54.0,
                  y0: 210.0,
                  x1: 558.0,
                  y1: 274.0,
                  verbatim_quote: "In Phase 2 AURORA-202 (N=184), Aurenix achieved 61.4% ORR."
                }
              ]
            }
          ],
          seeded_reuse_records: [
            {
              statement_id: "stmt_rd_custom_1",
              tenant_id: job.tenantId,
              exact_wording: "Non-small cell lung cancer (NSCLC) accounts for 85% of lung cancer diagnoses.",
              source_document_id: "SRC-PH2-IB-EPIDEMIOLOGY-2026",
              jurisdiction: "US",
              audience: "HCP",
              channel: "Press Release",
              product_or_device_version: "Aurenix-v1.0",
              provenance: "HumanConfirmed",
              confirmed_by: actorId
            },
            {
              statement_id: "stmt_rd_custom_2",
              tenant_id: job.tenantId,
              exact_wording: "In Phase 2 AURORA-202 (N=184), Aurenix achieved 61.4% ORR.",
              source_document_id: "SRC-PH2-CSR-AURORA202",
              jurisdiction: "US",
              audience: "HCP",
              channel: "Press Release",
              product_or_device_version: "Aurenix-v1.0",
              provenance: "HumanConfirmed",
              confirmed_by: actorId
            }
          ]
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );

    const onlineRes = await apiImportRdHandoffPack(job, actorId, {
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl: mockOnlineFetch,
      diseaseName: "EGFR+ NSCLC",
      humanConfirmed: true
    });
    expect(onlineRes.mode).toBe("online");
    expect(onlineRes.rdHandoffBoilerplate).toContain("About the Disease & Epidemiology Boilerplate");
    expect(onlineRes.seededStatements).toHaveLength(2);
    expect(onlineRes.seededStatements[0].statementId).toBe("clm_rd_custom_1");
    expect(onlineRes.seededStatements[0].linkedAnchors[0].boundingBox.pageNumber).toBe(4);
    expect(onlineRes.seededReuseRecords).toHaveLength(2);
    expect(onlineRes.seededReuseRecords[0].reuseId).toBe("stmt_rd_custom_1");
    expect(onlineRes.seededReuseRecords[0].provenance).toBe("HumanConfirmed");
    expect(onlineRes.seededReuseRecords[0].confirmedBy).toBe(actorId);

    const offlineFetch: typeof fetch = async () => {
      throw new Error("ECONNREFUSED");
    };
    const offlineRes = await apiImportRdHandoffPack(job, actorId, {
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl: offlineFetch
    });
    expect(offlineRes.mode).toBe("offline_fallback");
    expect(offlineRes.rdHandoffBoilerplate).toContain("About the Disease & Epidemiology Boilerplate");
    expect(offlineRes.seededStatements[0].linkedAnchors[0].boundingBox.pageNumber).toBe(2);

    const html = renderToStaticMarkup(React.createElement(App));
    expect(html).toContain(
      "Import Phase 2 R&amp;D Handoff Pack (&#x27;About the Disease&#x27; &amp; Epidemiology)"
    );
  });
});





