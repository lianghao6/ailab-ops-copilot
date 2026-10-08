# Enterprise Incident Agent V2 Core Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the rule-decided default path with a real-model-first, evidence-grounded incident investigation that also supports honest offline replay and approval-gated simulated actions.

**Architecture:** A typed investigation state is advanced by an orchestrator that lets the model choose read-only tools and propose hypotheses while deterministic services capture evidence, validate citations, enforce budgets, and gate actions. Existing world data, retrievers, gate, limits, and tracing are adapted behind the new interfaces; the old mock reasoner and signal scorer remain only until the V2 path passes its end-to-end tests, then leave the default runtime.

**Tech Stack:** Python 3.10+, dataclasses, Pydantic 2, FastAPI, httpx, PyYAML, pytest, existing BM25/vector retrieval.

**Spec:** `docs/superpowers/specs/2026-09-30-enterprise-incident-agent-v2-design.md`

## Global Constraints

- The default online path uses an OpenAI-compatible API; deterministic code must not select the final root cause.
- No API key is required for tests or offline replay, and replay must be visibly labelled rather than accepting arbitrary unseen questions.
- Knowledge visible to the agent and hidden evaluation labels must not derive from one shared source of truth.
- Every report claim marked as material must cite an evidence ID that exists in the investigation evidence store.
- Read-only tools may execute automatically; action tools only create approval requests and simulated execution requires explicit approval.
- Unit tests never access the network.
- Preserve Python 3.10 compatibility and the public CLI entry point `ailab-ops`.
- Do not delete the old implementation until its V2 replacement and migration tests pass.

## Review Focus

- A model cites an unknown or duplicate evidence ID: report publication is rejected with actionable validation errors (Task 2 tests).
- A model repeatedly calls a tool or exceeds step/token/time budgets: the investigation stops with a recoverable partial result (Task 5 tests).
- A replay request does not exactly match a recorded scenario: replay refuses instead of synthesizing an answer (Task 4 tests).
- An action is executed without approval, after rejection, or after expiry: execution is denied and audited (Task 6 tests).
- Visible knowledge accidentally contains hidden evaluation fields: separation tests scan every tool and knowledge response (Task 3 and Task 7 tests).

---

### Task 1: V2 investigation domain model

**Files:**
- Create: `src/ailab_ops/investigation/models.py`
- Create: `src/ailab_ops/investigation/__init__.py`
- Create: `tests/test_investigation_models.py`

**Interfaces:**
- Consumes: standard-library dataclasses, enums, datetime, and typing only.
- Produces: `InvestigationPhase`, `Budget`, `Hypothesis`, `Claim`, `InvestigationReport`, `InvestigationState`, and their `to_dict()` methods.

- [ ] **Step 1: Write failing model tests**

  Add tests named `test_new_investigation_starts_in_intake`, `test_budget_reports_each_exhausted_dimension`, `test_hypothesis_tracks_support_and_contradiction_without_duplicates`, and `test_state_round_trip_preserves_phase_and_report`. Assert exact enum values, deduplication order, and JSON-compatible dictionaries.

- [ ] **Step 2: Run the model tests and verify failure**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_investigation_models.py -q`  
  Expected: collection fails because `ailab_ops.investigation` does not exist.

- [ ] **Step 3: Implement the typed domain model**

  Define phases `intake`, `investigating`, `validating`, `awaiting_approval`, `completed`, `stopped`. `Budget` owns `max_steps`, `max_tokens`, `deadline_at`, current counters, and `exhausted(now) -> list[str]`. `InvestigationState` owns session identity, question, phase, plan, hypotheses, evidence IDs, report, stop reason, and serialization.

- [ ] **Step 4: Run focused and legacy tests**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_investigation_models.py tests/test_agent.py -q`  
  Expected: all pass.

- [ ] **Step 5: Commit**

  ```bash
  git add src/ailab_ops/investigation tests/test_investigation_models.py
  git commit -m "feat: add v2 investigation domain model"
  ```

### Task 2: Evidence store and report validation

**Files:**
- Create: `src/ailab_ops/evidence/models.py`
- Create: `src/ailab_ops/evidence/store.py`
- Create: `src/ailab_ops/evidence/validation.py`
- Create: `src/ailab_ops/evidence/__init__.py`
- Create: `tests/test_evidence.py`

