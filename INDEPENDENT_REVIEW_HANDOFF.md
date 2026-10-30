# MLR Genie Prototype — Independent-Review Handoff & Technical Reality Check

- **Workspace Path**: `C:\Users\abasu\Documents\antigravity\resilient-maxwell`
- **Downloadable Archive (Workspace Copy)**: `C:\Users\abasu\Documents\antigravity\resilient-maxwell\mlr-genie-independent-review-handoff.zip`
- **Downloadable Archive (Artifact Copy)**: `C:\Users\abasu\.gemini\antigravity\brain\7060dbb5-1e3c-40e0-bea0-c9d4a97d6829\mlr-genie-independent-review-handoff.zip`
- **Timestamp**: `2026-10-28T17:15:00-04:00`
- **Git Commit State**: Uncommitted working tree on empty Git repository (`git status -s` shows all files untracked `??`; tied to the cryptographic SHA-256 manifest in Section 5.4).

Every technical assertion in this handoff is labeled with one of four verification states:
- **`VERIFIED BY EXECUTION`**: Executed in this environment; raw command output or HTTP response is included.
- **`VERIFIED BY CODE`**: Confirmed directly by inspecting the source files and line numbers cited.
- **`SIMULATED`**: Implemented as an in-memory stub, deterministic fallback, or hard-coded fixture rather than a real external integration or model execution.
- **`NOT YET VERIFIED`**: Not implemented, not executed, or not tested against a real external service.

---

## 1. Downloadable ZIP Package & Sanitized Environment Template

