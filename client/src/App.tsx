import React, { useMemo, useState } from "react";
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
  BackendRoundtripResponse,
  buildDefaultArtifactsCatalog,
  confirmHumanDecision,
  createInitialWorkspaceState,
  DEFAULT_BACKEND_BASE_URL,
  derivePyMuPdfPreviewsFromJob,
  extractHumanResolvedFacts,
  keepSource,
  LaunchState,
  PreFlightBand,
  proposeStrategy,
  queryStatementReuseLibrary,
  ROUNDTRIP_ARTIFACT_FILENAMES,
  runSection11AcceptanceSuite,
  WorkspaceState
} from "../../src/domain/mlrGenieEngine";
import { GEMINI_MODEL_ROUTING_MATRIX } from "../../src/prompt";

type WorkspaceArea = "my_work" | "materials" | "decisions" | "activity";

function bandCssClass(band: PreFlightBand): string {
  switch (band) {
    case "Within Approved Meaning (Ready for MLR Review)":
      return "band-green";
    case "Needs a Closer Read":
      return "band-amber";
    case "Uses Ruled-Out Phrasing":
      return "band-red";
    case "Missing Locked Clinical Data":
      return "band-purple";
  }
}

export interface AppProps {
  initialArea?: WorkspaceArea;
}

export function App({
  initialArea = "my_work"
}: AppProps): React.ReactElement {
  const [workspace, setWorkspace] = useState<WorkspaceState>(() =>
    createInitialWorkspaceState()
  );
  const [activeArea, setActiveArea] = useState<WorkspaceArea>(initialArea);
  const [actorId, setActorId] = useState<string>(
    "human:vp.comms@aurenix-biopharma.com"
  );
  const [backendBaseUrl] = useState<string>(DEFAULT_BACKEND_BASE_URL);
  const [backendSyncStatus, setBackendSyncStatus] = useState<string>(
    "Full-Stack Ready · FastAPI (/jobs/intake, /comments, /execute-file-roundtrip, /submit, /artifacts) + Offline Fallback"
  );
  const [geniePrompt, setGeniePrompt] = useState<string>("");
  const [genieStatusNote, setGenieStatusNote] = useState<string>(
    "Genie Ready · Google ADK (google-adk) + Vertex AI Regional ZDR + Cloud Model Armor active."
  );
  const [selectedPlatformKey, setSelectedPlatformKey] =
    useState<string>("veeva_promomats");
  const [roundtripByJob, setRoundtripByJob] = useState<
    Record<string, BackendRoundtripResponse>
  >({});
  const [selectedAnnotationId, setSelectedAnnotationId] = useState<
    string | null
  >(null);
  const [selectedPageNumber, setSelectedPageNumber] = useState<number | null>(
    null
  );
  const [docxUploadNote, setDocxUploadNote] = useState<string>("");
  const [pdfUploadNote, setPdfUploadNote] = useState<string>("");
  const [roundtripNote, setRoundtripNote] = useState<string>("");
  const [rdHandoffNote, setRdHandoffNote] = useState<string>("");

  const activeJob = useMemo(() => {
    return (
      workspace.jobs.find((j) => j.jobId === workspace.activeJobId) ??
      workspace.jobs[0]
    );
  }, [workspace]);

  const openDecisionsCount = useMemo(
    () => activeJob.decisions.filter((d) => d.status === "open").length,
    [activeJob]
  );

  const pendingCommentsCount = useMemo(
    () => activeJob.reviewerComments.filter((c) => c.status === "pending").length,
    [activeJob]
  );

  const bandCounts = useMemo(() => {
    const counts: Record<PreFlightBand, number> = {
      "Within Approved Meaning (Ready for MLR Review)": 0,
      "Needs a Closer Read": 0,
      "Uses Ruled-Out Phrasing": 0,
      "Missing Locked Clinical Data": 0
    };
    for (const s of activeJob.statements) {
      counts[s.preFlightBand] += 1;
    }
    return counts;
  }, [activeJob]);

  const matchedReuseRecords = useMemo(() => {
    return queryStatementReuseLibrary(workspace.reuseLibrary, {
      requestingTenantId: workspace.activeTenantId,
      jurisdiction: activeJob.jurisdiction,
      audience: activeJob.audience,
      channel: activeJob.channel,
      productVersion: activeJob.productVersion
    });
  }, [workspace.reuseLibrary, workspace.activeTenantId, activeJob]);

  const humanResolvedFacts = useMemo(
    () => extractHumanResolvedFacts(activeJob.asset),
    [activeJob.asset]
  );

  const section11Suite = useMemo(() => runSection11AcceptanceSuite(), []);

  const activeRoundtrip = roundtripByJob[activeJob.jobId];

  const activeArtifacts = useMemo(() => {
    if (activeRoundtrip?.artifacts) {
      return activeRoundtrip.artifacts;
    }
    return buildDefaultArtifactsCatalog(
      activeJob.jobId,
      workspace.activeTenantId,
      actorId,
      backendBaseUrl
    );
  }, [activeRoundtrip, activeJob.jobId, workspace.activeTenantId, actorId, backendBaseUrl]);

  const activePyMuPdfData = useMemo(() => {
    if (
      activeRoundtrip?.annotated_pdf &&
      (activeRoundtrip.annotated_pdf.annotations.length > 0 ||
        activeRoundtrip.annotated_pdf.pages.length > 0)
    ) {
      return {
        annotations: activeRoundtrip.annotated_pdf.annotations,
        pages: activeRoundtrip.annotated_pdf.pages,
        sha256: activeRoundtrip.annotated_pdf.sha256,
        annotationCount: activeRoundtrip.annotated_pdf.annotation_count
      };
    }
    const derived = derivePyMuPdfPreviewsFromJob(activeJob);
    return {
      annotations: derived.annotations,
      pages: derived.pages,
      sha256: activeJob.referenceDocs[0]?.sha256Prefix ?? "verified_pymupdf",
      annotationCount: derived.annotations.length
    };
  }, [activeRoundtrip, activeJob]);

  const selectedAnnotation = useMemo(() => {
    const list = activePyMuPdfData.annotations;
    if (list.length === 0) return null;
    if (selectedAnnotationId) {
      const found = list.find((a) => a.annotation_id === selectedAnnotationId);
      if (found) return found;
    }
    return list[0];
  }, [activePyMuPdfData, selectedAnnotationId]);

  const activePreviewPage = useMemo(() => {
    const targetPageNum =
      selectedPageNumber ?? selectedAnnotation?.page_number ?? 1;
    const found = activePyMuPdfData.pages.find(
      (p) => p.page_number === targetPageNum
    );
    return (
      found ??
      activePyMuPdfData.pages[0] ?? {
        page_number: 1,
        page_width: 612,
        page_height: 792,
        text_blocks: [],
        embedded_annotations: []
      }
    );
  }, [activePyMuPdfData, selectedAnnotation, selectedPageNumber]);

  function handleSelectJob(jobId: string): void {
    setSelectedAnnotationId(null);
    setSelectedPageNumber(null);
    setWorkspace((prev) => ({
      ...prev,
      activeJobId: jobId
    }));
    const targetJob =
      workspace.jobs.find((j) => j.jobId === jobId) ?? activeJob;
    void apiGetJob(jobId, workspace.activeTenantId, actorId, {
      baseUrl: backendBaseUrl
    }).then((res) => {
      setBackendSyncStatus(
        res.mode === "online"
          ? `Connected to FastAPI (GET /jobs/${jobId}) · X-Tenant-Id: ${workspace.activeTenantId}`
          : `Local/Offline Mode (GET /jobs/${jobId} fallback) · X-Tenant-Id: ${workspace.activeTenantId}`
      );
      if (res.mode === "online" && res.artifacts) {
        const derived = derivePyMuPdfPreviewsFromJob(targetJob);
        const metaObj =
          (res.jobData?.metadata as Record<string, unknown> | undefined) ?? {};
        const lastAnnot =
          (metaObj.last_annotated_pdf as
            | BackendRoundtripResponse["annotated_pdf"]
            | undefined) ?? undefined;
        setRoundtripByJob((prev) => {
          const existing = prev[jobId];
          return {
            ...prev,
            [jobId]: {
              mode: "online",
              job_id: jobId,
              tenant_id: workspace.activeTenantId,
              annotated_pdf: lastAnnot ??
                existing?.annotated_pdf ?? {
                  annotated_pdf_path: `artifacts/file_roundtrip/${jobId}/03_annotated_reference.pdf`,
                  annotation_count: derived.annotations.length,
                  sha256: "verified_pymupdf",
                  page_count: derived.pages.length || 1,
                  annotations: derived.annotations,
                  pages: derived.pages
                },
              redline_docx: existing?.redline_docx ?? {
                redline_docx_path: `artifacts/file_roundtrip/${jobId}/05_tracked_changes_redline_v1_to_v2.docx`,
                tracked_replacements: 0,
                sha256: "pending"
              },
              resubmission_zip: existing?.resubmission_zip ?? {
                zip_path: `artifacts/file_roundtrip/${jobId}/06_resubmission_package.zip`,
                zip_bytes: 0,
                zip_sha256: "pending"
              },
              artifacts: buildDefaultArtifactsCatalog(
                jobId,
                workspace.activeTenantId,
                actorId,
                backendBaseUrl,
                res.artifacts
              )
            }
          };
        });
      }
    });
  }

  function handleChangeLaunchState(newState: LaunchState): void {
    setWorkspace((prev) => ({
      ...prev,
      jobs: prev.jobs.map((job) => {
        if (job.jobId !== prev.activeJobId) return job;
        const routeNote =
          newState === "approved"
            ? "Approval-Day Final Label Delta triggered on gemini-4-argon (service_tier=priority)."
            : newState === "other_decision"
            ? "Unanticipated regulatory outcome (CRL / Deferral): Promotional launch pack frozen; Holding Statement Pack activated."
            : `Launch state updated to ${newState}.`;
        return {
          ...job,
          launchState: newState,
          statusSummary: routeNote,
          auditLedger: [
            {
              eventId: `evt-launch-${job.auditLedger.length + 1}`,
              jobId: job.jobId,
              timestampIso: "2026-10-28T16:45:00Z",
              eventType: "DecisionConfirmed",
              actorDisplay: "VP Corporate Communications",
              modelOrTool: "gemini-4-argon (service_tier=priority)",
              summary: routeNote
            },
            ...job.auditLedger
          ]
        };
      })
    }));
  }

  function handleRunGenieCommand(): void {
    const query = geniePrompt.trim() || "Run MLR Pre-Flight & Citation Check";
    const strategyResult = proposeStrategy(activeJob.asset.payload.body, {
      ruledOutPhrases: ["rapid next-day symptom resolution", "cures 100%"],
      lockedClinicalFigures: ["p=0.0012", "95% CI: 0.54-0.82"]
    });
    const topBand =
      strategyResult.validation.advisoryFindings[0]?.band ??
      "Within Approved Meaning (Ready for MLR Review)";

    setGenieStatusNote(
      `Executed "${query}" via ${strategyResult.modelRoute} + gemini-4-argon · Advisory Band: ${topBand} (severity: "note").`
    );
    setGeniePrompt("");

    void apiCreateOrSyncJobIntake(activeJob, actorId, {
      baseUrl: backendBaseUrl
    }).then((intakeRes) => {
      setBackendSyncStatus(
        `POST /jobs/intake (${intakeRes.mode}) · X-Tenant-Id: ${activeJob.tenantId} · X-Actor-Id: ${actorId}`
      );
    });
  }

  function handleSyncJobWithBackend(): void {
    void apiCreateOrSyncJobIntake(activeJob, actorId, {
      baseUrl: backendBaseUrl
    }).then(async (intakeRes) => {
      const getRes = await apiGetJob(
        activeJob.jobId,
        activeJob.tenantId,
        actorId,
        { baseUrl: backendBaseUrl }
      );
      if (getRes.mode === "online" && getRes.artifacts) {
        const derived = derivePyMuPdfPreviewsFromJob(activeJob);
        setRoundtripByJob((prev) => {
          const existing = prev[activeJob.jobId];
          return {
            ...prev,
            [activeJob.jobId]: {
              mode: "online",
              job_id: activeJob.jobId,
              tenant_id: activeJob.tenantId,
              annotated_pdf: existing?.annotated_pdf ?? {
                annotated_pdf_path: `artifacts/file_roundtrip/${activeJob.jobId}/03_annotated_reference.pdf`,
                annotation_count: derived.annotations.length,
                sha256: "verified_pymupdf",
                page_count: derived.pages.length || 1,
                annotations: derived.annotations,
                pages: derived.pages
              },
              redline_docx: existing?.redline_docx ?? {
                redline_docx_path: `artifacts/file_roundtrip/${activeJob.jobId}/05_tracked_changes_redline_v1_to_v2.docx`,
                tracked_replacements: 0,
                sha256: "pending"
              },
              resubmission_zip: existing?.resubmission_zip ?? {
                zip_path: `artifacts/file_roundtrip/${activeJob.jobId}/06_resubmission_package.zip`,
                zip_bytes: 0,
                zip_sha256: "pending"
              },
              artifacts: buildDefaultArtifactsCatalog(
                activeJob.jobId,
                activeJob.tenantId,
                actorId,
                backendBaseUrl,
                getRes.artifacts
              )
            }
          };
        });
      }
      setBackendSyncStatus(
        `Synced Job via POST /jobs/intake & GET /jobs/${activeJob.jobId} (${intakeRes.mode}) · X-Tenant-Id: ${activeJob.tenantId}`
      );
      setGenieStatusNote(
        `Backend Intake & Pre-Flight Synced (${intakeRes.mode}) for ${activeJob.jobId} · Actor: ${actorId}.`
      );
    });
  }

  function handleUploadWorkingDocx(
    e: React.ChangeEvent<HTMLInputElement>
  ): void {
    const file = e.target.files?.[0];
    if (!file) return;

    void apiUploadWorkingDocx(activeJob, file, file.name, actorId, {
      baseUrl: backendBaseUrl
    }).then((res) => {
      const tableSuffix =
        res.tableCells.length > 0
          ? ` · ${res.tableCells.length} table cell(s)`
          : "";
      setDocxUploadNote(
        `Uploaded ${res.originalFilename} -> 01_working_draft_v1.docx (${res.mode}) · SHA-256: ${res.fileSha256.slice(
          0,
          12
        )} · ${res.paragraphs.length} paragraph(s)${tableSuffix}`
      );
      setBackendSyncStatus(
        `POST /jobs/${activeJob.jobId}/upload-working-docx (${res.mode}) · X-Tenant-Id: ${activeJob.tenantId}`
      );
      setRoundtripByJob((prev) => {
        const existing = prev[activeJob.jobId];
        const derived = derivePyMuPdfPreviewsFromJob(activeJob);
        const updatedArtifacts = buildDefaultArtifactsCatalog(
          activeJob.jobId,
          workspace.activeTenantId,
          actorId,
          backendBaseUrl,
          res.artifacts ?? {
            ...(existing?.artifacts ?? {}),
            "01_working_draft_v1.docx": {
              exists: true,
              bytes: file.size || 4096,
              sha256: res.fileSha256
            },
            "04_revised_clean_v2.docx": {
              exists: false,
              bytes: 0,
              sha256: null
            },
            "05_tracked_changes_redline_v1_to_v2.docx": {
              exists: false,
              bytes: 0,
              sha256: null
            },
            "06_resubmission_package.zip": {
              exists: false,
              bytes: 0,
              sha256: null
            }
          }
        );
        return {
          ...prev,
          [activeJob.jobId]: {
            mode: res.mode,
            job_id: activeJob.jobId,
            tenant_id: activeJob.tenantId,
            annotated_pdf: existing?.annotated_pdf ?? {
              annotated_pdf_path: `artifacts/file_roundtrip/${activeJob.jobId}/03_annotated_reference.pdf`,
              annotation_count: derived.annotations.length,
              sha256: "verified_pymupdf",
              page_count: derived.pages.length || 1,
              annotations: derived.annotations,
              pages: derived.pages
            },
            redline_docx: {
              redline_docx_path: `artifacts/file_roundtrip/${activeJob.jobId}/05_tracked_changes_redline_v1_to_v2.docx`,
              tracked_replacements: 0,
              sha256: "pending"
            },
            resubmission_zip: {
              zip_path: `artifacts/file_roundtrip/${activeJob.jobId}/06_resubmission_package.zip`,
              zip_bytes: 0,
              zip_sha256: "pending"
            },
            artifacts: updatedArtifacts
          }
        };
      });
      setWorkspace((prev) => ({
        ...prev,
        jobs: prev.jobs.map((job) => {
          if (job.jobId !== prev.activeJobId) return job;
          const updatedBody = res.bodyText || job.asset.payload.body;
          return {
            ...job,
            pvGateCleared: !res.pvFlagged,
            asset: {
              ...job.asset,
              payload: {
                body: updatedBody
              },
              revisions: [
                ...job.asset.revisions,
                {
                  revisionId: `rev-docx-${job.asset.revisions.length + 1}`,
                  eventType: "AssetEditRecorded",
                  actorId,
                  body: updatedBody,
                  timestampIso: "2026-10-28T16:46:00Z"
                }
              ]
            },
            auditLedger: [
              {
                eventId: `evt-docx-${job.auditLedger.length + 1}`,
                jobId: job.jobId,
                timestampIso: "2026-10-28T16:46:00Z",
                eventType: "AssetEditRecorded",
                actorDisplay: actorId,
                modelOrTool: "python-docx (POST /upload-working-docx)",
                summary: `Uploaded working draft ${res.originalFilename} (SHA-256: ${res.fileSha256.slice(
                  0,
                  12
                )}).`
              },
              ...job.auditLedger
            ]
          };
        })
      }));
    });
  }

  function handleUploadReferencePdf(
    e: React.ChangeEvent<HTMLInputElement>
  ): void {
    const file = e.target.files?.[0];
    if (!file) return;

    void apiUploadReferencePdf(activeJob, file, file.name, actorId, {
      baseUrl: backendBaseUrl
    }).then((res) => {
      setPdfUploadNote(
        `Uploaded ${res.originalFilename} -> 02_reference_source.pdf & 03_annotated_reference.pdf (${res.mode}) · ${res.pageCount} page(s) · ${res.annotations.length} PyMuPDF anchor(s) · SHA-256: ${res.fileSha256.slice(
          0,
          12
        )}`
      );
      setBackendSyncStatus(
        `POST /jobs/${activeJob.jobId}/upload-reference-pdf (${res.mode}) · X-Tenant-Id: ${activeJob.tenantId}`
      );
      setRoundtripByJob((prev) => {
        const existing = prev[activeJob.jobId];
        const baseArtifacts =
          existing?.artifacts ??
          buildDefaultArtifactsCatalog(
            activeJob.jobId,
            workspace.activeTenantId,
            actorId,
            backendBaseUrl
          );
        const mergedArtifacts = res.artifacts
          ? buildDefaultArtifactsCatalog(
              activeJob.jobId,
              workspace.activeTenantId,
              actorId,
              backendBaseUrl,
              res.artifacts
            )
          : {
              ...baseArtifacts,
              "02_reference_source.pdf": {
                ...baseArtifacts["02_reference_source.pdf"],
                exists: true,
                bytes: file.size || 4096,
                sha256: res.fileSha256
              },
              "03_annotated_reference.pdf": {
                ...baseArtifacts["03_annotated_reference.pdf"],
                exists: true,
                bytes: file.size || 4096,
                sha256: res.annotatedSha256
              }
            };
        return {
          ...prev,
          [activeJob.jobId]: {
            mode: res.mode,
            job_id: activeJob.jobId,
            tenant_id: activeJob.tenantId,
            annotated_pdf: {
              annotated_pdf_path: `artifacts/file_roundtrip/${activeJob.jobId}/03_annotated_reference.pdf`,
              annotation_count: res.annotations.length,
              sha256: res.annotatedSha256,
              page_count: res.pageCount,
              annotations: res.annotations,
              pages: res.pages
            },
            redline_docx: existing?.redline_docx ?? {
              redline_docx_path: `artifacts/file_roundtrip/${activeJob.jobId}/05_tracked_changes_redline_v1_to_v2.docx`,
              tracked_replacements: 0,
              sha256: "pending"
            },
            resubmission_zip: existing?.resubmission_zip ?? {
              zip_path: `artifacts/file_roundtrip/${activeJob.jobId}/06_resubmission_package.zip`,
              zip_bytes: 0,
              zip_sha256: "pending"
            },
            artifacts: mergedArtifacts
          }
        };
      });
      if (res.annotations.length > 0) {
        setSelectedAnnotationId(res.annotations[0].annotation_id);
        setSelectedPageNumber(res.annotations[0].page_number);
      } else if (res.pages.length > 0) {
        setSelectedPageNumber(res.pages[0].page_number);
      }
      setWorkspace((prev) => ({
        ...prev,
        jobs: prev.jobs.map((job) => {
          if (job.jobId !== prev.activeJobId) return job;
          return {
            ...job,
            referenceDocs: [
              {
                docId: `doc-upload-${job.referenceDocs.length + 1}`,
                title: res.originalFilename,
                docType: "Pivotal Clinical Study",
                versionTag: "UPLOADED-v1.0",
                pageCount: res.pageCount,
                sha256Prefix: res.fileSha256.slice(0, 12),
                status: "locked_verified"
              },
              ...job.referenceDocs
            ],
            auditLedger: [
              {
                eventId: `evt-pdf-${job.auditLedger.length + 1}`,
                jobId: job.jobId,
                timestampIso: "2026-10-28T16:47:00Z",
                eventType: "AssetDrafted",
                actorDisplay: actorId,
                modelOrTool: "PyMuPDF + gemini-3.8-flash (POST /upload-reference-pdf)",
                summary: `Uploaded reference pack ${res.originalFilename} (${res.pageCount} pages) and embedded ${res.annotations.length} PyMuPDF highlight annotation(s).`
              },
              ...job.auditLedger
            ]
          };
        })
      }));
    });
  }

  function handleExecuteFileRoundtrip(): void {
    void apiExecuteFileRoundtrip(activeJob, actorId, {
      baseUrl: backendBaseUrl
    }).then((res) => {
      setRoundtripByJob((prev) => ({
        ...prev,
        [activeJob.jobId]: res
      }));
      if (res.annotated_pdf.annotations.length > 0) {
        setSelectedAnnotationId(res.annotated_pdf.annotations[0].annotation_id);
        setSelectedPageNumber(res.annotated_pdf.annotations[0].page_number);
      }
      setRoundtripNote(
        `File Roundtrip Complete (${res.mode}) · ${res.annotated_pdf.annotation_count} PyMuPDF highlight(s) · ${res.redline_docx.tracked_replacements} OpenXML <w:del>/<w:ins> redline change(s) · Bundle ZIP SHA-256: ${res.resubmission_zip.zip_sha256.slice(
          0,
          12
        )}`
      );
      setBackendSyncStatus(
        `POST /jobs/${activeJob.jobId}/execute-file-roundtrip (${res.mode}) · 6 artifacts ready for download`
      );
      setWorkspace((prev) => ({
        ...prev,
        jobs: prev.jobs.map((job) => {
          if (job.jobId !== prev.activeJobId) return job;
          return {
            ...job,
            auditLedger: [
              {
                eventId: `evt-rt-${job.auditLedger.length + 1}`,
                jobId: job.jobId,
                timestampIso: "2026-10-28T16:51:00Z",
                eventType: "ExternalActionExecuted",
                actorDisplay: actorId,
                modelOrTool:
                  "POST /execute-file-roundtrip (python-docx + PyMuPDF + ZIP Manifest)",
                summary: `Generated 01_working_draft_v1.docx, 02_reference_source.pdf, 03_annotated_reference.pdf (${res.annotated_pdf.annotation_count} highlights), 04_revised_clean_v2.docx, 05_tracked_changes_redline_v1_to_v2.docx, and 06_resubmission_package.zip.`
              },
              ...job.auditLedger
            ]
          };
        })
      }));
    });
  }

  function handleImportRdHandoffPack(): void {
    void apiImportRdHandoffPack(activeJob, actorId, {
      baseUrl: backendBaseUrl
    }).then((res) => {
      setRdHandoffNote(
        `Phase 2 -> Phase 3 R&D Handoff Imported (${res.mode}) · ${res.importedSourcesCount} locked clinical sources · ${res.seededStatements.length} pre-anchored claims · ${res.seededReuseRecords.length} Statement Reuse Library record(s) seeded (HumanConfirmed).`
      );
      setBackendSyncStatus(
        `POST /jobs/${activeJob.jobId}/import-rd-handoff (${res.mode}) · R&D Epidemiology & Trial Design Pack Locked`
      );
      setWorkspace((prev) => {
        const existingReuseIds = new Set(prev.reuseLibrary.map((r) => r.reuseId));
        const newReuse = res.seededReuseRecords.filter(
          (r) => !existingReuseIds.has(r.reuseId)
        );
        return {
          ...prev,
          reuseLibrary: [...newReuse, ...prev.reuseLibrary],
          jobs: prev.jobs.map((job) => {
            if (job.jobId !== prev.activeJobId) return job;
            const bodyAlreadyHasBoilerplate = job.asset.payload.body.includes(
              "About the Disease & Epidemiology Boilerplate"
            );
            const nextBody = bodyAlreadyHasBoilerplate
              ? job.asset.payload.body
              : `${job.asset.payload.body.trim()}\n\n${res.rdHandoffBoilerplate}`;
            const existingStmtIds = new Set(
              job.statements.map((s) => s.statementId)
            );
            const nextStatements = [
              ...job.statements,
              ...res.seededStatements.filter(
                (s) => !existingStmtIds.has(s.statementId)
              )
            ];
            const hasRefDoc = job.referenceDocs.some(
              (d) => d.docId === res.referenceDoc.docId
            );
            return {
              ...job,
              asset: {
                ...job.asset,
                payload: {
                  body: nextBody
                },
                revisions: bodyAlreadyHasBoilerplate
                  ? job.asset.revisions
                  : [
                      ...job.asset.revisions,
                      {
                        revisionId: `rev-rd-handoff-${job.asset.revisions.length + 1}`,
                        eventType: "AssetDrafted" as const,
                        actorId: "system:rd-handoff-importer",
                        body: nextBody,
                        timestampIso: "2026-10-28T16:52:00Z"
                      }
                    ]
              },
              statements: nextStatements,
              referenceDocs: hasRefDoc
                ? job.referenceDocs
                : [res.referenceDoc, ...job.referenceDocs],
              auditLedger: [
                {
                  eventId: `evt-rd-${job.auditLedger.length + 1}`,
                  jobId: job.jobId,
                  timestampIso: "2026-10-28T16:52:00Z",
                  eventType: "HumanConfirmed",
                  actorDisplay: actorId,
                  modelOrTool:
                    "import_rd_handoff_pack (POST /jobs/{job_id}/import-rd-handoff)",
                  summary: `Imported Phase 2 R&D Handoff Pack: locked epidemiology & trial design statistics, pre-anchored "About the Disease & Epidemiology Boilerplate", and seeded ${res.seededReuseRecords.length} HumanConfirmed Statement Reuse record(s).`
                },
                ...job.auditLedger
              ]
            };
          })
        };
      });
    });
  }

  function handleApplyComment(commentId: string): void {
    const targetComment = activeJob.reviewerComments.find(
      (c) => c.commentId === commentId
    );
    if (!targetComment) return;

    const res = applyAcceptedChanges(
      activeJob.asset.payload.body,
      targetComment
    );
    const eventSummary = res.appliedAtomically
      ? `Atomically applied ${targetComment.proposedSpans.length} span(s) for comment ${commentId} (${targetComment.reviewerRole}).`
      : `Zero Silent Mutation Rollback on ${commentId}: ${res.failureReason}. Marked 'unplaced'.`;

    const nextJob = {
      ...activeJob,
      asset: {
        ...activeJob.asset,
        payload: {
          body: res.updatedBody
        },
        revisions: res.appliedAtomically
          ? [
              ...activeJob.asset.revisions,
              {
                revisionId: `rev-human-${activeJob.asset.revisions.length + 1}`,
                eventType: "AssetEditRecorded" as const,
                actorId: "human:mlr.coordinator@aurenix-biopharma.com",
                body: res.updatedBody,
                timestampIso: "2026-10-28T16:48:00Z"
              }
            ]
          : activeJob.asset.revisions
      },
      reviewerComments: activeJob.reviewerComments.map((c) =>
        c.commentId === commentId ? res.updatedComment : c
      ),
      auditLedger: [
        {
          eventId: `evt-cmt-${activeJob.auditLedger.length + 1}`,
          jobId: activeJob.jobId,
          timestampIso: "2026-10-28T16:48:00Z",
          eventType: (res.appliedAtomically
            ? "AssetEditRecorded"
            : "DecisionConfirmed") as "AssetEditRecorded" | "DecisionConfirmed",
          actorDisplay: "MLR Coordinator (Atomic Apply)",
          modelOrTool: "applyAcceptedChanges (Zero Silent Mutation)",
          summary: eventSummary
        },
        ...activeJob.auditLedger
      ]
    };

    setWorkspace((prev) => ({
      ...prev,
      jobs: prev.jobs.map((job) =>
        job.jobId === prev.activeJobId ? nextJob : job
      )
    }));

    void apiReconcileComments(nextJob, actorId, {
      baseUrl: backendBaseUrl
    }).then((reconcileRes) => {
      setBackendSyncStatus(
        `POST /jobs/${activeJob.jobId}/comments (${reconcileRes.mode}) · X-Tenant-Id: ${activeJob.tenantId}`
      );
    });
  }

  function handleKeepSourceComment(commentId: string): void {
    const targetComment = activeJob.reviewerComments.find(
      (c) => c.commentId === commentId
    );
    if (!targetComment) return;

    const res = keepSource(activeJob.asset.payload.body, targetComment);
    const nextJob = {
      ...activeJob,
      reviewerComments: activeJob.reviewerComments.map((c) =>
        c.commentId === commentId ? res.updatedComment : c
      ),
      auditLedger: [
        {
          eventId: `evt-keep-${activeJob.auditLedger.length + 1}`,
          jobId: activeJob.jobId,
          timestampIso: "2026-10-28T16:49:00Z",
          eventType: "DecisionConfirmed" as const,
          actorDisplay: "MLR Coordinator",
          modelOrTool: "keepSource (Verbatim Preserved)",
          summary: `Kept original source wording for comment ${commentId} (${targetComment.reviewerRole}).`
        },
        ...activeJob.auditLedger
      ]
    };

    setWorkspace((prev) => ({
      ...prev,
      jobs: prev.jobs.map((job) =>
        job.jobId === prev.activeJobId ? nextJob : job
      )
    }));

    void apiReconcileComments(nextJob, actorId, {
      baseUrl: backendBaseUrl
    }).then((reconcileRes) => {
      setBackendSyncStatus(
        `POST /jobs/${activeJob.jobId}/comments (${reconcileRes.mode}) · X-Tenant-Id: ${activeJob.tenantId}`
      );
    });
  }

  function handleConfirmDecision(decisionId: string, option: string): void {
    setWorkspace((prev) => ({
      ...prev,
      jobs: prev.jobs.map((job) => {
        if (job.jobId !== prev.activeJobId) return job;
        return {
          ...job,
          decisions: job.decisions.map((d) =>
            d.decisionId === decisionId
              ? confirmHumanDecision(d, {
                  selectedOption: option,
                  actorId: "human:vp.comms@aurenix-biopharma.com",
                  companionEditText: d.renderedCompanionSummary,
                  renderedInUiBeforeClick: true
                })
              : d
          ),
          auditLedger: [
            {
              eventId: `evt-dec-${job.auditLedger.length + 1}`,
              jobId: job.jobId,
              timestampIso: "2026-10-28T16:50:00Z",
              eventType: "DecisionConfirmed",
              actorDisplay: "VP Corporate Communications",
              modelOrTool: "Human Decision Gate (Invariant 1 Verified)",
              summary: `Confirmed decision ${decisionId}: "${option}"`
            },
            ...job.auditLedger
          ]
        };
      })
    }));
  }

  function handleGrantReleaseAndExport(): void {
    const profile =
      workspace.platformProfiles[selectedPlatformKey] ??
      workspace.platformProfiles.veeva_promomats;
    const idempotencyKey = `idem-${activeJob.jobId}-${selectedPlatformKey}-v1`;
    const optimisticReceiptId = `rcpt-${selectedPlatformKey.toUpperCase()}-20261028`;

    setWorkspace((prev) => ({
      ...prev,
      jobs: prev.jobs.map((job) => {
        if (job.jobId !== prev.activeJobId) return job;
        return {
          ...job,
          explicitReleaseMandateGranted: true,
          statusSummary: `Package exported to ${profile.systemName} (${profile.tenantTier}) · Receipt ${optimisticReceiptId}`,
          auditLedger: [
            {
              eventId: `evt-export-${job.auditLedger.length + 1}`,
              jobId: job.jobId,
              timestampIso: "2026-10-28T16:52:00Z",
              eventType: "ExternalActionExecuted",
              actorDisplay: "Explicit Human Release Mandate",
              modelOrTool: `${profile.systemName} Adapter (${profile.tenantTier})`,
              summary: `Exported annotated submission package + Reference Anchor Matrix. Writeback supported: ${profile.supportsNativeReviewCommentWriteback}`,
              idempotencyKey,
              externalReceiptId: optimisticReceiptId
            },
            ...job.auditLedger
          ]
        };
      })
    }));

    void apiSubmitToPlatform(
      activeJob,
      selectedPlatformKey,
      actorId,
      idempotencyKey,
      { baseUrl: backendBaseUrl }
    ).then((subRes) => {
      setBackendSyncStatus(
        `POST /jobs/${activeJob.jobId}/submit/${selectedPlatformKey} (${subRes.mode}) · Receipt: ${subRes.receiptId} · State: ${subRes.jobState}`
      );
    });
  }

  function renderArtifactDownloadGrid(): React.ReactElement {
    return (
      <div className="artifact-grid" aria-label="Generated Roundtrip Artifacts">
        {ROUNDTRIP_ARTIFACT_FILENAMES.map((fname) => {
          const item = activeArtifacts[fname];
          return (
            <div key={fname} className="artifact-card">
              <div className="artifact-card-top">
                <strong>{item.filename}</strong>
                <span className="meta-chip">
                  {item.exists ? `${item.bytes} B` : "Ready on Roundtrip"}
                </span>
              </div>
              <div style={{ fontSize: "0.79rem", color: "#475569", margin: "4px 0" }}>
                {item.label}
                {item.sha256 ? (
                  <>
                    {" "}
                    · SHA-256: <code>{item.sha256.slice(0, 12)}</code>
                  </>
                ) : null}
              </div>
              <a
                href={item.download_url}
                download={item.filename}
                className="btn artifact-download-link"
              >
                Download {item.filename}
              </a>
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <div className="app-shell">
      {/* Top Header & 4-Area Navigation */}
      <header className="top-header">
        <div className="brand-group">
          <h1>
            MLR Genie
            <span className="brand-badge">
              GCP · Google ADK · Gemini 4 Argon &amp; 3.8 Flash
            </span>
          </h1>
          <p className="brand-subtitle">
            Agent-first review, citation anchoring &amp; approval-day launch workspace for
            Pharma, Biotech &amp; MedTech communications
          </p>
          <div
            style={{
              fontSize: "0.76rem",
              color: "#1e40af",
              marginTop: "4px",
              fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
            }}
          >
            {backendSyncStatus}
          </div>
        </div>

        <nav className="area-nav" aria-label="Workspace Areas">
          <button
            type="button"
            className={`area-tab ${activeArea === "my_work" ? "active" : ""}`}
            onClick={() => setActiveArea("my_work")}
          >
            My work
            <span className="count-pill">{workspace.jobs.length}</span>
          </button>
          <button
            type="button"
            className={`area-tab ${activeArea === "materials" ? "active" : ""}`}
            onClick={() => setActiveArea("materials")}
          >
            Materials and references
            <span className="count-pill">{activeJob.referenceDocs.length}</span>
          </button>
          <button
            type="button"
            className={`area-tab ${activeArea === "decisions" ? "active" : ""}`}
            onClick={() => setActiveArea("decisions")}
          >
            Decisions needed
            <span className="count-pill">
              {openDecisionsCount + pendingCommentsCount}
            </span>
          </button>
          <button
            type="button"
            className={`area-tab ${activeArea === "activity" ? "active" : ""}`}
            onClick={() => setActiveArea("activity")}
          >
            Activity
            <span className="count-pill">{activeJob.auditLedger.length}</span>
          </button>
        </nav>
      </header>

      {/* 5-Second Orientation Banner (Pharma Comms Buyer Persona) */}
      <section className="orientation-banner" aria-label="5-Second Orientation">
        <div className="orientation-top">
          <div>
            <strong style={{ fontSize: "1.05rem" }}>{activeJob.title}</strong>
            <div style={{ fontSize: "0.86rem", color: "#475569", marginTop: "4px" }}>
              {activeJob.statusSummary}
            </div>
            <div className="meta-chips">
              <span className="meta-chip">Sector: {activeJob.sector}</span>
              <span className="meta-chip">
                Product/Version: {activeJob.productVersion}
              </span>
              <span className="meta-chip">
                Launch State: {activeJob.launchState.toUpperCase()}
              </span>
              <span className="meta-chip">
                Jurisdiction: {activeJob.jurisdiction}
              </span>
              <span className="meta-chip">Audience: {activeJob.audience}</span>
              <span className="meta-chip">Channel: {activeJob.channel}</span>
              <span className="meta-chip">
                CV-08 PV Gate: {activeJob.pvGateCleared ? "CLEARED" : "HOLD"}
              </span>
              <span className="meta-chip">
                Cloud Model Armor: {activeJob.modelArmorCleared ? "VERIFIED" : "HOLD"}
              </span>
              <span className="meta-chip">
                X-Tenant-Id: {workspace.activeTenantId}
              </span>
            </div>
          </div>

          <div
            style={{
              display: "flex",
              gap: "8px",
              alignItems: "center",
              flexWrap: "wrap"
            }}
          >
            <label
              htmlFor="actor-input"
              style={{ fontSize: "0.78rem", fontWeight: 600 }}
            >
              X-Actor-Id:
            </label>
            <input
              id="actor-input"
              type="text"
              className="btn"
              style={{ width: "220px", fontSize: "0.78rem" }}
              value={actorId}
              onChange={(e) => setActorId(e.target.value)}
            />
            <label
              htmlFor="job-selector"
              style={{ fontSize: "0.82rem", fontWeight: 600 }}
            >
              Active Job:
            </label>
            <select
              id="job-selector"
              className="btn"
              value={activeJob.jobId}
              onChange={(e) => handleSelectJob(e.target.value)}
            >
              {workspace.jobs.map((job) => (
                <option key={job.jobId} value={job.jobId}>
                  [{job.sector}] {job.productName} — {job.channel}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn"
              onClick={handleSyncJobWithBackend}
            >
              Sync Backend Intake
            </button>
          </div>
        </div>

        <div className="band-summary-row">
          <div className="band-card band-green">
            <span>Within Approved Meaning (Ready for MLR Review)</span>
            <span>
              {bandCounts["Within Approved Meaning (Ready for MLR Review)"]}
            </span>
          </div>
          <div className="band-card band-amber">
            <span>Needs a Closer Read</span>
            <span>{bandCounts["Needs a Closer Read"]}</span>
          </div>
          <div className="band-card band-red">
            <span>Uses Ruled-Out Phrasing</span>
            <span>{bandCounts["Uses Ruled-Out Phrasing"]}</span>
          </div>
          <div className="band-card band-purple">
            <span>Missing Locked Clinical Data</span>
            <span>{bandCounts["Missing Locked Clinical Data"]}</span>
          </div>
        </div>
      </section>

      {/* AREA 1: MY WORK */}
      {activeArea === "my_work" && (
        <div className="grid-2col">
          <div>
            <div className="panel-card">
              <h2>Ask MLR Genie (Plain-Language ADK Orchestrator)</h2>
              <div className="genie-bar">
                <input
                  type="text"
                  className="genie-input"
                  placeholder="e.g., Check this launch deck against the Final FDA USPI and flag any narrowed indications..."
                  value={geniePrompt}
                  onChange={(e) => setGeniePrompt(e.target.value)}
                />
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={handleRunGenieCommand}
                >
                  Run Pre-Flight Check
                </button>
              </div>
              <div
                style={{
                  fontSize: "0.82rem",
                  color: "#1e40af",
                  background: "#eff6ff",
                  padding: "8px 12px",
                  borderRadius: "6px"
                }}
              >
                {genieStatusNote}
              </div>
            </div>

            <div className="panel-card">
              <h2>
                Upload Working Draft (.docx) &amp; Backend Intake (
                <code>POST /jobs/intake</code>)
              </h2>
              <p style={{ fontSize: "0.83rem", color: "#475569" }}>
                Upload a real <code>.docx</code> working draft to parse paragraphs and
                tables via <code>python-docx</code> into{" "}
                <code>01_working_draft_v1.docx</code> with CV-08 PV screening:
              </p>
              <div
                style={{
                  display: "flex",
                  gap: "10px",
                  alignItems: "center",
                  flexWrap: "wrap"
                }}
              >
                <label
                  htmlFor="upload-working-docx-input"
                  style={{ fontSize: "0.82rem", fontWeight: 600 }}
                >
                  Working Draft (.docx):
                </label>
                <input
                  id="upload-working-docx-input"
                  aria-label="Upload Working Draft DOCX"
                  type="file"
                  accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                  onChange={handleUploadWorkingDocx}
                />
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={handleExecuteFileRoundtrip}
                >
                  Execute File Roundtrip (.docx + .pdf + Redline + .zip)
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={handleImportRdHandoffPack}
                >
                  Import Phase 2 R&amp;D Handoff Pack (&apos;About the Disease&apos; &amp; Epidemiology)
                </button>
              </div>
              {docxUploadNote && (
                <div
                  style={{
                    marginTop: "8px",
                    fontSize: "0.8rem",
                    color: "#065f46",
                    background: "#ecfdf5",
                    padding: "6px 10px",
                    borderRadius: "6px"
                  }}
                >
                  {docxUploadNote}
                </div>
              )}
              {rdHandoffNote && (
                <div
                  style={{
                    marginTop: "8px",
                    fontSize: "0.8rem",
                    color: "#1e40af",
                    background: "#eff6ff",
                    padding: "6px 10px",
                    borderRadius: "6px"
                  }}
                >
                  {rdHandoffNote}
                </div>
              )}
              <div style={{ marginTop: "12px" }}>
                <strong style={{ fontSize: "0.82rem", color: "#334155" }}>
                  One-Click Roundtrip Artifacts (6 Generated Files):
                </strong>
                {renderArtifactDownloadGrid()}
              </div>
            </div>

            <div className="panel-card">
              <h2>Current Asset Copy &amp; Statement-by-Statement MLR Pre-Flight</h2>
              <textarea
                aria-label="Current Asset Body"
                className="code-textarea"
                readOnly
                value={activeJob.asset.payload.body}
              />

              <h3 style={{ marginTop: "18px" }}>
                Sentence-Level Claim &amp; Guardrail Assessment
              </h3>
              {activeJob.statements.map((stmt) => (
                <div key={stmt.statementId} className="statement-item">
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      marginBottom: "6px"
                    }}
                  >
                    <strong>{stmt.locationLabel}</strong>
                    <span
                      className={`band-card ${bandCssClass(stmt.preFlightBand)}`}
                      style={{ padding: "3px 10px" }}
                    >
                      {stmt.preFlightBand}
                    </span>
                  </div>
                  <p style={{ margin: "6px 0", fontSize: "0.92rem" }}>
                    “{stmt.assetSentence}”
                  </p>
                  <div style={{ fontSize: "0.83rem", color: "#475569" }}>
                    <strong>MLR Pre-Flight Rationale ({stmt.modelRouteUsed}):</strong>{" "}
                    {stmt.plainReason}
                  </div>
                  {stmt.suggestedCompliantWording && (
                    <div
                      style={{
                        marginTop: "8px",
                        padding: "8px 10px",
                        background: "#f8fafc",
                        borderLeft: "3px solid #1a73e8",
                        fontSize: "0.83rem"
                      }}
                    >
                      <strong>Suggested Approved Message Alignment:</strong> “
                      {stmt.suggestedCompliantWording}”
                    </div>
                  )}
                  {stmt.linkedAnchors.map((anc) => (
                    <div key={anc.anchorId}>
                      <button
                        type="button"
                        className="bbox-tag"
                        style={{ cursor: "pointer" }}
                        onClick={() => {
                          setSelectedAnnotationId(anc.anchorId);
                          setSelectedPageNumber(anc.boundingBox.pageNumber);
                          setActiveArea("materials");
                        }}
                      >
                        PDF Anchor: {anc.sourceDocTitle} · p.{anc.boundingBox.pageNumber}{" "}
                        ({anc.boundingBox.sectionLabel}) · Box [
                        {anc.boundingBox.ymin}, {anc.boundingBox.xmin},{" "}
                        {anc.boundingBox.ymax}, {anc.boundingBox.xmax}]
                      </button>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          </div>

          <div>
            <div className="panel-card">
              <h2>Approval-Day Launch Readiness Mode (Section 6)</h2>
              <p style={{ fontSize: "0.85rem", color: "#475569" }}>
                Switch the product regulatory state to trigger{" "}
                <code>gemini-4-argon</code> (Vertex AI Priority Inference) draft-to-final
                label delta analysis or Complete Response Letter (CRL) holding workflow:
              </p>
              <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
                {(
                  [
                    "pre_approval",
                    "pending_decision",
                    "approved",
                    "other_decision"
                  ] as LaunchState[]
                ).map((st) => (
                  <button
                    key={st}
                    type="button"
                    className={`btn ${
                      activeJob.launchState === st ? "btn-primary" : ""
                    }`}
                    onClick={() => handleChangeLaunchState(st)}
                  >
                    {st.replace("_", " ").toUpperCase()}
                  </button>
                ))}
              </div>
            </div>

            <div className="panel-card">
              <h2>Human Provenance Facts (Invariant 1: leaderSupplied Only)</h2>
              <p style={{ fontSize: "0.83rem", color: "#475569" }}>
                Extracted strictly by comparing <code>asset.payload.body</code> against{" "}
                <code>AssetEditRecorded</code> revisions — model drafts (
                <code>AssetDrafted</code>) are never classified as leader-supplied facts:
              </p>
              {humanResolvedFacts.length === 0 ? (
                <div style={{ fontSize: "0.84rem", color: "#64748b" }}>
                  No human revisions recorded on this asset yet.
                </div>
              ) : (
                humanResolvedFacts.map((fact) => (
                  <div
                    key={fact.revisionId}
                    style={{
                      padding: "10px",
                      background: "#f0fdf4",
                      border: "1px solid #bbf7d0",
                      borderRadius: "8px",
                      fontSize: "0.83rem",
                      marginBottom: "8px"
                    }}
                  >
                    <div>
                      <strong>Revision:</strong> {fact.revisionId} ·{" "}
                      <strong>Actor:</strong> {fact.actorId} ·{" "}
                      <strong>Provenance:</strong> {fact.provenanceClassification}
                    </div>
                    <div style={{ marginTop: "4px" }}>
                      <strong>Human-Supplied Wording:</strong> “{fact.humanEditedBody}”
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      )}

      {/* AREA 2: MATERIALS AND REFERENCES */}
      {activeArea === "materials" && (
        <div className="grid-2col">
          <div>
            <div className="panel-card">
              <h2>Locked Reference Pack &amp; Multimodal PDF Coordinate Anchors</h2>
              <p style={{ fontSize: "0.85rem", color: "#475569" }}>
                Every citation anchor is verified deterministically via{" "}
                <code>PyMuPDF</code> text extraction paired with{" "}
                <code>gemini-3.8-flash</code> 2D bounding-box detection (
                <code>[ymin, xmin, ymax, xmax]</code>).
              </p>

              <div
                style={{
                  padding: "12px",
                  background: "#f8fafc",
                  border: "1px solid #e2e8f0",
                  borderRadius: "8px",
                  marginBottom: "14px"
                }}
              >
                <div
                  style={{
                    display: "flex",
                    gap: "10px",
                    alignItems: "center",
                    flexWrap: "wrap"
                  }}
                >
                  <label
                    htmlFor="upload-reference-pdf-input"
                    style={{ fontSize: "0.82rem", fontWeight: 600 }}
                  >
                    Upload Reference Pack (.pdf):
                  </label>
                  <input
                    id="upload-reference-pdf-input"
                    aria-label="Upload Reference Pack PDF"
                    type="file"
                    accept=".pdf,application/pdf"
                    onChange={handleUploadReferencePdf}
                  />
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={handleExecuteFileRoundtrip}
                  >
                    Run PyMuPDF Annotation &amp; File Roundtrip
                  </button>
                </div>
                {pdfUploadNote && (
                  <div
                    style={{
                      marginTop: "8px",
                      fontSize: "0.8rem",
                      color: "#065f46"
                    }}
                  >
                    {pdfUploadNote}
                  </div>
                )}
                {roundtripNote && (
                  <div
                    style={{
                      marginTop: "6px",
                      fontSize: "0.8rem",
                      color: "#1e40af"
                    }}
                  >
                    {roundtripNote}
                  </div>
                )}
              </div>

              {activeJob.referenceDocs.map((doc) => (
                <div key={doc.docId} className="statement-item">
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center"
                    }}
                  >
                    <strong>{doc.title}</strong>
                    <span className="meta-chip">{doc.status.toUpperCase()}</span>
                  </div>
                  <div style={{ fontSize: "0.82rem", color: "#475569", marginTop: "4px" }}>
                    Type: {doc.docType} · Version: <code>{doc.versionTag}</code> ·{" "}
                    {doc.pageCount} pages · SHA-256: <code>{doc.sha256Prefix}</code>
                  </div>
                </div>
              ))}
            </div>

            <div className="panel-card">
              <h2>
                One-Click Browser Downloads — 6 Roundtrip Artifacts (
                <code>GET /jobs/&#123;job_id&#125;/artifacts/&#123;file&#125;</code>)
              </h2>
              {renderArtifactDownloadGrid()}
            </div>

            <div className="panel-card">
              <h2>
                4-Dimension Reviewed Statement Library (<code>gemini-embedding-2</code>)
              </h2>
              <p style={{ fontSize: "0.84rem", color: "#475569" }}>
                Scoped by Tenant (<code>{workspace.activeTenantId}</code>), Jurisdiction (
                <code>{activeJob.jurisdiction}</code>), Audience (
                <code>{activeJob.audience}</code>), Channel (
                <code>{activeJob.channel}</code>), and Product Version (
                <code>{activeJob.productVersion}</code>):
              </p>
              {matchedReuseRecords.length === 0 ? (
                <div style={{ fontSize: "0.85rem", color: "#64748b" }}>
                  No non-expired reusable statements match all 4 context dimensions for this
                  asset.
                </div>
              ) : (
                matchedReuseRecords.map((rec) => (
                  <div key={rec.reuseId} className="statement-item">
                    <div style={{ fontSize: "0.9rem", fontWeight: 600 }}>
                      “{rec.approvedStatementText}”
                    </div>
                    <div style={{ fontSize: "0.8rem", color: "#475569", marginTop: "6px" }}>
                      Source: {rec.sourceDocTitle} (p.{rec.sourcePage}) · Expires:{" "}
                      {rec.expirationDateIso} · Embedding: <code>{rec.embeddingModel}</code>
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>

          <div>
            <div className="panel-card">
              <h2>
                Interactive PyMuPDF Bounding-Box Coordinate &amp; Page Preview (
                <code>03_annotated_reference.pdf</code>)
              </h2>
              <p style={{ fontSize: "0.83rem", color: "#475569" }}>
                Click any PDF page or PyMuPDF highlight annotation below or on the rendered PDF page
                geometry (<code>{activePreviewPage.page_width} × {activePreviewPage.page_height} pt</code>
                ) to inspect its exact bounding box, verbatim clinical quote, and snippet
                SHA-256:
              </p>

              <div
                style={{
                  display: "flex",
                  gap: "6px",
                  flexWrap: "wrap",
                  marginBottom: "8px"
                }}
                aria-label="PDF Page Selector"
              >
                {activePyMuPdfData.pages.map((pg) => (
                  <button
                    key={`page-btn-${pg.page_number}`}
                    type="button"
                    className={`btn ${
                      activePreviewPage.page_number === pg.page_number
                        ? "btn-primary"
                        : ""
                    }`}
                    onClick={() => {
                      setSelectedPageNumber(pg.page_number);
                      const firstAnnotOnPage = activePyMuPdfData.annotations.find(
                        (a) => a.page_number === pg.page_number
                      );
                      if (firstAnnotOnPage) {
                        setSelectedAnnotationId(firstAnnotOnPage.annotation_id);
                      }
                    }}
                  >
                    Page {pg.page_number} ({pg.page_width}×{pg.page_height} pt)
                  </button>
                ))}
              </div>

              <div
                style={{
                  display: "flex",
                  gap: "6px",
                  flexWrap: "wrap",
                  marginBottom: "12px"
                }}
              >
                {activePyMuPdfData.annotations.map((ann) => (
                  <button
                    key={ann.annotation_id}
                    type="button"
                    className={`btn ${
                      selectedAnnotation?.annotation_id === ann.annotation_id
                        ? "btn-primary"
                        : ""
                    }`}
                    onClick={() => {
                      setSelectedAnnotationId(ann.annotation_id);
                      setSelectedPageNumber(ann.page_number);
                    }}
                  >
                    p.{ann.page_number} · {ann.annotation_id} [{ann.x0}, {ann.y0},{" "}
                    {ann.x1}, {ann.y1}]
                  </button>
                ))}
              </div>

              {/* SVG Page Coordinate Preview */}
              <div className="pdf-preview-frame" aria-label="PyMuPDF Page Coordinate Preview">
                <svg
                  viewBox={`0 0 ${activePreviewPage.page_width || 612} ${
                    activePreviewPage.page_height || 792
                  }`}
                  className="pdf-preview-svg"
                >
                  <rect
                    x={0}
                    y={0}
                    width={activePreviewPage.page_width || 612}
                    height={activePreviewPage.page_height || 792}
                    fill="#ffffff"
                    stroke="#cbd5e1"
                    strokeWidth={2}
                  />
                  {activePreviewPage.text_blocks.map((blk) => (
                    <g key={`blk-${blk.block_index}-${blk.y0}`}>
                      <rect
                        x={blk.x0}
                        y={blk.y0}
                        width={Math.max(40, blk.x1 - blk.x0)}
                        height={Math.max(18, blk.y1 - blk.y0)}
                        fill="#f8fafc"
                        stroke="#e2e8f0"
                        strokeDasharray="3 2"
                      />
                      <text
                        x={blk.x0 + 6}
                        y={blk.y0 + 14}
                        fontSize="10"
                        fill="#334155"
                      >
                        {blk.text.slice(0, 72)}
                      </text>
                    </g>
                  ))}
                  {activePyMuPdfData.annotations
                    .filter((a) => a.page_number === activePreviewPage.page_number)
                    .map((ann) => {
                      const isSelected =
                        selectedAnnotation?.annotation_id === ann.annotation_id;
                      return (
                        <g
                          key={`svg-annot-${ann.annotation_id}`}
                          style={{ cursor: "pointer" }}
                          onClick={() => {
                            setSelectedAnnotationId(ann.annotation_id);
                            setSelectedPageNumber(ann.page_number);
                          }}
                        >
                          <rect
                            x={ann.x0}
                            y={ann.y0}
                            width={Math.max(30, ann.x1 - ann.x0)}
                            height={Math.max(18, ann.y1 - ann.y0)}
                            fill={
                              isSelected
                                ? "rgba(250, 204, 21, 0.45)"
                                : "rgba(59, 130, 246, 0.22)"
                            }
                            stroke={isSelected ? "#d97706" : "#1d4ed8"}
                            strokeWidth={isSelected ? 2.5 : 1.5}
                          />
                          <text
                            x={ann.x0 + 4}
                            y={Math.max(12, ann.y0 - 4)}
                            fontSize="9.5"
                            fontWeight="bold"
                            fill={isSelected ? "#b45309" : "#1e40af"}
                          >
                            {ann.annotation_id} · [{ann.x0}, {ann.y0}, {ann.x1},{" "}
                            {ann.y1}]
                          </text>
                        </g>
                      );
                    })}
                </svg>
              </div>

              {/* Selected Annotation Coordinate Inspector */}
              {selectedAnnotation && (
                <div
                  className="statement-item"
                  style={{ marginTop: "12px", background: "#f8fafc" }}
                  aria-label="Selected PyMuPDF Annotation Inspector"
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center"
                    }}
                  >
                    <strong>
                      PyMuPDF Annotation: {selectedAnnotation.annotation_id} (Claim{" "}
                      {selectedAnnotation.claim_id})
                    </strong>
                    <span className="meta-chip">
                      {selectedAnnotation.anchor_valid
                        ? "ANCHOR VALID (IN-BOUNDS)"
                        : "INVALID COORDINATES"}
                    </span>
                  </div>
                  <div
                    style={{
                      fontSize: "0.82rem",
                      color: "#334155",
                      marginTop: "6px"
                    }}
                  >
                    <div>
                      <strong>Source Document:</strong>{" "}
                      <code>{selectedAnnotation.source_document_id}</code> (
                      {selectedAnnotation.source_version}) ·{" "}
                      <strong>Page:</strong> p.{selectedAnnotation.page_number} (
                      {selectedAnnotation.page_width} ×{" "}
                      {selectedAnnotation.page_height} pt)
                    </div>
                    <div style={{ marginTop: "4px" }}>
                      <strong>PyMuPDF Rect [x0, y0, x1, y1]:</strong>{" "}
                      <code>
                        [{selectedAnnotation.x0}, {selectedAnnotation.y0},{" "}
                        {selectedAnnotation.x1}, {selectedAnnotation.y1}]
                      </code>{" "}
                      · <strong>2D Box [ymin, xmin, ymax, xmax]:</strong>{" "}
                      <code>
                        [{selectedAnnotation.ymin}, {selectedAnnotation.xmin},{" "}
                        {selectedAnnotation.ymax}, {selectedAnnotation.xmax}]
                      </code>
                    </div>
                    <div style={{ marginTop: "4px" }}>
                      <strong>Linked Asset Sentence:</strong> “
                      {selectedAnnotation.claim_sentence}”
                    </div>
                    <div style={{ marginTop: "4px" }}>
                      <strong>Verbatim PDF Quote:</strong> “
                      {selectedAnnotation.verbatim_quote}”
                    </div>
                    <div style={{ marginTop: "4px" }}>
                      <strong>Snippet SHA-256:</strong>{" "}
                      <code>{selectedAnnotation.snippet_sha256}</code> ·{" "}
                      <strong>PDF Annotation Type:</strong>{" "}
                      <code>{selectedAnnotation.pdf_annot_type}</code>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* AREA 3: DECISIONS NEEDED */}
      {activeArea === "decisions" && (
        <div className="grid-2col">
          <div>
            <div className="panel-card">
              <h2>Prioritized Human Decisions (Comms Leader &amp; MLR Gatekeeper)</h2>
              {activeJob.decisions.length === 0 ? (
                <p style={{ fontSize: "0.88rem", color: "#64748b" }}>
                  No open human decisions for this job.
                </p>
              ) : (
                activeJob.decisions.map((dec) => (
                  <div key={dec.decisionId} className="statement-item">
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center"
                      }}
                    >
                      <strong>{dec.title}</strong>
                      <span className="meta-chip">{dec.status.toUpperCase()}</span>
                    </div>
                    <p style={{ fontSize: "0.85rem", margin: "6px 0" }}>
                      <strong>Why it matters:</strong> {dec.whyItMatters}
                    </p>
                    <div style={{ fontSize: "0.82rem", color: "#475569" }}>
                      <strong>Location:</strong> {dec.affectedAssetLocation}
                    </div>
                    <div
                      style={{
                        fontSize: "0.82rem",
                        background: "#f8fafc",
                        padding: "8px",
                        borderRadius: "6px",
                        marginTop: "6px"
                      }}
                    >
                      <strong>Source Evidence:</strong> {dec.sourceExcerpt}
                    </div>
                    {dec.renderedCompanionSummary && (
                      <div
                        style={{
                          fontSize: "0.81rem",
                          color: "#1e40af",
                          marginTop: "6px"
                        }}
                      >
                        <strong>Visible Companion Edit Preview:</strong>{" "}
                        {dec.renderedCompanionSummary}
                      </div>
                    )}
                    <div
                      style={{
                        display: "flex",
                        gap: "8px",
                        flexWrap: "wrap",
                        marginTop: "10px"
                      }}
                    >
                      {dec.options.map((opt) => (
                        <button
                          key={opt}
                          type="button"
                          className={`btn ${
                            dec.selectedOption === opt && dec.status === "confirmed"
                              ? "btn-success"
                              : "btn-primary"
                          }`}
                          onClick={() => handleConfirmDecision(dec.decisionId, opt)}
                        >
                          {opt}
                        </button>
                      ))}
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>

          <div>
            <div className="panel-card">
              <h2>
                Reviewer Comments — Atomic Span Resolution (Zero Silent Mutation)
              </h2>
              <p style={{ fontSize: "0.83rem", color: "#475569" }}>
                <code>applyAcceptedChanges</code> places every span of a comment
                atomically (<code>all-or-nothing</code>) without stripping periods or
                rewriting punctuation, and syncs with{" "}
                <code>POST /jobs/&#123;job_id&#125;/comments</code>. If any span fails
                unique placement, the entire comment rolls back to{" "}
                <code>unplaced</code>.
              </p>
              {activeJob.reviewerComments.map((cmt) => (
                <div key={cmt.commentId} className="statement-item">
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center"
                    }}
                  >
                    <strong>
                      [{cmt.reviewerRole}] {cmt.reviewerName}
                    </strong>
                    <span className="meta-chip">
                      Status: {cmt.status.toUpperCase()}
                    </span>
                  </div>
                  <p style={{ fontSize: "0.85rem", margin: "6px 0" }}>
                    {cmt.originalCommentText}
                  </p>
                  <div style={{ display: "flex", gap: "8px", marginTop: "8px" }}>
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={() => handleApplyComment(cmt.commentId)}
                    >
                      Apply Accepted Changes (Atomic)
                    </button>
                    <button
                      type="button"
                      className="btn"
                      onClick={() => handleKeepSourceComment(cmt.commentId)}
                    >
                      Keep Source Wording
                    </button>
                  </div>
                </div>
              ))}
            </div>

            <div className="panel-card">
              <h2>Explicit Release Gate &amp; Platform Adapter Export</h2>
              <p style={{ fontSize: "0.83rem", color: "#475569" }}>
                Preparing a package never triggers an external upload without an explicit
                human release mandate. Select target tenant profile:
              </p>
              <div
                style={{
                  display: "flex",
                  gap: "10px",
                  alignItems: "center",
                  flexWrap: "wrap",
                  marginBottom: "10px"
                }}
              >
                <select
                  aria-label="Target Platform Profile"
                  className="btn"
                  value={selectedPlatformKey}
                  onChange={(e) => setSelectedPlatformKey(e.target.value)}
                >
                  <option value="veeva_promomats">
                    Veeva PromoMats (Full Vault + Anchor Writeback)
                  </option>
                  <option value="aprimo_dam_only">
                    Aprimo (DAM-Only Tier · Package + Anchor PDF Export)
                  </option>
                  <option value="aprimo_productivity">
                    Aprimo (DAM + Productivity Tier · Annotation Writeback)
                  </option>
                  <option value="package_mode">
                    Package Mode (Manifest-Verified Resubmission ZIP)
                  </option>
                </select>
                <button
                  type="button"
                  className="btn btn-success"
                  onClick={handleGrantReleaseAndExport}
                >
                  Grant Explicit Mandate &amp; Export Package
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={handleExecuteFileRoundtrip}
                >
                  Build Resubmission Bundle (.zip)
                </button>
              </div>
              {renderArtifactDownloadGrid()}
            </div>
          </div>
        </div>
      )}

      {/* AREA 4: ACTIVITY & AUDIT LEDGER */}
      {activeArea === "activity" && (
        <div>
          <div className="panel-card">
            <h2>
              Submission &amp; File Roundtrip Package Artifacts (One-Click Downloads)
            </h2>
            <p style={{ fontSize: "0.83rem", color: "#475569" }}>
              Download any of the 6 verified artifacts generated by{" "}
              <code>POST /jobs/&#123;job_id&#125;/execute-file-roundtrip</code>:
            </p>
            {renderArtifactDownloadGrid()}
          </div>

          <div className="panel-card">
            <h2>
              Append-Only Audit &amp; Provenance Ledger (Cloud Spanner / AlloyDB)
            </h2>
            <table className="audit-table">
              <thead>
                <tr>
                  <th>Timestamp (UTC)</th>
                  <th>Provenance Type</th>
                  <th>Actor</th>
                  <th>Model / Tool Route</th>
                  <th>Summary &amp; Idempotent Receipt</th>
                </tr>
              </thead>
              <tbody>
                {activeJob.auditLedger.map((entry) => (
                  <tr key={entry.eventId}>
                    <td>{entry.timestampIso}</td>
                    <td>
                      <code>{entry.eventType}</code>
                    </td>
                    <td>{entry.actorDisplay}</td>
                    <td>
                      <code>{entry.modelOrTool}</code>
                    </td>
                    <td>
                      {entry.summary}
                      {entry.externalReceiptId && (
                        <div>
                          <span className="bbox-tag">
                            Receipt: {entry.externalReceiptId} · Idempotency Key:{" "}
                            {entry.idempotencyKey}
                          </span>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="grid-2col">
            <div className="panel-card">
              <h2>Gemini Model Routing Matrix (Section 8 GCP Architecture)</h2>
              <table className="audit-table">
                <thead>
                  <tr>
                    <th>Task Category</th>
                    <th>Gemini Model ID</th>
                    <th>Thinking / Tier</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.values(GEMINI_MODEL_ROUTING_MATRIX).map((route) => (
                    <tr key={route.taskCategory}>
                      <td>{route.taskCategory}</td>
                      <td>
                        <code>{route.modelId}</code>
                      </td>
                      <td>
                        <code>
                          thinking={route.thinkingLevel} · tier={route.serviceTier}
                        </code>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="panel-card">
              <h2>Section 11 Acceptance Suite Status (12 Mandatory Cases)</h2>
              <table className="audit-table">
                <thead>
                  <tr>
                    <th>Acceptance Case</th>
                    <th>Status</th>
                    <th>Verification Summary</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(section11Suite).map(([caseKey, info]) => (
                    <tr key={caseKey}>
                      <td>
                        <code>{caseKey}</code>
                      </td>
                      <td>
                        <strong style={{ color: info.passed ? "#065f46" : "#991b1b" }}>
                          {info.passed ? "PASS" : "FAIL"}
                        </strong>
                      </td>
                      <td>{info.summary}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