**Interfaces:**
- Consumes: `Claim` and `InvestigationReport` from Task 1.
- Produces: `Evidence`, `EvidenceRelation`, `EvidenceStore.add(Evidence) -> Evidence`, `EvidenceStore.get(str) -> Evidence | None`, `EvidenceStore.to_context(ids) -> list[dict]`, and `validate_report(report, store) -> list[ValidationIssue]`.

- [ ] **Step 1: Write failing evidence tests**

  Add `test_store_assigns_stable_ids_and_deduplicates_identical_sources`, `test_context_marks_truncated_evidence`, `test_report_rejects_unknown_evidence_id`, `test_report_rejects_material_claim_without_citation`, and `test_report_accepts_existing_citations`. Pin stable IDs to the prefix `ev-` without pinning a hash implementation.

- [ ] **Step 2: Verify tests fail for missing package**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_evidence.py -q`  
  Expected: import failure for `ailab_ops.evidence`.

- [ ] **Step 3: Implement evidence types and in-memory store**

  `Evidence` includes source tool, arguments, summary, excerpt, observed time range, truncation flag, relation, hypothesis ID, and metadata. Deduplicate on canonical source tool + arguments + excerpt while preserving first insertion order.

- [ ] **Step 4: Implement deterministic report validation**

  Validate existence, unique citations per claim, and at least one citation for every material claim. Return structured issues with `code`, `path`, and `message`; never infer whether the cited evidence proves the root cause.

- [ ] **Step 5: Run tests**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_evidence.py tests/test_investigation_models.py -q`  
  Expected: all pass.

- [ ] **Step 6: Commit**

  ```bash
  git add src/ailab_ops/evidence tests/test_evidence.py
  git commit -m "feat: add evidence store and citation validation"
  ```

### Task 3: Independent V2 case, knowledge, and evaluation assets

**Files:**
- Create: `data/v2/cases/case-gpu-assert.json`
- Create: `data/v2/cases/case-collective-timeout.json`
- Create: `data/v2/cases/case-insufficient-evidence.json`
- Create: `data/v2/knowledge/runbooks/gpu-assert.md`
- Create: `data/v2/knowledge/runbooks/collective-timeout.md`
- Create: `data/v2/evals/labels.jsonl`
- Create: `src/ailab_ops/cases/loader.py`
- Create: `src/ailab_ops/cases/__init__.py`
- Create: `tests/test_v2_assets.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: no Task 1 types; raw assets remain inspectable teaching material.
- Produces: `CaseWorld`, `load_case(case_id: str, root: Path | None = None) -> CaseWorld`, `list_cases() -> list[str]`, and separate `load_eval_labels(path) -> dict[str, dict]` used only by evaluation code.

- [ ] **Step 1: Write failing asset-boundary tests**

  Test exact three case IDs, deterministic loading, missing-case error text, absence of `root_cause`, `expected_*`, and `ground_truth` keys from recursive case/tool-visible data, and the presence of those labels only in `data/v2/evals/labels.jsonl`.

- [ ] **Step 2: Verify failure**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_v2_assets.py -q`  
  Expected: missing loader/assets.

- [ ] **Step 3: Add three hand-authored cases and two runbooks**

  Cases contain jobs, timestamped logs, metrics, nodes, incidents, and safe simulated actions. Labels are written independently and reference case IDs only. Runbooks explain discriminators without embedding case IDs or evaluation answers.

- [ ] **Step 4: Implement loaders and package data configuration**

  Load from repository `data/v2` in development and expose an explicit root override for tests. Do not import the legacy playbook or `faults.yaml`.

- [ ] **Step 5: Run boundary and legacy leakage tests**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_v2_assets.py tests/test_world.py tests/test_tools.py -q`  
  Expected: all pass.

- [ ] **Step 6: Commit**

  ```bash
  git add data/v2 src/ailab_ops/cases pyproject.toml tests/test_v2_assets.py
  git commit -m "feat: add independent v2 cases and knowledge assets"
  ```

### Task 4: Real-model gateway and honest replay backend

**Files:**
- Create: `src/ailab_ops/models/protocol.py`
- Create: `src/ailab_ops/models/openai.py`
- Create: `src/ailab_ops/models/replay.py`
- Create: `src/ailab_ops/models/__init__.py`
- Create: `data/v2/replays/gpu-assert.jsonl`
- Create: `tests/test_model_backends.py`
- Modify: `src/ailab_ops/config.py`

**Interfaces:**
- Consumes: existing `ChatMessage`, `ToolSpec`, `LLMResponse`, and `ToolCall` while migration is in progress.
- Produces: `ModelGateway.complete(messages, tools, *, max_tokens) -> LLMResponse`, `OpenAIModelGateway`, `ReplayModelGateway`, and `build_model_gateway(settings)`.

- [ ] **Step 1: Write failing backend contract tests**

  Test OpenAI request shape with `httpx.MockTransport`, fragmented tool arguments, connect/read errors translated to typed backend errors, replay of the recorded scenario, and refusal with `ReplayMissError` for an unrecorded conversation.

- [ ] **Step 2: Verify failure**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_model_backends.py -q`  
  Expected: missing `ailab_ops.models`.

