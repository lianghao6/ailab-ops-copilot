# V2 Investigation UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a clear, dependency-light Web UI that teaches and demonstrates a V2 investigation from intake through evidence, hypotheses, cited report, and approval-gated simulated action.

**Architecture:** Extend the existing FastAPI application with read-only presentation endpoints only where the current V2 API lacks data, then replace the legacy single-file dashboard with a focused static application. The UI polls typed investigation resources and renders server-owned facts; it never reconstructs diagnosis, policy, citation validity, or approval decisions in browser code.

**Tech Stack:** FastAPI, existing V2 runtime/API, semantic HTML, modern CSS, vanilla JavaScript, pytest/TestClient; no Node build chain.

**Spec:** `docs/superpowers/specs/2026-09-30-enterprise-incident-agent-v2-design.md`

## Global Constraints

- The UI is a teaching and investigation interface, not an operations-metric dashboard.
- Online and authored replay modes must be visually unmistakable on every investigation view.
- All conclusions, evidence, policy decisions and approval states come from V2 server responses.
- Evidence excerpts are rendered as text, never injected as HTML.
- Actions remain simulated and require the existing explicit approval flow.
- Tenant ownership checks apply to every investigation, evidence and approval request.
- No Node/npm dependency or external CDN is introduced; the UI works offline in replay mode.
- Existing V2 API, CLI and 435-test baseline remain compatible.

## Review Focus

- Malicious evidence text containing HTML/script is displayed literally and cannot execute (Task 2).
- Replay mode is not mistaken for a live model investigation (Tasks 1–3).
- A wrong tenant cannot retrieve an investigation, evidence or operate its approval (Task 1).
- Refresh/reload during pending, completed, rejected or expired approval reconstructs the same view (Tasks 1 and 3).
- Narrow screens, long evidence lines and long Chinese/English text remain readable without horizontal page overflow (Tasks 2 and 4).

---

### Task 1: Complete the presentation API

**Files:**
- Modify: `src/ailab_ops/serving/v2.py`
- Modify: `src/ailab_ops/v2_runtime.py`
- Create: `tests/test_v2_presentation_api.py`

**Interfaces:**
- Consumes: V2 investigation states, evidence archive, trace events, approval store and tenant ownership.
- Produces: `GET /v2/investigations/{session_id}/evidence`, `GET /v2/investigations/{session_id}/timeline`, `GET /v2/investigations/{session_id}/approvals`, and a stable aggregate detail response used by the UI.

- [ ] **Step 1: Write failing presentation API tests**

  Cover replay/online mode, plan, hypotheses, evidence with stable IDs and source metadata, report citations, bounded/redacted timeline, pending and terminal approvals, reload consistency, tenant mismatch 404, and unknown session 404.

- [ ] **Step 2: Run tests and verify RED**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_v2_presentation_api.py -q`  
  Expected: missing routes or response fields.

- [ ] **Step 3: Add typed presentation serializers and routes**

  Return copies of server state and evidence. Timeline payloads are already redacted by `TraceRecorder`; cap returned events and expose truncation metadata. Never return hidden evaluation labels, API credentials, raw model private reasoning, or another tenant's state.

- [ ] **Step 4: Run focused and API regression tests**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_v2_presentation_api.py tests/test_v2_api.py -q`  
  Expected: all pass.

- [ ] **Step 5: Commit**

  ```bash
  git add src/ailab_ops/serving/v2.py src/ailab_ops/v2_runtime.py tests/test_v2_presentation_api.py
  git commit -m "feat: expose v2 investigation presentation data"
  ```

### Task 2: Build the investigation workspace UI

**Files:**
- Create: `src/ailab_ops/serving/static/v2/index.html`
- Create: `src/ailab_ops/serving/static/v2/app.css`
- Create: `src/ailab_ops/serving/static/v2/app.js`
- Modify: `src/ailab_ops/serving/app.py`
- Modify: `pyproject.toml`
- Create: `tests/test_v2_ui.py`

**Interfaces:**
- Consumes: Task 1 aggregate/detail endpoints and existing create/list/health endpoints.
- Produces: `/` V2 investigation workspace and static assets under `/static/v2/`.

- [ ] **Step 1: Write failing UI delivery and safety tests**

  Assert the root serves V2 UI, assets are local and cacheable, CSP forbids remote scripts, replay banner and mode copy exist, all dynamic rendering uses `textContent`/safe node construction, and no `innerHTML` receives server data.

- [ ] **Step 2: Verify RED**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_v2_ui.py -q`  
  Expected: V2 assets/routes absent.

- [ ] **Step 3: Implement semantic HTML and visual system**

  Create a bookish technical interface rather than a generic admin template: intake and case selector, mode banner, investigation header, phase rail, evidence board, hypotheses, cited report, timeline, and action panel. Use local system/font stack, CSS variables, accessible contrast, focus states and reduced-motion support.

- [ ] **Step 4: Implement safe browser state and API client**

  Use DOM node construction and `textContent`; encode URL segments; abort stale requests; show typed empty/loading/error/replay-miss states; preserve tenant and current session locally without storing secrets.

- [ ] **Step 5: Run UI and API tests**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_v2_ui.py tests/test_v2_presentation_api.py -q`  
  Expected: all pass.

