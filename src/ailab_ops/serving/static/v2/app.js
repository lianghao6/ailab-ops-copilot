"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const storageKey = "ailab-ops-v2-context";
  const state = { tenantId: "tenant-01", sessionId: "", detail: null, modelMode: "unknown", kind: "empty", busy: false, writing: false, syncRequired: false };
  let activeController = null;
  let generation = 0;
  let pollTimer = null;
  let pollPaused = false;

  class ApiError extends Error {
    constructor(kind, status, payload) {
      super(kind);
      this.kind = kind;
      this.status = status;
      this.payload = payload;
    }
  }

  function node(tag, text, className) {
    const result = document.createElement(tag);
    if (text !== undefined && text !== null) result.textContent = String(text);
    if (className) result.className = className;
    return result;
  }

  function list(value) { return Array.isArray(value) ? value : []; }
  function json(value) { return JSON.stringify(value ?? {}, null, 2); }
  function percent(value) { return Number.isFinite(value) ? `${Math.round(value * 100)}%` : "未给出"; }

  function normalizeTenant(value) {
    const tenantId = typeof value === "string" ? value.trim() : "";
    return tenantId.length > 0 && tenantId.length <= 100 ? tenantId : "";
  }

  function confirmTenant() {
    const input = byId("tenant-id");
    const tenantId = normalizeTenant(input.value);
    input.setAttribute("aria-invalid", String(!tenantId));
    if (tenantId) input.value = tenantId;
    if (tenantId && tenantId === state.tenantId) return tenantId;
    if (activeController) activeController.abort();
    stopPolling();
    generation += 1;
    state.busy = false;
    state.writing = false;
    state.syncRequired = false;
    pollPaused = false;
    state.tenantId = tenantId;
    state.sessionId = "";
    byId("session-id").value = "";
    for (const id of ["action-actor", "proposal-tool", "proposal-arguments", "proposal-evidence", "proposal-reason", "proposal-risk", "proposal-rollback"]) byId(id).value = "";
    persistContext();
    render(null);
    banner(state.modelMode);
    status(tenantId ? "empty" : "error", tenantId
      ? "已切换租户。选择案例或恢复当前租户的会话。"
      : "租户标识不能为空白，且不得超过 100 个字符。请填写有效租户后重试。");
    return tenantId;
  }

  function persistContext() {
    try {
      localStorage.setItem(storageKey, JSON.stringify({ tenantId: state.tenantId, sessionId: state.sessionId }));
    } catch (_) { /* The workspace also works when browser storage is unavailable. */ }
  }

  function readContext() {
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || "null");
      if (saved && normalizeTenant(saved.tenantId)) {
        state.tenantId = normalizeTenant(saved.tenantId);
        if (typeof saved.sessionId === "string" && saved.sessionId.length <= 200) state.sessionId = saved.sessionId;
      }
    } catch (_) { /* A malformed saved context must not prevent intake. */ }
    byId("tenant-id").value = state.tenantId;
    byId("session-id").value = state.sessionId;
  }

  function sessionPath(sessionId, suffix = "") {
    const query = new URLSearchParams({ tenant_id: state.tenantId });
    return `/v2/investigations/${encodeURIComponent(sessionId)}${suffix}?${query.toString()}`;
  }

  async function request(path, options = {}) {
    const response = await fetch(path, { ...options, credentials: "same-origin", headers: {
      Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers
    } });
    let payload;
    try { payload = await response.json(); }
    catch (_) { throw new ApiError("invalid_response", response.status, null); }
    if (!response.ok) {
      const kind = payload.error?.kind || (response.status === 404 ? "not_found" : response.status === 422 ? "validation_error" : "http_error");
      throw new ApiError(kind, response.status, payload);
    }
    return payload;
  }

  function status(kind, message) {
    state.kind = kind;
    byId("workspace-status").dataset.state = kind;
    byId("workspace-status").textContent = message;
    byId("investigation").setAttribute("aria-busy", String(kind === "loading"));
    updateControls();
  }

  function updateControls() {
    byId("start-button").disabled = state.busy || !["replay", "online"].includes(state.modelMode);
    byId("restore-button").disabled = state.writing;
    byId("refresh-button").disabled = state.busy || !state.sessionId;
    byId("cancel-button").hidden = !state.busy;
    byId("action-controls").hidden = !state.detail;
    byId("proposal-form").hidden = state.detail?.phase !== "completed" || !state.detail?.report;
    byId("proposal-fields").disabled = state.busy || state.syncRequired;
    byId("proposal-button").disabled = state.busy || state.syncRequired;
    function walk(element) {
      if (element.dataset.action) element.disabled = state.busy || state.syncRequired || element.dataset.unavailable === "true";
      for (const child of element.children) walk(child);
    }
    walk(byId("approval-list"));
  }

  function banner(mode) {
    const supported = ["replay", "online"].includes(mode);
    byId("mode-banner").dataset.mode = supported ? mode : "unknown";
    byId("mode-label").textContent = mode === "replay" ? "回放模式 / REPLAY" : mode === "online" ? "在线模式 / ONLINE" : "运行模式未确认";
    byId("mode-copy").textContent = mode === "replay"
      ? "本地已录制请求 · 不调用在线模型 · 所有行动均为模拟。"
      : mode === "online" ? "使用服务配置的在线模型 · 判断以当前会话证据为依据 · 所有行动均为模拟。"
        : "暂时无法核验服务模式。恢复会话后可查看该会话的模式。";
  }

  function configureIntake(mode) {
    state.modelMode = mode;
    byId("replay-case-field").hidden = mode === "online";
    byId("online-case-field").hidden = mode !== "online";
    byId("case-input").required = mode === "online";
    byId("question-note").textContent = mode === "online"
      ? "留空使用案例默认问题。可用案例 / 作业由服务端配置和校验。"
      : "回放问题必须与已录制请求一致；未录制的请求会显示 replay miss。";
    byId("start-button").disabled = state.kind === "loading" || !["replay", "online"].includes(mode);
  }

  function empty(container, message) { container.replaceChildren(node("p", message, "empty")); }

  function citations(ids, available) {
    const group = node("span", null, "citation-list");
    for (const id of list(ids)) {
      const position = available.indexOf(id);
      if (position < 0) {
        group.append(node("span", `${id}（证据不可用）`, "citation-unavailable"));
      } else {
        const link = node("a", `[${id}]`, "citation");
        link.href = `#evidence-${position}`;
        link.setAttribute("aria-label", `查看证据 ${id}`);
        group.append(link);
      }
    }
    return group;
  }

  function details(label, value) {
    const disclosure = node("details");
    disclosure.append(node("summary", label), node("pre", json(value)));
    return disclosure;
  }

  function renderEvidence(evidence) {
    const board = byId("evidence-list");
    board.replaceChildren();
    byId("evidence-count").textContent = String(evidence.length);
    if (!evidence.length) return empty(board, "暂无证据。未完成的调查可能未能获取观察。");
    evidence.forEach((item, index) => {
      const article = node("article", null, "evidence-card");
      article.id = `evidence-${index}`;
      article.tabIndex = -1;
      const mark = node("div", null, "evidence-mark");
      mark.append(node("span", item.evidence_id), node("span", item.relation || "related", "badge"));
      const body = node("div");
      body.append(node("h4", item.summary || "原始观察"), node("p", `来源 / ${item.source_tool || "未给出"}`, "source-line"));
      const range = list(item.observed_time_range);
      body.append(node("p", range.length ? `观察时间 / ${range.join(" → ")}` : "观察时间 / 未提供", "source-line"));
      if (item.excerpt) body.append(node("pre", item.excerpt));
      if (item.truncated) body.append(node("p", "来源片段已截断。", "field-note"));
      body.append(details("来源参数与元信息", { arguments: item.arguments, metadata: item.metadata }));
      article.append(mark, body);
      board.append(article);
    });
  }

  function renderHypotheses(hypotheses, ids) {
    const board = byId("hypothesis-list");
    board.replaceChildren();
    if (!hypotheses.length) return empty(board, "暂无假设。支持与反证会并列呈现。");
    for (const item of hypotheses) {
      const article = node("article", null, "hypothesis");
      article.append(node("h4", item.title), node("p", `${item.hypothesis_id} · 置信度 ${percent(item.confidence)}`, "source-line"));
      for (const [label, refs] of [["支持", item.supporting_evidence_ids], ["反证", item.contradicting_evidence_ids]]) {
        const line = node("p", `${label} / ${list(refs).length ? "" : "尚无引用"}`);
        line.append(citations(refs, ids));
        article.append(line);
      }
      board.append(article);
    }
  }

  function renderReport(report, ids) {
    const board = byId("report-content");
    board.replaceChildren();
    if (!report) return empty(board, "尚未形成报告。停止原因与已取得证据仍会保留。");
    board.append(node("p", `${report.root_cause} · 置信度 ${percent(report.confidence)}`, "report-finding"), node("p", report.summary, "report-summary"));
    const claims = node("ol", null, "claims");
    for (const claim of list(report.claims)) {
      const item = node("li", claim.text);
      item.append(citations(claim.evidence_ids, ids));
      claims.append(item);
    }
    board.append(claims);
    for (const [title, items] of [["已排除", report.ruled_out], ["仍然未知", report.unknowns], ["建议", report.recommendations]]) {
      const section = node("section", null, "report-subsection");
      section.append(node("h4", title));
      const bullets = node("ul");
      for (const value of list(items)) bullets.append(node("li", value));
      if (list(items).length) section.append(bullets);
      else section.append(node("p", "暂无记录。", "empty"));
      board.append(section);
    }
  }

  function renderTimeline(timeline) {
    const board = byId("timeline-list");
    board.replaceChildren();
    const events = list(timeline?.events);
    byId("timeline-summary").textContent = events.length
      ? `${timeline.trace_id || ""} · ${events.length} / ${timeline.total_events} 条事件${timeline.truncated ? " · 仅展示最近事件" : ""}`
      : "暂无事件。";
    for (const event of events) {
      const item = node("li");
      const time = node("time", event.timestamp || "时间未提供");
      if (event.timestamp) time.dateTime = event.timestamp;
      item.append(time, node("p", `${event.kind || ""} / ${event.event || ""}`));
      if (event.payload && Object.keys(event.payload).length) item.append(details("事件数据", event.payload));
      board.append(item);
    }
  }

  function renderApprovals(approvals, ids) {
    const board = byId("approval-list");
    const snapshots = approvals.map((approval) => json(approval));
    if (approvals.length && board.children.length === approvals.length
        && snapshots.every((snapshot, index) => board.children[index].dataset.snapshot === snapshot)) {
      // Keep the actual input nodes during polling so keyboard focus and draft
      // rejection reasons survive. Only the server may change request status.
      function expiryControls(element, unavailable, noteId) {
        if (element.dataset.action) {
          element.dataset.unavailable = String(unavailable);
          if (unavailable) element.setAttribute("aria-describedby", noteId);
          else element.removeAttribute("aria-describedby");
        }
        if (element.dataset.expiryNote) element.hidden = !unavailable;
        for (const child of element.children) expiryControls(child, unavailable, noteId);
      }
      approvals.forEach((approval, index) => {
        const deadline = Date.parse(approval.expires_at);
        expiryControls(board.children[index], !Number.isFinite(deadline) || Date.now() >= deadline, `expiry-${approval.request_id}`);
      });
      return;
    }
    const previous = new Map();
    for (const article of board.children) {
      if (!article.dataset.snapshot) continue;
      const values = {};
      function remember(element) {
        if (element.dataset.confirm) values[element.dataset.confirm] = element.checked;
        if (element.dataset.reason) values.reason = element.value;
        for (const child of element.children) remember(child);
      }
      remember(article);
      previous.set(article.dataset.snapshot, values);
    }
    board.replaceChildren();
    if (!approvals.length) return empty(board, "暂无行动提案。所有行动均为模拟。");
    for (const approval of approvals) {
      const article = node("article", null, "approval");
      article.dataset.snapshot = json(approval);
      const review = previous.get(article.dataset.snapshot) || {};
      article.append(node("h4", approval.tool || "行动提案"), node("p", `${approval.request_id} · ${approval.status}`, "source-line"));
      for (const [label, field] of [["理由", "reason"], ["风险", "risk"], ["回滚", "rollback"], ["到期", "expires_at"]]) {
        article.append(node("p", `${label} / ${approval[field] || "未给出"}`));
      }
      const parameters = details("行动参数", approval.arguments);
      parameters.open = true;
      article.append(citations(approval.evidence_ids, ids), parameters);
      if (approval.approved_by) article.append(node("p", `批准人 / ${approval.approved_by}`));
      if (approval.rejection_reason) article.append(node("p", `拒绝原因 / ${approval.rejection_reason}`));
      if (approval.result) article.append(details("模拟执行结果 / SIMULATION", approval.result));
      const deadline = Date.parse(approval.expires_at);
      const unavailable = !Number.isFinite(deadline) || Date.now() >= deadline;
      if (["pending", "approved"].includes(approval.status)) {
        const expiryNote = node("p", "到期时间已过或无法核验。请刷新服务端状态；当前显示保留服务端最后确认的状态。", "field-note");
        expiryNote.id = `expiry-${approval.request_id}`;
        expiryNote.dataset.expiryNote = "true";
        expiryNote.hidden = !unavailable;
        expiryNote.setAttribute("role", "status");
        expiryNote.setAttribute("aria-live", "polite");
        article.append(expiryNote);
        const controls = node("div", null, "decision-controls");
        const decision = approval.status === "pending" ? "approve" : "execute";
        const checkbox = node("input");
        checkbox.type = "checkbox";
        checkbox.dataset.confirm = decision;
        checkbox.checked = Boolean(review[decision]);
        const label = node("label");
        label.append(checkbox, node("span", decision === "approve"
          ? "我已核查工具、参数、证据、理由、风险、回滚及有效期，确认批准此提案。"
          : "我确认单独执行已批准的行动（仅模拟）。"));
        const button = node("button", decision === "approve" ? "批准提案" : "执行模拟");
        button.type = "button";
        button.dataset.action = decision;
        button.dataset.unavailable = String(unavailable);
        if (unavailable) button.setAttribute("aria-describedby", expiryNote.id);
        button.addEventListener("click", () => decideApproval(approval.request_id, decision, { confirmed: checkbox.checked }));
        controls.append(label, button);
        if (approval.status === "pending") {
          const reason = node("textarea");
          reason.dataset.reason = "reject";
          reason.value = review.reason || "";
          reason.id = `reject-${approval.request_id}`;
          const reasonLabel = node("label", "拒绝原因（必填）");
          reasonLabel.setAttribute("for", reason.id);
          const reject = node("button", "拒绝提案");
          reject.type = "button";
          reject.dataset.action = "reject";
          reject.dataset.unavailable = String(unavailable);
          if (unavailable) reject.setAttribute("aria-describedby", expiryNote.id);
          reject.addEventListener("click", () => decideApproval(approval.request_id, "reject", { reason: reason.value }));
          controls.append(reasonLabel, reason, reject);
        }
        article.append(controls);
      }
      board.append(article);
    }
  }

  function renderAudit(timeline) {
    const board = byId("approval-audit");
    board.replaceChildren();
    const events = list(timeline?.events).filter((event) => event.kind === "approval");
    for (const event of events) {
      const item = node("li");
      item.append(node("time", event.timestamp || "时间未提供"), node("p", `${event.event} / ${event.payload?.actor || "未给出"} / ${event.approval_id || ""}`));
      item.append(details("审计数据", event.payload));
      board.append(item);
    }
    byId("audit-note").textContent = timeline?.truncated
      ? "服务端审计窗口已截断，当前显示最近事件；更早事件未包含在响应中。"
      : events.length ? `已展示服务端返回的 ${events.length} 条审批审计，按发生顺序排列。` : "暂无审批审计。";
  }

  function render(detail) {
    state.detail = detail;
    if (detail) {
      state.sessionId = detail.session_id;
      byId("session-id").value = state.sessionId;
      persistContext();
      banner(detail.mode);
    }
    byId("case-reference").textContent = detail?.case_id || "尚未建立";
    byId("investigation-title").textContent = detail?.case_id ? `调查 / ${detail.case_id}` : "把线索写成证据。";
    byId("investigation-question").textContent = detail?.question || "调查计划、原始观察与引用将在这里展开。";
    byId("session-reference").textContent = detail?.session_id || "无当前会话";
    const budget = detail?.budget;
    byId("budget-summary").textContent = budget ? `步骤 ${budget.steps_used}/${budget.max_steps} · tokens ${budget.tokens_used}/${budget.max_tokens}` : "—";
    for (const item of byId("phase-rail").children) {
      if (item.dataset.phase === detail?.phase) item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
    }
    const plan = byId("plan-list");
    plan.replaceChildren();
    for (const step of list(detail?.plan)) plan.append(node("li", step));
    if (!plan.children.length) plan.append(node("li", "暂无调查计划。", "empty"));
    const evidence = list(detail?.evidence);
    const ids = evidence.map((item) => item.evidence_id);
    renderEvidence(evidence);
    renderHypotheses(list(detail?.hypotheses), ids);
    renderReport(detail?.report, ids);
    renderTimeline(detail?.timeline);
    renderApprovals(list(detail?.approvals), ids);
    renderAudit(detail?.timeline);
    updateControls();
    document.dispatchEvent(new CustomEvent("ailab:investigation", { detail }));
  }

  function begin(message, writing = false) {
    if (activeController) activeController.abort();
    stopPolling();
    activeController = new AbortController();
    generation += 1;
    state.busy = true;
    state.writing = writing;
    status("loading", message);
    return { signal: activeController.signal, generation };
  }

  function stopPolling() {
    if (pollTimer !== null) clearTimeout(pollTimer);
    pollTimer = null;
  }

  function schedulePoll() {
    stopPolling();
    if (pollPaused || state.busy || !state.sessionId || !state.detail || ["completed", "stopped"].includes(state.detail.phase)) return;
    const owner = generation;
    pollTimer = setTimeout(async () => {
      pollTimer = null;
      if (owner === generation && !state.busy) await refreshSession();
    }, 2500);
  }

  function finish(operation) {
    if (operation.generation !== generation) return;
    activeController = null;
    state.busy = false;
    state.writing = false;
    updateControls();
    schedulePoll();
  }

  function cancelRequest() {
    if (!state.busy) return;
    if (activeController) activeController.abort();
    stopPolling();
    generation += 1;
    if (state.writing && state.detail) state.syncRequired = true;
    activeController = null;
    state.busy = false;
    state.writing = false;
    status("cancelled", "已取消本次浏览器等待；服务端是否停止或完成操作尚未确认。已有会话请刷新核验。");
  }

  function showError(error) {
    const messages = {
      replay_miss: "回放未命中：这个请求尚未录制。请使用内置案例并留空问题，或由服务配置切换在线模式。",
      not_found: "找不到案例或会话：请确认标识、租户与服务进程。",
      validation_error: "请求格式未通过校验。请检查案例标识与输入长度。",
      invalid_response: "服务响应不是有效 JSON。请检查服务连接后重试。",
      network_error: "无法连接服务。请检查连接后重试。",
      approval_conflict: "审批操作与当前策略或状态冲突。请刷新并核查提案。",
      queue_full: "服务队列已满。",
      queue_timeout: "等待服务队列超时。",
      circuit_open: "模型服务暂时不可用。",
      upstream: "上游服务暂时不可用。"
    };
    const kind = error.kind || "network_error";
    pollPaused = !error.payload?.error?.retryable && !["network_error", "invalid_response"].includes(kind);
    const seconds = error.payload?.error?.retry_after_s;
    const retry = error.payload?.error?.retryable ? ` 服务允许${Number.isFinite(seconds) ? ` ${seconds} 秒后` : "稍后"}重试。` : "";
    const sync = state.syncRequired ? " 操作结果尚未核验，请刷新服务端状态后继续。" : "";
    status(kind === "replay_miss" ? "replay_miss" : "error", (messages[kind] || `调查失败 / ${kind}。`) + retry + sync);
  }

  async function readSession(sessionId, operation) {
    const detail = await request(sessionPath(sessionId), { signal: operation.signal });
    if (operation.generation !== generation) return detail;
    // Presentation resources cap timeline windows at 500. Ask for the largest
    // window for audit history and report truncation rather than inventing it.
    if (list(detail.approvals).length) {
      const timeline = await request(`${sessionPath(sessionId, "/timeline")}&limit=500`, { signal: operation.signal });
      detail.timeline = timeline;
    }
    return detail;
  }

  function acceptSession(detail) {
    state.syncRequired = false;
    pollPaused = false;
    render(detail);
    if (detail.error) showError(new ApiError(detail.error.kind, 200, detail));
    else status("ready", detail.stop_reason ? `调查已停止 / ${detail.stop_reason}`
      : ["completed", "stopped"].includes(detail.phase) ? "调查已返回。请核查证据、未知项与建议。"
        : `当前阶段 / ${detail.phase}。服务端状态将自动刷新。`);
  }

  async function loadSession(sessionId) {
    if (!confirmTenant() || !sessionId || state.writing) return;
    const operation = begin("正在恢复调查与引用证据…");
    try {
      const detail = await readSession(sessionId, operation);
      if (operation.generation !== generation) return;
      acceptSession(detail);
    } catch (error) {
      if (error.name === "AbortError" || operation.generation !== generation) return;
      showError(error);
    } finally { finish(operation); }
  }

  async function refreshSession() {
    if (!confirmTenant() || state.busy || !state.sessionId) return;
    return loadSession(state.sessionId);
  }

  byId("intake-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!confirmTenant() || state.busy || !["replay", "online"].includes(state.modelMode)) return;
    const body = { tenant_id: state.tenantId, case_id: state.modelMode === "online" ? byId("case-input").value.trim() : byId("case-select").value };
    if (byId("question").value.trim()) body.question = byId("question").value.trim();
    const operation = begin("正在调查：收集证据、验证假设与整理报告…", true);
    try {
      let detail = await request("/v2/investigations", { method: "POST", body: JSON.stringify(body), signal: operation.signal });
      if (operation.generation !== generation) return;
      if (list(detail.approvals).length) {
        // Retain the confirmed create response even if the expanded audit read
        // fails; it includes the session ID needed for a manual retry.
        render(detail);
        detail = await readSession(detail.session_id, operation);
        if (operation.generation !== generation) return;
      }
      acceptSession(detail);
    } catch (error) {
      if (error.name === "AbortError" || operation.generation !== generation) return;
      if (error.payload?.session_id) render(error.payload);
      showError(error);
    } finally { finish(operation); }
  });

  async function mutate(path, body) {
    const sessionId = state.sessionId;
    const operation = begin("正在提交操作并刷新服务端审批与审计…", true);
    // A network failure may arrive after a committed write. Until a complete
    // reread, keep the old snapshot and prevent decisions based on it.
    state.syncRequired = true;
    try {
      await request(path, { method: "POST", body: JSON.stringify(body), signal: operation.signal });
      if (operation.generation !== generation) return;
      const detail = await readSession(sessionId, operation);
      if (operation.generation !== generation) return;
      acceptSession(detail);
    } catch (error) {
      if (error.name === "AbortError" || operation.generation !== generation) return;
      showError(error);
    } finally { finish(operation); }
  }

  async function proposeAction() {
    if (!confirmTenant() || state.busy || state.syncRequired || !state.detail) return;
    if (state.detail.phase !== "completed" || !state.detail.report) return status("error", "需要已完成且有引用的调查报告才能提出行动。");
    const proposal = {};
    for (const field of ["tool", "reason", "risk", "rollback"]) proposal[field] = byId(`proposal-${field}`).value.trim();
    try { proposal.arguments = JSON.parse(byId("proposal-arguments").value); }
    catch (_) { return status("error", "行动参数必须是有效 JSON 对象。"); }
    if (!proposal.arguments || Array.isArray(proposal.arguments) || typeof proposal.arguments !== "object") return status("error", "行动参数必须是 JSON 对象。");
    proposal.evidence_ids = [...new Set(byId("proposal-evidence").value.split(",").map((id) => id.trim()).filter(Boolean))];
    const knownIds = list(state.detail.evidence).map((item) => item.evidence_id);
    if (["tool", "reason", "risk", "rollback"].some((field) => !proposal[field]) || !proposal.evidence_ids.length || proposal.evidence_ids.some((id) => !knownIds.includes(id))) {
      return status("error", "请完整填写工具、理由、风险、回滚，以及当前会话中可核查的引用证据。");
    }
    return mutate(sessionPath(state.sessionId, "/approvals"), { tenant_id: state.tenantId, proposal });
  }

  async function decideApproval(approvalId, decision, { confirmed = false, reason = "" } = {}) {
    if (!confirmTenant() || state.busy || state.syncRequired || !state.detail) return;
    const approval = list(state.detail.approvals).find((item) => item.request_id === approvalId);
    if (!["approve", "reject", "execute"].includes(decision) || !approval) return;
    const expected = decision === "execute" ? "approved" : "pending";
    const expiry = Date.parse(approval.expires_at);
    if (approval.status !== expected || !Number.isFinite(expiry) || Date.now() >= expiry) return status("error", "当前审批状态或有效期不允许该操作。请刷新核验。");
    const actor = byId("action-actor").value.trim();
    if (!actor || actor.length > 100) return status("error", "请填写明确的审阅 / 执行人（1–100 个字符）。");
    reason = reason.trim();
    if (decision === "reject" ? !reason : !confirmed) return status("error", decision === "reject" ? "拒绝提案必须填写原因。" : "请先勾选明确确认，再进行批准或模拟执行。");
    return mutate(`/v2/approvals/${encodeURIComponent(approvalId)}/${decision}`, { tenant_id: state.tenantId, actor, reason });
  }

  byId("proposal-form").addEventListener("submit", (event) => { event.preventDefault(); return proposeAction(); });
  byId("refresh-button").addEventListener("click", refreshSession);
  byId("cancel-button").addEventListener("click", cancelRequest);

  byId("restore-form").addEventListener("submit", (event) => {
    event.preventDefault();
    return loadSession(byId("session-id").value.trim());
  });

  byId("tenant-id").addEventListener("change", confirmTenant);

  // The action workflow can subscribe to ailab:investigation and use this
  // presentation boundary without duplicating session ownership or DOM sinks.
  window.AILabWorkspace = { state, request, sessionPath, loadSession, refreshSession, proposeAction, decideApproval, cancelRequest, render, status, node, citations, ApiError, confirmTenant };

  async function boot() {
    readContext();
    try {
      const health = await request("/v2/health");
      configureIntake(health.model_mode);
      banner(health.model_mode);
    } catch (_) {
      configureIntake("unknown");
      banner("unknown");
      status("error", "无法核验服务模式。请检查连接；仍可尝试恢复已有会话。");
    }
    if (state.sessionId) await loadSession(state.sessionId);
  }
  boot();
})();