- [ ] **Step 3: Implement gateway protocol and OpenAI adapter**

  Adapt the proven parsing/retry behavior from `llm/openai_compat.py`; do not make real network calls in tests. Settings use `AILAB_MODEL_MODE=online|replay`, `AILAB_LLM_BASE_URL`, `AILAB_LLM_API_KEY`, and `AILAB_LLM_MODEL`.

- [ ] **Step 4: Implement strict replay**

  Match a canonical hash of messages and available tool names. Return only recorded responses, expose `mode="replay"`, and raise `ReplayMissError` on any mismatch.

- [ ] **Step 5: Run tests**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_model_backends.py tests/test_agent.py -q`  
  Expected: all pass.

- [ ] **Step 6: Commit**

  ```bash
  git add src/ailab_ops/models src/ailab_ops/config.py data/v2/replays tests/test_model_backends.py
  git commit -m "feat: add online model gateway and strict replay"
  ```

### Task 5: Evidence-aware investigation orchestrator

**Files:**
- Create: `src/ailab_ops/investigation/prompts.py`
- Create: `src/ailab_ops/investigation/orchestrator.py`
- Create: `src/ailab_ops/investigation/parsing.py`
- Create: `tests/test_orchestrator.py`
- Modify: `src/ailab_ops/tools/registry.py`
- Modify: `src/ailab_ops/investigation/__init__.py`

**Interfaces:**
- Consumes: `ModelGateway`, `InvestigationState`, `EvidenceStore`, existing `ToolRegistry` and tool schemas.
- Produces: `InvestigationOrchestrator.run(question, *, case_id, budget) -> InvestigationState`, `advance(state) -> InvestigationState`, and normalized evidence from successful read-only tool results.

- [ ] **Step 1: Write failing orchestrator tests with scripted model responses**

  Add tests for dynamic tool choice, evidence capture, two competing hypotheses, a valid cited report, repair after a tool argument error, duplicate-call detection, unknown citation retry once, and partial stopped state when step/token/deadline budgets expire.

- [ ] **Step 2: Verify failure**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_orchestrator.py -q`  
  Expected: missing orchestrator.

- [ ] **Step 3: Add tool metadata and evidence normalization seam**

  Extend `Tool` with `kind: Literal["read", "action"]`, `sensitivity`, and `idempotent`; preserve `read_only` as a compatibility property until legacy removal. `ToolResult` may carry `evidence_items` without embedding evaluation labels.

- [ ] **Step 4: Implement orchestration and structured parsing**

  The model may request tools or return a typed control object containing plan, hypotheses, report, or proposed action. Persist state after every transition. Retry report generation once on deterministic validation errors; do not silently repair root causes or citations.

- [ ] **Step 5: Run focused and registry tests**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_orchestrator.py tests/test_tools.py tests/test_evidence.py -q`  
  Expected: all pass.

- [ ] **Step 6: Commit**

  ```bash
  git add src/ailab_ops/investigation src/ailab_ops/tools/registry.py tests/test_orchestrator.py
  git commit -m "feat: orchestrate evidence-grounded investigations"
  ```

### Task 6: Policy enforcement, approvals, and simulated actions

**Files:**
- Create: `src/ailab_ops/policy/models.py`
- Create: `src/ailab_ops/policy/engine.py`
- Create: `src/ailab_ops/policy/__init__.py`
- Create: `src/ailab_ops/approvals/store.py`
- Create: `src/ailab_ops/approvals/service.py`
- Create: `src/ailab_ops/approvals/__init__.py`
- Modify: `src/ailab_ops/investigation/orchestrator.py`
- Create: `tests/test_approvals.py`

**Interfaces:**
- Consumes: action proposals from Task 5 and action metadata in `ToolRegistry`.
- Produces: `PolicyEngine.authorize(context, tool, arguments) -> PolicyDecision`, `ApprovalRequest`, `ApprovalService.create`, `approve`, `reject`, `execute`, and append-only `AuditEvent` records.

- [ ] **Step 1: Write failing policy and lifecycle tests**

  Cover automatic read access, action conversion into a pending request, reason/risk/rollback/evidence requirements, explicit approval before execution, rejection, expiry, duplicate execution idempotency, unknown action denial, and audit events for every transition.

- [ ] **Step 2: Verify failure**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_approvals.py -q`  
  Expected: missing packages.

