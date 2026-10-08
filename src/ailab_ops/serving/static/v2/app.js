"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const storageKey = "ailab-ops-v2-context";
  const state = { tenantId: "tenant-01", sessionId: "", detail: null, modelMode: "unknown", kind: "empty" };
  let activeController = null;
  let generation = 0;

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
    generation += 1;
    state.tenantId = tenantId;
    state.sessionId = "";
    byId("session-id").value = "";
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
    byId("start-button").disabled = kind === "loading" || !["replay", "online"].includes(state.modelMode);
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
    board.replaceChildren();
    if (!approvals.length) return empty(board, "暂无行动提案。所有行动均为模拟。");
    for (const approval of approvals) {
      const article = node("article", null, "approval");
      article.append(node("h4", approval.tool || "行动提案"), node("p", `${approval.request_id} · ${approval.status}`, "source-line"));
      for (const [label, field] of [["理由", "reason"], ["风险", "risk"], ["回滚", "rollback"], ["到期", "expires_at"]]) {
        article.append(node("p", `${label} / ${approval[field] || "未给出"}`));
      }
      article.append(citations(approval.evidence_ids, ids), details("行动参数", approval.arguments));
      board.append(article);
    }
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
    document.dispatchEvent(new CustomEvent("ailab:investigation", { detail }));
  }

  function begin(message) {
    if (activeController) activeController.abort();
    activeController = new AbortController();
    generation += 1;
    status("loading", message);
    return { signal: activeController.signal, generation };
  }

  function showError(error) {
    const messages = {
      replay_miss: "回放未命中：这个请求尚未录制。请使用内置案例并留空问题，或由服务配置切换在线模式。",
      not_found: "找不到案例或会话：请确认标识、租户与服务进程。",
      validation_error: "请求格式未通过校验。请检查案例标识与输入长度。",
      invalid_response: "服务响应不是有效 JSON。请检查服务连接后重试。",
      network_error: "无法连接服务。请检查连接后重试。"
    };
    const kind = error.kind || "network_error";
    const retry = error.payload?.error?.retryable ? " 服务允许稍后重试。" : "";
    status(kind === "replay_miss" ? "replay_miss" : "error", (messages[kind] || `调查失败 / ${kind}。`) + retry);
  }

  async function loadSession(sessionId) {
    if (!confirmTenant() || !sessionId) return;
    const operation = begin("正在恢复调查与引用证据…");
    render(null);
    try {
      const detail = await request(sessionPath(sessionId), { signal: operation.signal });
      if (operation.generation !== generation) return;
      render(detail);
      if (detail.error) showError(new ApiError(detail.error.kind, 200, detail));
      else status("ready", detail.stop_reason ? `调查已停止 / ${detail.stop_reason}` : "调查已恢复。可核查下方证据与引用。");
    } catch (error) {
      if (error.name === "AbortError" || operation.generation !== generation) return;
      showError(error);
    }
  }

  byId("intake-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!confirmTenant() || !["replay", "online"].includes(state.modelMode)) return;
    const body = { tenant_id: state.tenantId, case_id: state.modelMode === "online" ? byId("case-input").value.trim() : byId("case-select").value };
    if (byId("question").value.trim()) body.question = byId("question").value.trim();
    const operation = begin("正在调查：收集证据、验证假设与整理报告…");
    state.sessionId = "";
    persistContext();
    render(null);
    try {
      const detail = await request("/v2/investigations", { method: "POST", body: JSON.stringify(body), signal: operation.signal });
      if (operation.generation !== generation) return;
      render(detail);
      status("ready", detail.stop_reason ? `调查已停止 / ${detail.stop_reason}` : "调查已返回。请核查证据、未知项与建议。");
    } catch (error) {
      if (error.name === "AbortError" || operation.generation !== generation) return;
      if (error.payload?.session_id) render(error.payload);
      showError(error);
    }
  });

  byId("restore-form").addEventListener("submit", (event) => {
    event.preventDefault();
    return loadSession(byId("session-id").value.trim());
  });

  byId("tenant-id").addEventListener("change", confirmTenant);

  // The action workflow can subscribe to ailab:investigation and use this
  // presentation boundary without duplicating session ownership or DOM sinks.
  window.AILabWorkspace = { state, request, sessionPath, loadSession, render, status, node, citations, ApiError, confirmTenant };

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