- **ZIP Location 1 (Workspace)**: [`mlr-genie-independent-review-handoff.zip`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/mlr-genie-independent-review-handoff.zip) (**`VERIFIED BY EXECUTION`**)
- **ZIP Location 2 (Artifacts)**: [`mlr-genie-independent-review-handoff.zip`](file:///C:/Users/abasu/.gemini/antigravity/brain/7060dbb5-1e3c-40e0-bea0-c9d4a97d6829/mlr-genie-independent-review-handoff.zip) (**`VERIFIED BY EXECUTION`**)
- **Included Files**:
  - Python backend & tools: `backend/__init__.py`, `backend/config.py`, `backend/main.py`, `backend/domain/schemas.py`, `backend/agent/mlr_genie_agent.py`, `backend/agent/models_router.py`, `backend/tools/document_tools.py`, `backend/tools/review_tools.py`, `backend/tools/platform_adapters.py`
  - TypeScript domain & React client: `src/prompt.ts`, `src/domain/mlrGenieEngine.ts`, `client/src/App.tsx`, `client/src/main.tsx`, `client/src/styles.css`, `index.html`
  - Tests & evaluations: `tests/test_mlr_genie.py`, `evals/acceptance_suite.py`, `evals/pharma_comms_scenario_battery.py`, `src/__tests__/mlrGenie.test.ts`
  - Deployment & manifests: `deploy/terraform/main.tf`, `pyproject.toml`, `package.json`, `package-lock.json`, `tsconfig.json`, `tsconfig.client.json`, `vite.config.ts`, `.gitignore`, `.env.example`
  - Project specification, documentation & handoff: `.agents-cli-spec.md`, `README.md`, `INDEPENDENT_REVIEW_HANDOFF.md`, `docs/implementation_plan.md`, `docs/walkthrough.md`, `docs/mlr_genie_v2_archive.md`, `docs/mlr_genie_v3_gcp_adk_plan.md`
- **Excluded Items**: `.env` files, credentials, OAuth tokens, private keys, customer data, `node_modules/`, `.venv/`, `.git/`, `.pytest_cache/`, `__pycache__/`, and `dist/` (**`VERIFIED BY EXECUTION`**).
- **Sanitized [`.env.example`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/.env.example)** (variable names only):
  ```dotenv
  GOOGLE_CLOUD_PROJECT=
  GOOGLE_CLOUD_LOCATION=
  GOOGLE_GENAI_USE_VERTEXAI=
  GEMINI_API_KEY=
  GCP_CMEK_KEY_NAME=
  GCP_MODEL_ARMOR_TEMPLATE=
  MLR_GCS_BUCKET=
  DATABASE_URL=
  VEEVA_OAUTH_SECRET=
  APRIMO_OAUTH_SECRET=
  ```

---

## 2. Exact Installation, Startup, Test Commands & Cost/Network Disclosure

Full instructions are in [`README.md`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/README.md). Summary for independent reproduction:

### 2.1 Required Versions
- **Python**: `>= 3.11` (executed on `Python 3.12.12`, `pytest 9.1.1`, `fastapi 0.137.1`, `uvicorn 0.49.0`, `pydantic 2.x`) (**`VERIFIED BY EXECUTION`**)
- **Node.js**: `>= 20.x` (executed with `typescript 5.5.4`, `vite 5.4.21`, `vitest 2.1.9`, `react 18.3.1`) (**`VERIFIED BY EXECUTION`**)

### 2.2 Exact Commands
```powershell
# 1. Install Node dependencies
npm ci

# 2. Ensure zero paid Gemini API calls by unsetting GEMINI_API_KEY
Remove-Item Env:\GEMINI_API_KEY -ErrorAction SilentlyContinue

# 3. Run TypeScript typechecks, Vite build, and Vitest unit tests
npm run check
npm run client:check
npm run client:build
npx vitest run

# 4. Run Python pytest suite and standalone evaluation scripts
python -m pytest tests/ evals/ -v
python -m evals.acceptance_suite
python -m evals.pharma_comms_scenario_battery

# 5. Start local servers (optional)
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
npx vite --host 127.0.0.1 --port 5173
```

### 2.3 External Network Call & Paid API Cost Disclosure
- **With `GEMINI_API_KEY` unset (default)**: **Zero external network calls and zero paid API costs** (`VERIFIED BY CODE` at [`backend/agent/models_router.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/agent/models_router.py#L44-L50)). `self._client` remains `None`, and all router calls return local deterministic fallbacks (`used_live_sdk=False`).
- **With `GEMINI_API_KEY` set**: `GeminiModelRouter` initializes `google.genai.Client(api_key=...)` and attempts network calls to `self._client.interactions.create(...)` and `self._client.models.embed_content(...)` (`backend/agent/models_router.py:87-148`), which could incur Gemini API charges if pointed at valid billable model IDs.
- **Veeva & Aprimo Adapters**: **Zero external network calls under all configurations** (`VERIFIED BY CODE` at [`backend/tools/platform_adapters.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/platform_adapters.py#L34-L35)).
- **React Client**: **Zero network calls** (`VERIFIED BY CODE` at [`client/src/App.tsx`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/client/src/App.tsx#L33-L35)).

---

## 3. Component-by-Component Reality Check

| Subsystem | Status Label | What Is Actually Implemented vs. Mocked / Unfinished (with Source Citations) |
| :--- | :--- | :--- |
| **1. ADK Execution** | **`SIMULATED`** (Custom in-process Python class; no `google-adk` package) | [`pyproject.toml`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/pyproject.toml#L11-L18) does **not** depend on `google-adk`, and no code imports `google.adk`. [`MlrGenieAgent`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/agent/mlr_genie_agent.py#L22-L131) is a 110-line custom Python class holding a dictionary of functions (`self.registered_tools`). There is no LLM-driven agent loop or autonomous tool selection; callers invoke `genie.invoke_tool("tool_name", ...)` deterministically in Python ([`backend/main.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/main.py#L83-L107)). (**`VERIFIED BY CODE`**) |
| **2. Gemini Model Calls** | **`SIMULATED`** in all runs (`used_live_sdk=False`); **`NOT YET VERIFIED`** against live API | [`backend/config.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/config.py#L36-L41) and [`src/prompt.ts`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/src/prompt.ts#L10-L79) configure future/custom model strings (`gemini-4-argon`, `gemini-3.8-flash`, `gemini-3.5-flash-lite`, `gemini-3.5-transcribe`, `antigravity-preview-09-2026`). In [`GeminiModelRouter.create_interaction`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/agent/models_router.py#L70-L130), when `self._client` is `None` (or when `self._client.interactions.create` raises an exception inside `except Exception: pass` at lines 118–119), the router appends `ModelCallTelemetry(used_live_sdk=False, ...)` and returns the caller-supplied `fallback_payload`. Moreover, in [`document_tools.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/document_tools.py#L302-L307) and [`review_tools.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/review_tools.py#L40-L45), the return value of `create_interaction()` is discarded; all claim and comment logic is computed by local Python rules before `create_interaction()` is even called. (**`VERIFIED BY CODE`**) |
| **3. Embeddings & Retrieval** | **`SIMULATED`** (Exact string/date filtering; no vector search) | [`GeminiModelRouter.embed_multimodal`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/agent/models_router.py#L132-L161) has a 16-float SHA-256 token hash fallback, **but `embed_multimodal` is never called anywhere in the codebase** (`VERIFIED BY CODE`). Both [`query_statement_reuse_library`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/review_tools.py#L97-L170) and [`queryStatementReuseLibrary`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/src/domain/mlrGenieEngine.ts#L390-L416) iterate over an in-memory list and compare string fields (`tenant_id`, `jurisdiction`, `audience`, `channel`, `product_version`) and ISO dates. No `pgvector` database or cosine similarity calculation exists. |
| **4. Document Parsing** | **`SIMULATED`** (Plain strings & keyword substring matching) | Although `pymupdf>=1.24.0` and `python-docx>=1.1.2` appear in [`pyproject.toml`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/pyproject.toml#L16-L17), neither `fitz` nor `docx` is imported in any `.py` file (**`VERIFIED BY CODE`**). [`ingest_working_document`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/document_tools.py#L26-L48) accepts a Python `str`. [`locate_and_anchor_evidence`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/document_tools.py#L171-L327) splits text with `split_medical_sentences()`, extracts digits via `re.findall(r"\b\d+(?:\.\d+)?%?\b", sent)`, and matches a sentence to the **first** `source_pack` dictionary where `any(kw in sent.lower() for kw in src.get("keywords", []))` (`lines 210–215`). |
| **5. PDF Coordinates** | **`SIMULATED`** (Hard-coded fixture coordinates & arithmetic addition) | No PDF pages are parsed or measured. [`locate_and_anchor_evidence`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/document_tools.py#L281-L295) copies the `bbox` dict from the matched `source_pack` item (or defaults to `page=1, x0=72, y0=140, x1=540, y1=195`). [`reanchor_after_reflow`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/document_tools.py#L330-L361) adds a caller-provided `vertical_shift_pt` float to `y0`/`y1` and increments `page_number` if `new_y1 > page_height_pt - 36.0`. In the React UI ([`src/domain/mlrGenieEngine.ts`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/src/domain/mlrGenieEngine.ts#L609-L905)), bounding boxes (`[ymin, xmin, ymax, xmax]`) and `verifiedByPyMuPdf: true` / `verifiedByGeminiVision: true` are hard-coded static literals in `createInitialWorkspaceState()`. (**`VERIFIED BY CODE`**) |
| **6. DOCX Editing & Redlines** | **`VERIFIED BY EXECUTION`** on plain strings; **`NOT YET VERIFIED`** on `.docx` files | [`apply_wording_span`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/document_tools.py#L51-L63), [`apply_accepted_changes`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/document_tools.py#L66-L128), and [`keep_source`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/document_tools.py#L131-L141) (and their TypeScript counterparts in [`src/domain/mlrGenieEngine.ts`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/src/domain/mlrGenieEngine.ts#L346-L402)) deterministically enforce unique substring matching (`body.count(target_text) == 1`), preserve punctuation, and roll back multi-span comments atomically to `"unplaced"` if any span fails. However, they operate **only on in-memory plain strings**—no `.docx` file manipulation or Word Track Changes redline generation exists. |
| **7. Persistence** | **`SIMULATED`** (Volatile in-memory dicts/React state only) | In Python, [`JOBS_DB: dict[str, JobRecord] = {}`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/main.py#L46) is an in-process dictionary. `database_url` and `gcs_bucket` in [`backend/config.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/config.py#L30-L33) are unused configuration strings. In React ([`client/src/App.tsx`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/client/src/App.tsx#L33-L35)), state is held in `useState(() => createInitialWorkspaceState())`. **Nothing survives a server restart or browser refresh.** (**`VERIFIED BY CODE`**) |
| **8. Authentication** | **`NOT YET VERIFIED`** (Unimplemented) | Neither [`backend/main.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/main.py) nor [`client/src/App.tsx`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/client/src/App.tsx) implements user authentication, tokens, or session middleware. Actors (`actor`, `actor_human`) are passed as plain strings in request payloads. (**`VERIFIED BY CODE`**) |
| **9. Tenant Isolation** | **`VERIFIED BY EXECUTION`** in unit calls; **`NOT YET VERIFIED`** in HTTP routes | [`MlrGenieAgent.before_tool_callback`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/agent/mlr_genie_agent.py#L66-L70) blocks a call only if `kwargs.get("requesting_tenant_id")` is non-empty and differs from `job.tenant_id`. In [`backend/main.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/main.py#L81-L144), none of the FastAPI endpoints pass `requesting_tenant_id` or authenticate the caller's tenant. [`query_statement_reuse_library`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/review_tools.py#L118-L120) filters out records where `rec.tenant_id != requesting_tenant_id`. (**`VERIFIED BY CODE`**) |
| **10. Approval Gates & Safety (`CV-08`, Model Armor)** | **`VERIFIED BY EXECUTION`** on unit functions; **Partial Wiring Gap** in HTTP route | In [`backend/tools/review_tools.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/review_tools.py#L32-L38), `screen_pharmacovigilance_cv08` checks only 5 hard-coded phrases (`"patient experienced"`, `"hospitalized after taking"`, `"adverse event report"`, `"reported severe nausea after"`, `"anaphylaxis in a 64-year-old"`). Furthermore, in [`backend/main.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/main.py#L83-L107), `/jobs/intake` calls `screen_pharmacovigilance_cv08` **but does not abort on `cv08_flagged=True`**—it continues running `ingest_working_document` and `locate_and_anchor_evidence`. Google Cloud Model Armor is not called anywhere; `modelArmorCleared: true` in [`src/domain/mlrGenieEngine.ts`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/src/domain/mlrGenieEngine.ts#L574) is a hard-coded boolean. (**`VERIFIED BY CODE`**) |
| **11. Veeva PromoMats & Aprimo Connectors** | **`SIMULATED`** (In-memory dict stub) | [`CapabilityCheckedPlatformAdapter`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/platform_adapters.py#L15-L226) is an in-memory state machine using `self._external_store = {}` (`line 34`). It enforces capability matrices (`veeva_promomats` vs. `aprimo_dam` vs. `aprimo_productivity`), idempotency keys, and webhook deduplication in memory, but makes **zero real calls** to Veeva or Aprimo APIs. (**`VERIFIED BY CODE`**) |

---

## 4. End-to-End Trace of One Actual User Action & State Authority Analysis

### 4.1 Does the React Interface Call the Python Backend?
- **Answer**: **No (`VERIFIED BY CODE`)**.
- Inspecting [`client/src/App.tsx`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/client/src/App.tsx#L1-L654) and [`src/domain/mlrGenieEngine.ts`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/src/domain/mlrGenieEngine.ts#L1-L1012) confirms there are **zero `fetch()`, `axios`, or `WebSocket` calls** in the frontend code. The React UI and the Python FastAPI backend are two parallel, unconnected implementations of the domain logic.

### 4.2 Trace A: Actual User Action in the React Interface ("Apply Accepted Changes (Atomic)")
1. **UI Trigger**: In the **Decisions needed** tab of [`client/src/App.tsx`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/client/src/App.tsx#L533-L539), clicking **"Apply Accepted Changes (Atomic)"** on a reviewer comment invokes `handleApplyComment(cmt.commentId)` ([`client/src/App.tsx:142-194`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/client/src/App.tsx#L142-L194)).
2. **Domain Function Call**: `handleApplyComment` locates the active `MlrJob` in React `workspace` state and calls [`applyAcceptedChanges(job.asset.payload.body, targetComment)`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/src/domain/mlrGenieEngine.ts#L351-L386).
3. **Span Placement**: `applyAcceptedChanges` loops through `comment.proposedSpans` and calls [`applyWordingSpan(workingBody, span)`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/src/domain/mlrGenieEngine.ts#L316-L344). If every span matches uniquely (`firstIdx !== -1 && secondIdx === -1`), it returns `{ updatedBody, updatedComment: { ...comment, status: "applied" }, appliedAtomically: true }`. If any span fails unique placement, it rolls back to the original `body` and sets `status: "unplaced"`.
4. **Saved Result**: `setWorkspace(...)` updates the in-memory React state hook (`job.asset.payload.body`, `job.asset.revisions`, `job.reviewerComments`, and prepends an `AuditLedgerEntry` to `job.auditLedger`). Because it is stored only in React `useState`, **reloading the browser tab resets all changes back to `createInitialWorkspaceState()`** (**`VERIFIED BY CODE`**).

### 4.3 Trace B: Actual HTTP Request to the Python Backend (`POST /jobs/intake`)
1. **HTTP Entrypoint**: Sending `POST http://127.0.0.1:8000/jobs/intake` invokes [`create_job_intake(req: IntakeRequest)`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/main.py#L80-L112).
2. **Agent Wrapper Dispatch**: `create_job_intake` sequentially calls `genie.invoke_tool(...)` ([`backend/agent/mlr_genie_agent.py:97-131`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/agent/mlr_genie_agent.py#L97-L131)) four times:
   - `screen_pharmacovigilance_cv08` ([`review_tools.py:19-61`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/review_tools.py#L19-L61))
   - `ingest_working_document` ([`document_tools.py:26-48`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/document_tools.py#L26-L48))
   - `locate_and_anchor_evidence` ([`document_tools.py:171-327`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/document_tools.py#L171-L327), with `required_mandate_action="prepare_annotations"` checked in `before_tool_callback`)
   - `validate_strategy` ([`review_tools.py:64-94`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/review_tools.py#L64-L94))
3. **Saved Result & Authoritative State**: Line 108 of [`backend/main.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/main.py#L108) executes `JOBS_DB[job.job_id] = job`, where `JOBS_DB` is a module-level Python `dict` (`line 46`), and `genie.tool_audit_log` is an in-memory `list` on `MlrGenieAgent`. **When the Uvicorn process restarts, `JOBS_DB` and `genie.tool_audit_log` are emptied completely** (**`VERIFIED BY CODE`**).

---

## 5. Complete Test & Evaluation Inventory, Categorization, Raw Results & File-Hash Manifest

### 5.1 Separation of Test Categories
1. **Local Fixture Unit & Scenario Tests (`VERIFIED BY EXECUTION`)**:
   - Python: [`tests/test_mlr_genie.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/tests/test_mlr_genie.py) (7 tests), [`evals/acceptance_suite.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/evals/acceptance_suite.py) (12 cases), [`evals/pharma_comms_scenario_battery.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/evals/pharma_comms_scenario_battery.py) (15 cases). All run against in-memory Pydantic fixtures and deterministic Python functions.
   - TypeScript: [`src/__tests__/mlrGenie.test.ts`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/src/__tests__/mlrGenie.test.ts) (7 tests). Runs against in-memory TypeScript objects in `src/domain/mlrGenieEngine.ts`.
2. **Live Local HTTP Endpoint Tests (`VERIFIED BY EXECUTION`)**:
   - Executed against `http://127.0.0.1:8000/health`, `http://127.0.0.1:8000/evals/run-12-cases`, and `http://127.0.0.1:8000/jobs/intake`.
3. **Real Paid Gemini / Vertex AI Model Tests (`NOT YET VERIFIED`)**:
   - **0 tests executed against real Gemini APIs.** Every test in `pytest` and `vitest` ran with `GEMINI_API_KEY` unset (`used_live_sdk=False`).
4. **Real External Platform Tests — Veeva PromoMats / Aprimo (`NOT YET VERIFIED`)**:
   - **0 tests executed against real Veeva or Aprimo endpoints.** All connector tests (`case_10`, `case_11`, `E08`, `test_aprimo_dam_vs_productivity_capability_separation_and_cv08_gate`) tested the in-memory `CapabilityCheckedPlatformAdapter` simulation class (`SIMULATED`).

### 5.2 Raw Execution Output — Local Fixture Tests (`VERIFIED BY EXECUTION`)

```text
> mlr-genie-gcp-adk@3.0.0 check
> tsc --noEmit

> mlr-genie-gcp-adk@3.0.0 client:check
> tsc --noEmit -p tsconfig.client.json

> mlr-genie-gcp-adk@3.0.0 client:build
> vite build

vite v5.4.21 building for production...
transforming...
✓ 33 modules transformed.
rendering chunks...
computing gzip size...
dist/index.html                   0.46 kB │ gzip:  0.33 kB
dist/assets/index-DwXveonb.css    4.26 kB │ gzip:  1.35 kB
dist/assets/index-BE1dsSKf.js   195.36 kB │ gzip: 60.44 kB
✓ built in 1.85s

 RUN  v2.1.9 C:/Users/abasu/Documents/antigravity/resilient-maxwell

 ✓ src/__tests__/mlrGenie.test.ts (7 tests) 12ms

 Test Files  1 passed (1)
      Tests  7 passed (7)

============================= test session starts =============================
platform win32 -- Python 3.12.12, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\Users\abasu\Documents\antigravity\resilient-maxwell
configfile: pyproject.toml
collected 7 items

tests/test_mlr_genie.py::test_all_12_section_11_acceptance_cases_pass PASSED [ 14%]
tests/test_mlr_genie.py::test_all_15_pharma_comms_real_world_scenarios_pass PASSED [ 28%]
tests/test_mlr_genie.py::test_zero_silent_mutation_atomic_all_or_nothing_rollback PASSED [ 42%]
tests/test_mlr_genie.py::test_apply_wording_span_preserves_exact_punctuation PASSED [ 57%]
tests/test_mlr_genie.py::test_invariant_1_strict_human_vs_model_provenance PASSED [ 71%]
tests/test_mlr_genie.py::test_invariant_10_heuristics_advise_never_block PASSED [ 85%]
tests/test_mlr_genie.py::test_aprimo_dam_vs_productivity_capability_separation_and_cv08_gate PASSED [100%]

============================== 7 passed in 0.44s ==============================
```

### 5.3 Raw Execution Output — Live Local HTTP Smoke Tests (`VERIFIED BY EXECUTION`)

1. **First attempt at `POST http://127.0.0.1:8000/jobs/intake`** (with `"sector": "pharma"`):
   - Returned `HTTP Error 422: Unprocessable Entity` because `SectorType` requires `'pharma_preapproval'`, `'pharma_approved'`, `'biotech'`, `'medtech'`, or `'corporate'`.
2. **Second attempt at `POST http://127.0.0.1:8000/jobs/intake`** (with `"sector": "pharma_approved"` and `source_pack: [{"doc_id": "doc-csr-301", "text": "..."}]`):
   - Returned `HTTP 200 OK`, **but marked both sentences `"support_status": "unresolved"`** (`"Missing reference: No authorized document in the source pack supports this statement."`) because `locate_and_anchor_evidence` (`backend/tools/document_tools.py:210-215`) looks for `"keywords"` and `"verbatim_text"` keys in `source_pack` dicts, whereas `IntakeRequest.source_pack: list[dict[str, Any]]` (`backend/main.py:59`) is untyped and accepted `"text"` without schema validation:
   ```json
   {
     "job_id": "live-test-001",
     "state": "draft",
     "pv_screening": {
       "cv08_flagged": false,
       "routing_note": null,
       "model_used": "gemini-3.5-flash-lite"
     },
     "claims": [
       {
         "claim_id": "clm_live-test-001_1",
         "sentence_text": "Aurenix 100mg reduced primary flaring events by 34% at Week 16 (p=0.0012; 95% CI: 0.54-0.82).",
         "paragraph_index": 0,
         "support_status": "unresolved",
         "locked_numbers": ["34", "16", "0.0012", "95", "0.54", "0.82"],
         "required_qualifiers": [],
         "missing_qualifiers": [],
         "paired_isi_present": false,
         "requires_isi": true,
         "plain_english_diagnosis": "Missing reference: No authorized document in the source pack supports this statement.",
         "anchors": [],
         "model_used": "gemini-4-argon"
       },
       {
         "claim_id": "clm_live-test-001_2",
         "sentence_text": "Aurenix is a 100% cure with zero risk.",
         "paragraph_index": 0,
         "support_status": "unresolved",
         "locked_numbers": ["100"],
         "required_qualifiers": [],
         "missing_qualifiers": [],
         "paired_isi_present": false,
         "requires_isi": true,
         "plain_english_diagnosis": "Missing reference: No authorized document in the source pack supports this statement.",
         "anchors": [],
         "model_used": "gemini-4-argon"
       }
     ],
     "advisory_findings": []
   }
   ```

### 5.4 Cryptographic File-Hash Manifest (`SHA-256`, `VERIFIED BY EXECUTION`)

| File Path | Size (Bytes) | SHA-256 Digest |
| :--- | ---: | :--- |
| `.agents-cli-spec.md` | 2,870 | `a316431b1f2e44680fcf07f3abcfe71e064df5a3aab90c42cfad783ac53205a5` |
| `.env.example` | 412 | `2c3e20e4a6792843a907ed496c56da86032086f5e6ab71fc0f27dfe88b5bdd0b` |
| `.gitignore` | 91 | `8b9a27f6a9c078cd694afa450b34fae80b9628012222e90a0bfe3b077de3dc28` |
| `README.md` | 6,133 | `7719460f7a6882a1b97950443a085326d9b728a33e2ea9167044e1c644be8ab8` |
| `backend/__init__.py` | 62 | `1044ee2f109dfffe8f2d254724a6aee6ae2a676e74284270c7d8cb1a7539c478` |
| `backend/agent/__init__.py` | 70 | `5d015742b9df420a8b8d440da761c50e216936debb3d29c6e682dcf0658d7490` |
| `backend/agent/mlr_genie_agent.py` | 4,992 | `a9a5b79df6e537aac15d6e048edb16bdad35d015e091e47842b78c20e9d22dda` |
| `backend/agent/models_router.py` | 6,621 | `cfe09922924681d03fe3121bde892feaf46a57b9fcc604cdbc7d84b8c78691f2` |
| `backend/config.py` | 1,855 | `4663f5a382a33a85f1d3eef3ed16614c0ae7bd9878c9013b1227f1b6709ab460` |
| `backend/domain/__init__.py` | 76 | `c1e14862e5721fb64e74f1af8a444de1eba8ebc605ca6ff34a9f446d9a14a2f7` |
| `backend/domain/schemas.py` | 9,082 | `f3a72814aa6b6faf5f14b7f5be335c56478e1b76257dbe4cf63704924c0d527c` |
| `backend/main.py` | 5,789 | `52d515df3183402de9af44cba9ac76113f846180de89ba4fc00a7e82d5466914` |
| `backend/tools/__init__.py` | 60 | `9f56b5992177bade17203ae0059573a1dd275768102650321e1c91ee595118a2` |
| `backend/tools/document_tools.py` | 15,453 | `332ceb6222eb41980fb53ccf14bc623f5560591717d8fe72b6925996aabeed00` |
| `backend/tools/platform_adapters.py` | 10,602 | `672419c02484f4772044f90da823e159efaed0831ab0f58a3473fe318d198743` |
| `backend/tools/review_tools.py` | 14,250 | `b8c5c671a92aaf14bf569ee3ecb50e785c85a79a3ab1e15177bc0c6fb29e706a` |
| `client/src/App.tsx` | 36,244 | `9b3c9b47d4cc6277249d9e59fcbb8c5c12c6c90e49cea913d6667f028491cc2d` |
| `client/src/main.tsx` | 316 | `725b6dcc4a912fbc62dec1be8212e6d207e0eaa79f3f0a552916ee8ac5bfa4c5` |
| `client/src/styles.css` | 5,208 | `f5c70ad365618c346c3c3b5a010f8b52bf8996826f934d5a872c2699f3dec3b1` |
| `deploy/terraform/main.tf` | 3,639 | `993ae80cf2ccaf9fb5278329e9d144e6c86046a69d3cc13ea59a67ce3e5c172b` |
| `evals/acceptance_suite.py` | 19,193 | `a4720a19c0e2f9faa75c2b3c3bcc26828955bc22b3603b10bdb7fe92eef89c28` |
| `evals/pharma_comms_scenario_battery.py` | 30,779 | `3221eed0afbd33de0e70a265d5740a3bd7480597d9b82d570cdb54f6bd360116` |
| `index.html` | 373 | `82b832d35dc14ed76f6ad0d1111991190d55eeada65260d391149436f6ab1c0c` |
| `package-lock.json` | 73,220 | `98b48a0a064fd500c54ff37c2221852737f9107e7ecd6a0f83bc73208c88bed0` |
| `package.json` | 753 | `b53cb52601a4fcb73d528a93364da8558fffedffc015a52fb527f1d691209751` |
| `pyproject.toml` | 643 | `e45f28bbbb88fb3b7f1437984eb2082ee0796e054b9f4f790f399c8a6a3fd8cc` |
| `src/__tests__/mlrGenie.test.ts` | 7,635 | `d198eb65ac2c90eaf8bd6deb2e2295ce6fd3fdf8d8d849122f8b486d95108c77` |
| `src/domain/mlrGenieEngine.ts` | 48,880 | `d83bb38fef5604d1ae423afd1afc88270cb4413c20b018aedb4f3bee0ced9e0f` |
| `src/prompt.ts` | 5,065 | `d71003012cb75c50f8c66856bf1e2a05960a5bed648acdc379f9a6493ce35ca7` |
| `tests/test_mlr_genie.py` | 8,172 | `b1600b286cb2834a8ccbb1e46696cea7831c298fd648371bbd094e02c1149472` |
| `tsconfig.client.json` | 81 | `8d10246116788497c72ab15e12f78ec31a70c8b32ae47b5da8681bdfe1b17419` |
| `tsconfig.json` | 430 | `d5d651faaeb0a1bdb5315f6c6fc866f6ce92adcaefc71a8fe9128129ce919820` |
| `vite.config.ts` | 251 | `09f490fd91d97df9d0bf7357ef8e52634836a697339ff947401383567f6630fa` |

---

## 6. End-to-End File Demonstration Artifacts Check (`prepare -> reviewer comments -> revise -> resubmit-package`)

- **Status**: **`NOT YET VERIFIED`** (File-based workflow has **not** been demonstrated).
- **Explicit Disclosure**:
  - There are **no** original fictional source PDF files, **no** annotated reference PDF files, **no** `.docx` drafts, **no** revised clean `.docx` files, **no** `.docx` Track Changes redline files, and **no** exported `.zip` submission packages in the workspace.
  - All evaluations in [`evals/acceptance_suite.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/evals/acceptance_suite.py) and [`evals/pharma_comms_scenario_battery.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/evals/pharma_comms_scenario_battery.py) operate on in-memory Python strings and dictionaries (`JobRecord`, `ReviewComment`, `WordingSpanEdit`, `ConnectorReceipt`), and the React UI ([`client/src/App.tsx`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/client/src/App.tsx)) operates on in-memory TypeScript objects (`createInitialWorkspaceState()`).
  - Per your instruction, no synthetic `.docx`, `.pdf`, or submission `.zip` artifacts have been manufactured to fill this gap.

---

## 7. Model & Connector Execution Receipts & Zero-Data-Retention (ZDR) Evidence

### 7.1 Model Execution Receipts
- **Status**: **`SIMULATED`** (`used_live_sdk=False` across all runs; **zero real Gemini API calls were executed**).
- Example telemetry object produced by [`GeminiModelRouter.create_interaction`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/agent/models_router.py#L121-L130) during `evals/acceptance_suite.py`:
  ```json
  {
    "model": "gemini-4-argon",
    "task_type": "claim_verification",
    "service_tier": "standard",
    "used_live_sdk": false,
    "previous_interaction_id": null,
    "interaction_id": "int_7f9c4b1e8a20"
  }
  ```
  There is no upstream HTTP request path, token usage metadata, or Vertex AI response ID because `self._client` is `None` (`used_live_sdk: false`).

### 7.2 Connector Execution Receipts (Veeva Vault PromoMats & Aprimo)
- **Status**: **`SIMULATED`** (Generated by in-memory dictionary `self._external_store` in [`CapabilityCheckedPlatformAdapter.execute_action`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/platform_adapters.py#L69-L186)).
- Example receipt generated in Acceptance Case 10 (`evals/acceptance_suite.py:374-395`):
  ```json
  {
    "receipt_id": "rcpt_4c1e89f0a2",
    "job_id": "job_case_10",
    "destination_platform": "veeva_promomats",
    "action": "upload_version",
    "capability_status": "verified",
    "external_object_id": "VEEVA_PROMOMATS-JOB_CASE_10-DOC",
    "external_version": "1.0",
    "actor_human": "sarah.chen@onco.example",
    "integration_identity": "mcp-veeva_promomats-sa@mlr-genie-enterprise-prod.iam.gserviceaccount.com",
    "idempotency_key": "idem_job10_v1",
    "read_back_verified": true,
    "read_back_digest": "8f2d9c1a...",
    "status": "reconciled_no_duplicate",
    "detail": "Reconciled existing external record via idempotency key; prevented duplicate creation."
  }
  ```
  Both `external_object_id` and `read_back_verified: true` come from reading back `self._external_store[idempotency_key]` in local Python memory (`platform_adapters.py:106-125, 160-161`).

### 7.3 Evidence Behind Zero-Data-Retention (ZDR) Claims
- **Status**: **`NOT YET VERIFIED`** (Config constant only).
- In [`backend/config.py:17`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/config.py#L17), `zero_data_retention: bool = True` is a hard-coded dataclass attribute echoed by `GET /health` ([`backend/main.py:68`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/main.py#L68)).
- Furthermore, in [`backend/agent/models_router.py:44-50`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/agent/models_router.py#L44-L50), if `GEMINI_API_KEY` is provided, the router calls `genai.Client(api_key=self.config.gemini_api_key)` without passing `vertexai=True`, `project=self.config.project_id`, `location=self.config.location`, or any CMEK / ZDR headers. Nor has [`deploy/terraform/main.tf`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/deploy/terraform/main.tf) been applied to a real GCP project. Therefore, ZDR is an architectural target in the specification, **not** an active runtime control in this prototype.

---

## 8. Known Failures, Limitations, Untested Cases & Clarification of "Four Dimensions"

### 8.1 Clarification: What "Four Dimensions" Actually Means (`VERIFIED BY CODE`)
- **"Four dimensions" does NOT refer to an embedding vector size.** It refers to **four business/governance metadata attributes** evaluated when deciding whether a previously reviewed statement in the Statement Reuse Library can be reused without new MLR review.
- Additionally, code inspection reveals a **schema divergence between the Python backend and the TypeScript engine** (**`VERIFIED BY CODE`**):
  1. **Python ([`backend/domain/schemas.py:143-146`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/domain/schemas.py#L143-L146) & [`backend/tools/review_tools.py:109-168`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/review_tools.py#L109-L168))**: Defines the 4 dimensions as:
     - `evidence_support` (`verified_source`, `partial_source`, `unverified`)
     - `public_disclosure` (`public_company_footprint`, `external_peer_precedent`, `internal_confidential`)
     - `review_decision` (`cleared_in_context`, `scoped_ruling`, `adopted_house_rule`, `rejected_or_replaced`, `awaiting_review`)
     - `permitted_contexts` (list of `"jurisdiction:audience:channel"` strings), alongside separate fields for `jurisdiction`, `audience`, `channel`, `product_or_device_version`, and `valid_until`.
     - *(Separately, `GeminiModelRouter.embed_multimodal` in [`models_router.py:132`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/agent/models_router.py#L132) defaults to `dimensions: int = 16` for its unused dummy vector).*
  2. **TypeScript ([`src/domain/mlrGenieEngine.ts:390-416`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/src/domain/mlrGenieEngine.ts#L390-L416))**: Implements the 4 dimensions as exact equality on:
     - `jurisdiction`
     - `audience`
     - `channel`
     - `productVersion` (plus `tenantId` and `isExpired`). It omits `evidence_support`, `public_disclosure`, and `review_decision`.

### 8.2 Known Bugs, Architectural Disconnects & Untested Cases (`VERIFIED BY CODE`)
1. **Complete Disconnection Between React UI and Python FastAPI Backend**:
   - [`client/src/App.tsx`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/client/src/App.tsx) never makes HTTP calls to [`backend/main.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/main.py). Any edits made in the browser UI exist only in browser RAM, and any jobs created via `POST /jobs/intake` on port 8000 never appear in the React UI.
2. **Untyped `source_pack` in `POST /jobs/intake` Causes False-Negative `"Missing reference"` (`VERIFIED BY EXECUTION`)**:
   - [`IntakeRequest.source_pack`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/main.py#L59) is typed as `list[dict[str, Any]]`. If a caller submits source dicts without a `"keywords"` list and `"verbatim_text"` key (for example, passing `"text"` or `"quote"`), [`locate_and_anchor_evidence`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/document_tools.py#L210-L221) silently fails to match any source and marks every claim as `unresolved` (`"Missing reference"`).
3. **`POST /jobs/intake` Does Not Halt When `CV-08` Pharmacovigilance Screening Flags an Adverse Event**:
   - In [`backend/main.py:83-107`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/main.py#L83-L107), `create_job_intake` calls `screen_pharmacovigilance_cv08`, stores `pv_result`, and then **unconditionally proceeds** to ingest the text as a working comms document and run claim anchoring, instead of halting intake when `pv_result["cv08_flagged"] is True`.
4. **Narrow Hard-Coded Keyword List in `screen_pharmacovigilance_cv08` and `validate_strategy`**:
   - [`screen_pharmacovigilance_cv08`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/review_tools.py#L32-L38) only matches 5 literal substrings (`"patient experienced"`, `"hospitalized after taking"`, `"adverse event report"`, `"reported severe nausea after"`, `"anaphylaxis in a 64-year-old"`). Any other adverse event phrasing (e.g., `"Patient developed acute liver failure after starting Aurenix"`) is missed (`cv08_flagged: False`).
   - Similarly, Python [`validate_strategy`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/review_tools.py#L72-L81) only checks 2 literal phrases (`"best-in-class"` and `"guaranteed"`).
5. **Naive First-Keyword Match & Number-Contradiction Heuristic in `locate_and_anchor_evidence`**:
   - [`locate_and_anchor_evidence`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/tools/document_tools.py#L210-L254) binds a sentence to the **first** `source_pack` entry sharing any keyword, and marks a sentence `contradicted` whenever any number in `locked_numbers` is absent from the sentence while any other number (such as `"Week 16"` or `"Phase 3"`) is present in the sentence.
6. **Non-Existent / Placeholder Model Names & Unwired Vertex AI Client**:
   - The model identifiers `gemini-4-argon`, `gemini-3.8-flash`, `gemini-3.5-flash-lite`, `gemini-3.5-transcribe`, and `antigravity-preview-09-2026` in [`backend/config.py`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/config.py#L36-L41) are specification placeholders, and [`GeminiModelRouter`](file:///c:/Users/abasu/Documents/antigravity/resilient-maxwell/backend/agent/models_router.py#L44-L50) does not wire `vertexai=True` into `google.genai.Client`.
7. **Cases Not Covered by Current Tests**:
   - Real PDF parsing, OCR, multi-column table extraction, or actual visual bounding-box detection.
   - Real `.docx` ingestion, formatting preservation, Word comment extraction, or Track Changes redline generation.
   - Concurrent multi-user edits, database transactions, or server restart recovery.
   - Authentication, RBAC, and tenant enforcement on FastAPI HTTP endpoints.
   - Live network error handling, rate limiting, or OAuth token refresh against Veeva Vault PromoMats or Aprimo REST APIs.