- [ ] **Step 3: Implement policy engine and in-memory approval store**

  Policies are code/config enforced, not prompt enforced. Default expiry is 15 minutes. Only simulated action handlers are registerable in this build.

- [ ] **Step 4: Connect proposed actions to orchestrator terminal state**

  A valid proposal moves the investigation to `awaiting_approval`; approval execution records the simulated result and moves it to `completed`. Denial leaves the diagnosis report intact.

- [ ] **Step 5: Run tests**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_approvals.py tests/test_orchestrator.py -q`  
  Expected: all pass.

- [ ] **Step 6: Commit**

  ```bash
  git add src/ailab_ops/policy src/ailab_ops/approvals src/ailab_ops/investigation tests/test_approvals.py
  git commit -m "feat: gate simulated actions with approvals"
  ```

### Task 7: V2 evaluation and trace records

**Files:**
- Create: `src/ailab_ops/evals/models.py`
- Create: `src/ailab_ops/evals/scoring.py`
- Create: `src/ailab_ops/evals/runner.py`
- Create: `src/ailab_ops/evals/__init__.py`
- Create: `src/ailab_ops/observability/events.py`
- Create: `src/ailab_ops/observability/recorder.py`
- Create: `src/ailab_ops/observability/__init__.py`
- Create: `tests/test_v2_evals.py`
- Create: `tests/test_v2_tracing.py`

**Interfaces:**
- Consumes: `InvestigationState`, hidden labels from Task 3, orchestrator events, usage and latency.
- Produces: `EvaluationResult`, `score_investigation(state, label)`, `run_evaluation(case_ids, repeats, factory)`, `TraceEvent`, and `TraceRecorder`.

- [ ] **Step 1: Write failing scoring and trace tests**

  Assert separate scores for tool choice, required evidence, citation validity, root cause, abstention, policy compliance, latency, and token use. Assert repeated runs report mean and spread. Assert traces contain state changes but redact configured sensitive fields.

- [ ] **Step 2: Verify failure**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_v2_evals.py tests/test_v2_tracing.py -q`  
  Expected: missing packages.

- [ ] **Step 3: Implement typed trace recorder and attach it to Tasks 5–6 events**

  Record timestamps, session, event kind, phase, model/tool metadata, usage, retry, evidence IDs, approval IDs and errors. Store redacted payloads, not secrets or raw API keys.

- [ ] **Step 4: Implement independent scoring and repeated-run aggregation**

  Evaluation code alone may read hidden labels. Missing or invalid outputs score explicitly rather than crashing or failing open.

- [ ] **Step 5: Run tests**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_v2_evals.py tests/test_v2_tracing.py tests/test_v2_assets.py -q`  
  Expected: all pass.

- [ ] **Step 6: Commit**

  ```bash
  git add src/ailab_ops/evals src/ailab_ops/observability src/ailab_ops/investigation src/ailab_ops/approvals tests/test_v2_evals.py tests/test_v2_tracing.py
  git commit -m "feat: add layered agent evaluation and traces"
  ```

### Task 8: Runtime, CLI, and API migration to V2

**Files:**
- Modify: `src/ailab_ops/runtime.py`
- Modify: `src/ailab_ops/cli.py`
- Modify: `src/ailab_ops/serving/app.py`
- Modify: `src/ailab_ops/serving/service.py`
- Modify: `.env.example`
- Modify: `README.md`
- Create: `tests/test_v2_runtime.py`
- Create: `tests/test_v2_api.py`

**Interfaces:**
- Consumes: all prior tasks plus existing gate, limits, cache and FastAPI lifecycle.
- Produces: default V2 runtime, `ailab-ops investigate`, `ailab-ops replay`, `ailab-ops eval-v2`, `POST /v2/investigations`, `GET /v2/investigations/{id}`, approval endpoints, and health metadata exposing `model_mode`.

- [ ] **Step 1: Write failing runtime and API tests**

  Cover replay boot without API key, online boot requiring model configuration, full recorded investigation, replay miss response, typed budget stop, report retrieval, pending approval, approve/reject endpoints, simulated execution, and health showing `online` or `replay` prominently.

- [ ] **Step 2: Verify failure**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_v2_runtime.py tests/test_v2_api.py -q`  
  Expected: missing V2 runtime/routes.

