# MLR Genie Prototype — Independent Review Package (`v3.0.0-proto`)

This repository contains the **MLR Genie** prototype workspace (`c:\Users\abasu\Documents\antigravity\resilient-maxwell`), including:
- **Python Backend & Deterministic Domain/Tool Engine**: `backend/` (`FastAPI`, `Pydantic`, custom ADK-style `MlrGenieAgent` wrapper, `GeminiModelRouter`, and deterministic review/document/adapter tools)
- **TypeScript Domain Engine & 4-Area React Client**: `src/` and `client/` (`React 18`, `TypeScript`, `Vite`, `Vitest`)
- **Evaluation & Test Suites**: `evals/acceptance_suite.py` (12 Section 11 cases), `evals/pharma_comms_scenario_battery.py` (15 Pharma/Biotech/MedTech scenarios), `tests/test_mlr_genie.py` (`pytest`), and `src/__tests__/mlrGenie.test.ts` (`vitest`)
- **Reference Infrastructure-as-Code**: `deploy/terraform/main.tf` (unapplied Terraform template)

> **Important Scope Notice**: This is a **local prototype with deterministic/in-memory tool implementations and synthetic fixtures**, not a production-deployed system. See [`INDEPENDENT_REVIEW_HANDOFF.md`](./INDEPENDENT_REVIEW_HANDOFF.md) for the complete component-by-component reality check.

---

## 1. Required Runtime & Tool Versions

- **Operating System**: Windows 10/11, macOS, or Linux
- **Python**: `>= 3.11` (verified on `Python 3.12.12`)
  - Python packages required to run tests and the FastAPI server:
    - `pydantic >= 2.7.0`
    - `fastapi >= 0.115.0` (verified on `0.137.1`)
    - `uvicorn >= 0.30.0` (verified on `0.49.0`)
    - `pytest >= 8.2.0` (verified on `9.1.1`)
  - *Note on `pyproject.toml`*: `pyproject.toml` also declares `google-genai>=2.30.1`, `pymupdf>=1.24.0`, and `python-docx>=1.1.2`. In the current code, `pymupdf` (`fitz`) and `python-docx` (`docx`) are **not imported** anywhere in `backend/`, and `google-genai` is imported lazily inside a `try/except` block only if `GEMINI_API_KEY` is set in the environment (`backend/agent/models_router.py:44-50`).
- **Node.js & npm**: `Node.js >= 20.x`, `npm >= 10.x`
  - Verified with `TypeScript 5.5.4`, `Vite 5.4.21`, `Vitest 2.1.9`, `React 18.3.1`.

---

## 2. Safe Execution With Fictional Data & External Call / Cost Disclosure

### External Calls & Paid API Cost Disclosure
1. **Default Mode (`GEMINI_API_KEY` unset) — ZERO External Calls / ZERO Cost**:
   - When `GEMINI_API_KEY` is **not** set in the environment, `GeminiModelRouter.__init__` (`backend/agent/models_router.py:44-50`) leaves `self._client = None`.
   - Every call to `router.create_interaction(...)` (`backend/agent/models_router.py:70-130`) and `router.embed_multimodal(...)` (`backend/agent/models_router.py:132-161`) executes **100% locally** using deterministic fallback payloads and SHA-256 token hashing (`used_live_sdk=False`).
2. **If `GEMINI_API_KEY` Is Set — Potential Paid API Calls**:
   - If `GEMINI_API_KEY` is set in your shell and `google-genai` is installed, `GeminiModelRouter` initializes `genai.Client(api_key=...)` and will attempt live calls to `self._client.interactions.create(...)` and `self._client.models.embed_content(...)` using the configured model strings (`gemini-4-argon`, `gemini-3.8-flash`, `gemini-3.5-flash-lite`, `gemini-embedding-2`). If the model ID is unavailable or the call raises an exception, it catches `Exception` (`backend/agent/models_router.py:118-119`, `147-148`) and falls back to the deterministic local result.
   - **To guarantee zero paid model calls**, ensure `GEMINI_API_KEY` is unset before running any command:
     ```powershell
     Remove-Item Env:\GEMINI_API_KEY -ErrorAction SilentlyContinue
     ```
3. **Veeva Vault PromoMats & Aprimo Calls — ZERO External Network Calls**:
   - `CapabilityCheckedPlatformAdapter` (`backend/tools/platform_adapters.py:15-186`) is an **in-memory simulation** backed by a Python `dict` (`self._external_store = {}`). It makes **no HTTP requests** to Veeva or Aprimo servers under any configuration.
4. **React Web UI (`client/src/App.tsx`) — ZERO Backend or External Network Calls**:
   - The React client runs entirely in browser memory against `src/domain/mlrGenieEngine.ts` (`createInitialWorkspaceState()`). It does not call `fetch()` or `XMLHttpRequest` to the FastAPI backend or any external API.
5. **Fictional Data Only**:
   - All pre-loaded brands and clinical studies (*Aurenix / AUR-301 / AURORA-301*, *Celvanta / BTX-409*, *OncoVex*, *PulseNav 3D / K261044*, *CardioPulse v4.2*) are 100% synthetic/fictional.

---

## 3. Exact Installation Commands

### A. Node / TypeScript / React Client Setup
```powershell
cd C:\Users\abasu\Documents\antigravity\resilient-maxwell
npm ci
```
*(Or `npm install` if regenerating `node_modules`.)*

### B. Python Environment Setup
```powershell
cd C:\Users\abasu\Documents\antigravity\resilient-maxwell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# Install only the packages actively imported by the runtime & test suite:
pip install "pydantic>=2.7.0" "fastapi>=0.115.0" "uvicorn>=0.30.0" "pytest>=8.2.0" "httpx>=0.27.0"
```

---

## 4. Exact Startup Commands

### Start the Python FastAPI Server (`http://127.0.0.1:8000`)
```powershell
Remove-Item Env:\GEMINI_API_KEY -ErrorAction SilentlyContinue
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```
- Interactive OpenAPI / Swagger UI: `http://127.0.0.1:8000/docs`
- Health check: `http://127.0.0.1:8000/health`
- Run 12 Section 11 acceptance cases via HTTP: `http://127.0.0.1:8000/evals/run-12-cases`
- Run 15 Pharma Comms scenarios via HTTP: `http://127.0.0.1:8000/evals/run-pharma-comms-battery`

### Start the React 4-Area Web UI (`http://127.0.0.1:5173`)
```powershell
npx vite --host 127.0.0.1 --port 5173
```

---

## 5. Exact Typecheck, Build & Test Commands

### TypeScript Typechecks, Production Bundle Build & Vitest Unit Tests
```powershell
npm run check
npm run client:check
npm run client:build
npx vitest run
```

### Python Pytest Suite & Standalone Evaluation Scripts
```powershell
Remove-Item Env:\GEMINI_API_KEY -ErrorAction SilentlyContinue
python -m pytest tests/ evals/ -v
python -m evals.acceptance_suite
python -m evals.pharma_comms_scenario_battery
```