- [ ] **Step 6: Commit**

  ```bash
  git add src/ailab_ops/serving/static/v2 src/ailab_ops/serving/app.py pyproject.toml tests/test_v2_ui.py
  git commit -m "feat: add the v2 investigation workspace"
  ```

### Task 3: Add approval and investigation interactions

**Files:**
- Modify: `src/ailab_ops/serving/static/v2/app.js`
- Modify: `src/ailab_ops/serving/static/v2/index.html`
- Modify: `src/ailab_ops/serving/static/v2/app.css`
- Create: `tests/test_v2_ui_flows.py`

**Interfaces:**
- Consumes: existing investigation and approval mutation endpoints plus Task 1 presentation resources.
- Produces: replay start, online investigation start, refresh, action proposal, approve, reject and simulated execute flows.

- [ ] **Step 1: Write failing static contract and browser-flow tests**

  Test request shapes and state transitions with a lightweight fake DOM/API harness or deterministic JS contract extraction: replay start, typed 422 replay miss, completion refresh, proposal validation, explicit confirmation, rejection reason, approve then execute, expiry, double-submit prevention and retryable errors.

- [ ] **Step 2: Verify RED**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_v2_ui_flows.py -q`  
  Expected: interaction hooks/functions absent.

- [ ] **Step 3: Implement investigation controls**

  Disable duplicate submissions, expose budget stop/cancel/error honestly, poll only while non-terminal, and keep the last server-confirmed state on transient errors.

- [ ] **Step 4: Implement approval controls**

  Display tool, parameters, cited evidence, reason, risk, rollback and expiry before confirmation. Never combine approve and execute into one click. Label execution result as simulation and render the full audit sequence.

- [ ] **Step 5: Run interaction and backend regression tests**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_v2_ui_flows.py tests/test_v2_api.py tests/test_approvals.py -q`  
  Expected: all pass.

- [ ] **Step 6: Commit**

  ```bash
  git add src/ailab_ops/serving/static/v2 tests/test_v2_ui_flows.py
  git commit -m "feat: add investigation and approval UI flows"
  ```

### Task 4: Visual QA, accessibility and documentation

**Files:**
- Modify: `src/ailab_ops/serving/static/v2/index.html`
- Modify: `src/ailab_ops/serving/static/v2/app.css`
- Modify: `src/ailab_ops/serving/static/v2/app.js`
- Modify: `README.md`
- Create: `docs/ui/v2-investigation-workspace.md`
- Create: `tests/test_v2_ui_accessibility.py`

**Interfaces:**
- Consumes: completed Tasks 1–3.
- Produces: documented, keyboard-usable, responsive V2 workspace ready to capture for the textbook.

- [ ] **Step 1: Add failing accessibility/responsive contract tests**

  Assert a single H1, labelled controls, live status region, keyboard-operable tabs/disclosures, focus-visible styles, reduced motion, responsive breakpoints, wrapped code/evidence text and no fixed desktop-only content widths.

- [ ] **Step 2: Verify RED and correct markup/styles**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_v2_ui_accessibility.py -q`  
  Expected: missing contracts fail, then pass after corrections.

- [ ] **Step 3: Run a local replay service and capture deterministic QA pages**

  Inspect at desktop and narrow viewport: intake, investigating, completed report, insufficient evidence, pending approval, rejected and simulated execution. Record findings and fixes in `docs/ui/v2-investigation-workspace.md`; do not commit generated browser caches.

- [ ] **Step 4: Update README UI walkthrough**

  Put online mode first and replay second; label replay and simulation; explain evidence/hypothesis/timeline/approval panels without claiming production authentication or persistence.

- [ ] **Step 5: Run full verification**

  Run: `PYTHONPATH=src python3 -m pytest -q`  
  Expected: all tests pass with only the documented legacy sample skip.  
  Run: `PYTHONPATH=src python3 -m ailab_ops.cli replay --case case-gpu-assert`  
  Expected: completed cited report including independent Runbook evidence.

- [ ] **Step 6: Commit**

  ```bash
  git add src/ailab_ops/serving/static/v2 README.md docs/ui tests/test_v2_ui_accessibility.py
  git commit -m "docs: finish the v2 investigation workspace"
  ```

## Follow-on Plan

After this UI plan is complete and reviewed, execute a separate six-course textbook plan. It will use the stable UI for screenshots and the stable V2 CLI/evaluation outputs for diagrams and measured results; it will generate six A4 PDFs and one combined volume.