- [ ] **Step 3: Assemble V2 runtime and preserve production guards**

  Reuse the existing concurrency gate, tenant limits and circuit-breaker concepts around model calls. Do not route V2 through `MockLLMClient` or `signals.decide`.

- [ ] **Step 4: Add CLI and API surfaces**

  Responses include mode, phase, evidence, hypotheses, report, stop reason, trace ID and approval state. Replay misses use a typed 422 response; upstream unavailability remains retryable.

- [ ] **Step 5: Rewrite quick start and environment documentation**

  Put online configuration first, replay second, clearly label simulation, and remove claims that the deterministic mock is a genuine model-equivalent reasoner.

- [ ] **Step 6: Run V2 and full regression suites**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_v2_runtime.py tests/test_v2_api.py -q`  
  Expected: all pass.  
  Run: `PYTHONPATH=src python3 -m pytest -q`  
  Expected: all tests pass with no unexpected skips.

- [ ] **Step 7: Run command-line smoke tests**

  Run: `PYTHONPATH=src python3 -m ailab_ops.cli replay --case case-gpu-assert`  
  Expected: completed investigation marked `mode=replay`, with a cited report.  
  Run: `PYTHONPATH=src python3 -m ailab_ops.cli eval-v2 --mode replay`  
  Expected: layered metrics for recorded cases and no network access.

- [ ] **Step 8: Commit**

  ```bash
  git add src/ailab_ops/runtime.py src/ailab_ops/cli.py src/ailab_ops/serving .env.example README.md tests/test_v2_runtime.py tests/test_v2_api.py
  git commit -m "feat: make v2 investigation the default runtime"
  ```

### Task 9: Remove obsolete default-path claims and quarantine legacy code

**Files:**
- Modify: `src/ailab_ops/llm/registry.py`
- Modify: `src/ailab_ops/llm/__init__.py`
- Modify: `src/ailab_ops/__init__.py`
- Modify: `Makefile`
- Create: `docs/legacy-v1.md`
- Modify or delete only after import scan: `src/ailab_ops/llm/mock.py`
- Modify or relocate only after import scan: `src/ailab_ops/signals.py`
- Modify: legacy tests whose assertions describe the V1 default rather than preserved behavior

**Interfaces:**
- Consumes: the complete V2 runtime from Task 8.
- Produces: one unambiguous default path and documented access to the tagged Git history for V1.

- [ ] **Step 1: Add migration tests**

  Assert default runtime never constructs `MockLLMClient`, V2 never imports `signals.decide`, replay is the only no-key inference path, and public CLI help describes online/replay modes accurately.

- [ ] **Step 2: Run tests and verify they expose remaining V1 coupling**

  Run: `PYTHONPATH=src python3 -m pytest tests/test_v2_runtime.py -q`  
  Expected: new migration assertions fail before cleanup.

- [ ] **Step 3: Search imports and quarantine or remove obsolete code**

  Use `grep -R` because this host may not provide `rg`. Delete only modules with no preserved consumer; otherwise move V1-only imports behind explicit legacy commands. Document the last V1 commit and the architectural reason for removal.

- [ ] **Step 4: Update Make targets**

  `make demo`, `make serve`, `make eval`, and `make test` target V2. If a temporary `legacy-demo` target remains, label it unsupported and remove it before the course release.

- [ ] **Step 5: Run full verification**

  Run: `PYTHONPATH=src python3 -m pytest -q`  
  Expected: all tests pass.  
  Run: `git grep -n "MockLLMClient\|signals.decide" -- ':!docs/legacy-v1.md' ':!docs/superpowers/*'`  
  Expected: no V2/default-path reference.

- [ ] **Step 6: Commit**

  ```bash
  git add -A src tests Makefile docs/legacy-v1.md
  git commit -m "refactor: retire the v1 rule-decided default path"
  ```

## Follow-on Plans

After this plan is complete and verified, create two separate implementation plans:

1. **V2 investigation UI:** evidence board, hypothesis changes, timeline, report, approval workflow, replay banner and model-mode health.
2. **Six-course textbook:** book-style PDF engine, diagrams, verified excerpts and experiments, six chapters, combined volume, instructor reading notes and visual QA.

These depend on the stable interfaces and reproducible outputs produced here; implementing them earlier would force the UI and textbook to document moving targets.
