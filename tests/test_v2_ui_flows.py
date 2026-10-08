"""Run the shipped browser script with deterministic DOM, transport and clock."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from ailab_ops.runtime import build_runtime
from ailab_ops.serving.app import create_app
from test_v2_api import create, proposal
from test_v2_runtime import replay_settings
from test_v2_ui import Document


HARNESS = r'''
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {}; this.value = ""; this.text = ""; this.handlers = {}; this.disabled = false; this.checked = false; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(x => x.textContent).join(""); }
  set innerHTML(_) { throw Error("unsafe HTML sink"); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.text = ""; this.children = children; }
  setAttribute(key, value) { this.attrs[key] = value; }
  removeAttribute(key) { delete this.attrs[key]; }
  addEventListener(key, callback) { this.handlers[key] = callback; }
}
const elements = {};
for (const [tag, attrs] of input.elements) {
  if (!attrs.id) continue;
  const e = elements[attrs.id] = new Element(tag);
  e.value = attrs.value || ""; e.hidden = "hidden" in attrs;
}
elements["case-select"].value = "case-gpu-assert";
elements["phase-rail"].children = ["intake", "investigating", "validating", "awaiting_approval", "completed", "stopped"].map(phase => {
  const e = new Element("li"); e.dataset.phase = phase; return e;
});
const find = (root, predicate) => { if (predicate(root)) return root; for (const child of root.children) { const result = find(child, predicate); if (result) return result; } };
const action = name => find(elements["approval-list"], e => e.dataset.action === name);
const tick = () => new Promise(setImmediate);
const reply = (value, status=200) => ({ok: status < 400, status, json: async () => value});
let saved;
const calls = [];
let transport = async path => reply(path === "/v2/health" ? {model_mode: input.scenario === "online" ? "online" : "replay"}
  : path.includes("timeline") ? input.approved.timeline : input.approved);
global.fetch = async (path, options={}) => { calls.push({path, options}); return transport(path, options); };
global.document = {getElementById: id => elements[id], createElement: tag => new Element(tag), dispatchEvent() {}};
global.window = {};
global.CustomEvent = class { constructor(type, options) { this.type = type; this.detail = options.detail; } };
global.localStorage = {getItem: () => input.scenario === "reload" ? JSON.stringify({tenantId:"tenant-01",sessionId:input.approved.session_id}) : null, setItem: (_, value) => { saved = JSON.parse(value); }};
let timers = new Map(); let timerId = 0;
global.setTimeout = fn => { timers.set(++timerId, fn); return timerId; };
global.clearTimeout = id => timers.delete(id);
Date.now = () => Date.parse(input.pending.approvals[0].created_at) + 1000;
vm.runInThisContext(fs.readFileSync(input.script, "utf8"));
const watchdog = require("timers").setTimeout(() => { process.stderr.write("Flow did not settle; unresolved browser operation\n"); process.exit(1); }, 2000);
(async () => {
  await tick();
  const ws = window.AILabWorkspace;
  assert.strictEqual(typeof ws.refreshSession, "function", "refresh flow must be implemented");
  assert.strictEqual(typeof ws.proposeAction, "function", "proposal flow must be implemented");
  const submit = id => elements[id].handlers.submit({preventDefault() {}});
  const setProposal = () => {
    elements["proposal-tool"].value = "annotate_incident";
    elements["proposal-arguments"].value = '{"case_id":"case-gpu-assert","note":"Review diagnosis"}';
    elements["proposal-reason"].value = "Preserve investigation";
    elements["proposal-risk"].value = "Incorrect annotation";
    elements["proposal-rollback"].value = "Remove note";
    elements["proposal-evidence"].value = input.detail.evidence_ids.join(", ");
  };
  const start = async () => { transport = async () => reply(input.detail); await submit("intake-form"); };
  if (input.scenario === "accessible_states") {
    const snapshots = [null, {...input.detail, phase:"investigating", report:null}, input.detail, input.insufficient, input.pending, input.rejected, input.executed];
    for (const snapshot of snapshots) {
      ws.render(snapshot);
      if (snapshot) {
        const phase = find(elements["phase-rail"], e => e.attrs["aria-current"] === "step");
        assert.strictEqual(phase.dataset.phase, snapshot.phase);
        for (const evidence of snapshot.evidence) assert(elements["evidence-list"].textContent.includes(evidence.excerpt));
        if (snapshot.report) assert(elements["report-content"].textContent.includes(snapshot.report.summary));
      } else {
        assert.strictEqual(elements["action-controls"].hidden, true);
        assert(elements["report-content"].textContent.includes("尚未形成"));
      }
      const all = [];
      const walk = e => { all.push(e); for (const child of e.children) walk(child); };
      for (const id of ["evidence-list", "hypothesis-list", "report-content", "timeline-list", "approval-list", "approval-audit"]) walk(elements[id]);
      for (const e of all) {
        if (e.tag === "details") assert(e.children[0].tag === "summary" && e.children[0].textContent, "disclosures must have native named summaries");
        if (e.tag === "button") assert(e.type === "button" && e.textContent, "decisions must be named native buttons");
        if (e.dataset.confirm) assert(all.some(label => label.tag === "label" && label.children.includes(e) && label.textContent), "checkbox must be wrapped in a named label");
        if (e.dataset.reason) assert(all.some(label => label.tag === "label" && label.attrs.for === e.id && label.textContent), "reason input must have a linked label");
        if (e.className === "citation") assert(all.some(target => target.className === "evidence-card" && "#" + target.id === e.href), "citations must reach displayed evidence");
      }
      if (snapshot === input.insufficient) {
        assert(snapshot.report.unknowns.length > 0);
        for (const unknown of snapshot.report.unknowns) assert(elements["report-content"].textContent.includes(unknown));
      }
      if (snapshot === input.rejected) assert.strictEqual(action("execute"), undefined);
      if (snapshot === input.executed) assert(elements["approval-list"].textContent.includes("模拟执行结果"));
    }
    ws.status("loading", "调查中");
    assert.strictEqual(elements.investigation.attrs["aria-busy"], "true");
    ws.status("ready", "可审阅");
    assert.strictEqual(elements.investigation.attrs["aria-busy"], "false");
  } else if (["replay", "online"].includes(input.scenario)) {
    elements["tenant-id"].value = " tenant&next ";
    elements["question"].value = "  custom question  ";
    elements["case-input"].value = " case-gpu-assert ";
    await start();
    const create = calls.find(c => c.options.method === "POST");
    assert.strictEqual(create.path, "/v2/investigations");
    assert.deepStrictEqual(JSON.parse(create.options.body), {tenant_id:"tenant&next",case_id:"case-gpu-assert",question:"custom question"});
    assert.strictEqual(ws.state.detail.phase, "completed");
    assert.strictEqual(timers.size, 0, "terminal investigation must not poll");
    assert.deepStrictEqual(Object.keys(saved).sort(), ["sessionId", "tenantId"]);
  } else if (input.scenario === "errors") {
    transport = async () => reply({...input.detail, phase:"stopped", error:{kind:"replay_miss",retryable:false}}, 422);
    await submit("intake-form");
    assert.strictEqual(ws.state.kind, "replay_miss");
    assert.strictEqual(ws.state.detail.phase, "stopped");
    transport = async () => reply(input.detail);
    await ws.refreshSession();
    const confirmed = ws.state.detail;
    transport = async () => reply({error:{kind:"queue_full",retryable:true,retry_after_s:3}}, 503);
    await ws.refreshSession();
    assert.strictEqual(ws.state.detail, confirmed, "transient failure must retain confirmed detail");
    assert(elements["workspace-status"].textContent.includes("重试"));
    await ws.loadSession("transient-restore");
    assert.strictEqual(ws.state.detail, confirmed, "failed restore must retain previous confirmed investigation");
    await submit("intake-form");
    assert.strictEqual(ws.state.detail, confirmed, "failed create without a new snapshot must retain previous investigation");
    assert.strictEqual(saved.sessionId,confirmed.session_id);
    transport = async () => reply({...input.detail,phase:"stopped",stop_reason:"budget_exhausted:steps"});
    await ws.refreshSession();
    assert(elements["workspace-status"].textContent.includes("budget_exhausted:steps"));
  } else if (input.scenario === "duplicate_cancel_stale") {
    let resolve;
    transport = () => new Promise(done => { resolve = done; });
    const first = submit("intake-form");
    const second = submit("intake-form");
    assert.strictEqual(calls.filter(c => c.options.method === "POST").length, 1);
    assert.strictEqual(elements["start-button"].disabled, true);
    elements["cancel-button"].handlers.click();
    assert.strictEqual(calls[calls.length-1].options.signal.aborted, true);
    assert(elements["workspace-status"].textContent.includes("未确认"));
    transport = async () => reply({...input.detail,session_id:"new"});
    await ws.loadSession("new");
    resolve(reply({...input.detail,session_id:"old"}));
    await first; await second;
    assert.strictEqual(ws.state.sessionId, "new");
  } else if (input.scenario === "poll") {
    transport = async () => reply({...input.detail,phase:"investigating",report:null});
    await ws.loadSession(input.detail.session_id);
    assert.strictEqual(timers.size, 1);
    transport = async () => reply(input.detail);
    const fn = timers.values().next().value; timers.clear(); await fn(); await tick();
    assert.strictEqual(ws.state.detail.phase, "completed");
    assert.strictEqual(timers.size, 0);
    transport = async () => reply({...input.detail,phase:"investigating",report:null});
    await ws.loadSession(input.detail.session_id);
    assert.strictEqual(timers.size, 1);
    elements["tenant-id"].value = "other"; ws.confirmTenant();
    assert.strictEqual(timers.size, 0, "tenant switch must cancel polling");
    assert.strictEqual(ws.state.detail, null);
  } else if (input.scenario === "proposal") {
    await start(); setProposal(); calls.length=0;
    elements["proposal-arguments"].value = "[]";
    await submit("proposal-form"); assert.strictEqual(calls.length, 0);
    setProposal(); elements["proposal-evidence"].value = "not-in-session";
    await submit("proposal-form"); assert.strictEqual(calls.length, 0);
    setProposal();
    let resolve;
    transport = (path, options) => options.method === "POST" ? new Promise(done => {resolve=done;}) : Promise.resolve(reply(path.includes("timeline") ? input.pending.timeline : input.pending));
    const first = submit("proposal-form"); await submit("proposal-form");
    assert.strictEqual(calls.length, 1);
    assert.strictEqual(calls[0].path,"/v2/investigations/" + input.detail.session_id + "/approvals?tenant_id=tenant-01");
    assert.deepStrictEqual(JSON.parse(calls[0].options.body), {tenant_id:"tenant-01",proposal:input.proposal});
    resolve(reply(input.pending.approvals[0])); await first;
    assert.strictEqual(ws.state.detail.phase, "awaiting_approval");
    const text = elements["approval-list"].textContent;
    for (const value of ["annotate_incident", "Review diagnosis", "Preserve investigation", "Incorrect annotation", "Remove note", input.pending.approvals[0].expires_at]) assert(text.includes(value));
    assert.strictEqual(find(elements["approval-list"], e => e.tag === "details" && e.children[0].textContent === "行动参数").open,true,"arguments must be visible before approval confirmation");
    assert.strictEqual(action("execute"), undefined, "pending request must not expose execution");
  } else if (input.scenario === "decisions") {
    ws.render(input.pending); elements["action-actor"].value = " reviewer "; calls.length=0;
    await action("approve").handlers.click(); assert.strictEqual(calls.length, 0, "approval requires explicit confirmation");
    find(elements["approval-list"], e => e.dataset.confirm === "approve").checked = true;
    transport = async (path, options) => reply(options.method === "POST" ? input.approved.approvals[0] : path.includes("timeline") ? input.approved.timeline : input.approved);
    await action("approve").handlers.click();
    assert.strictEqual(calls.filter(c => c.options.method === "POST").length, 1);
    assert(calls[0].path.endsWith("/approve"));
    assert.deepStrictEqual(JSON.parse(calls[0].options.body), {tenant_id:"tenant-01",actor:"reviewer",reason:""});
    assert.strictEqual(ws.state.detail.approvals[0].status, "approved");
    assert(action("execute"));
    assert(!calls.some(c => c.path.endsWith("/execute")), "approve must never auto-execute");
    find(elements["approval-list"], e => e.dataset.confirm === "execute").checked=true;
    transport = async (path, options) => reply(options.method === "POST" ? {ok:true,simulated:true} : path.includes("timeline") ? input.executed.timeline : input.executed);
    await action("execute").handlers.click();
    assert.strictEqual(ws.state.detail.approvals[0].status, "executed");
    assert(elements["approval-list"].textContent.includes("模拟执行结果"));
    const audit = elements["approval-audit"].textContent;
    for (const event of ["created", "approved", "execution_started", "executed"]) assert(audit.includes(event), event);
    assert.strictEqual(action("execute"), undefined);
  } else if (input.scenario === "reject_expiry_identity") {
    ws.render(input.reject_pending); elements["action-actor"].value = "reviewer"; calls.length=0;
    await action("reject").handlers.click(); assert.strictEqual(calls.length, 0);
    find(elements["approval-list"], e => e.dataset.reason === "reject").value=" Unsafe evidence ";
    transport = async (path, options) => reply(options.method === "POST" ? input.rejected.approvals[0] : path.includes("timeline") ? input.rejected.timeline : input.rejected);
    await action("reject").handlers.click();
    assert.strictEqual(calls[0].path,"/v2/approvals/" + input.reject_pending.approvals[0].request_id + "/reject");
    assert.deepStrictEqual(JSON.parse(calls[0].options.body),{tenant_id:"tenant-01",actor:"reviewer",reason:"Unsafe evidence"});
    assert.strictEqual(ws.state.detail.approvals[0].status,"rejected");
    const expired = {...input.approved, approvals:input.approved.approvals.map(a=>({...a,expires_at:"2000-01-01T00:00:00+00:00"}))};
    ws.render(expired);
    assert.strictEqual(action("execute").disabled,true);
    assert.strictEqual(ws.state.detail.approvals[0].status,"approved", "local clock must not forge server expiry");
    ws.render(input.pending);
    find(elements["approval-list"], e => e.dataset.confirm === "approve").checked=true;
    elements["tenant-id"].value="   "; calls.length=0;
    await action("approve").handlers.click();
    assert.strictEqual(calls.length,0); assert.strictEqual(ws.state.detail,null);
  } else if (input.scenario === "expiry_in_place") {
    for (const snapshot of [input.pending, input.approved]) {
      Date.now = () => Date.parse(snapshot.approvals[0].created_at) + 1000;
      ws.render(snapshot);
      const decision = snapshot.approvals[0].status === "pending" ? "approve" : "execute";
      assert.strictEqual(action(decision).attrs["aria-describedby"], undefined, "available action must not announce a hidden expired explanation");
      const checkbox = find(elements["approval-list"], e => e.dataset.confirm === decision);
      checkbox.checked = true;
      const editing = find(elements["approval-list"], e => e.dataset.reason === "reject");
      if (editing) editing.value = "review in progress";
      Date.now = () => Date.parse(snapshot.approvals[0].expires_at);
      ws.render(snapshot);
      assert.strictEqual(action(decision).disabled, true);
      const note = find(elements["approval-list"], e => e.dataset.expiryNote === "true");
      assert(note && !note.hidden && note.textContent.includes("到期"), "in-place expiry must explain disabled controls nearby");
      assert.strictEqual(action(decision).attrs["aria-describedby"], note.id);
      assert.strictEqual(find(elements["approval-list"], e => e.dataset.confirm === decision), checkbox);
      assert.strictEqual(checkbox.checked, true);
      if (editing) {
        assert.strictEqual(find(elements["approval-list"], e => e.dataset.reason === "reject"), editing);
        assert.strictEqual(editing.value, "review in progress");
      }
      assert.strictEqual(ws.state.detail.approvals[0].status, snapshot.approvals[0].status);
    }
  } else if (input.scenario === "reload") {
    await tick();
    assert.strictEqual(ws.state.detail.approvals[0].status, "approved");
    assert(calls.every(c => !c.options.method || c.options.method === "GET"), "reload must only read server state");
    assert(calls.some(c => c.path.includes("/timeline?")));
    assert.strictEqual(find(elements["approval-list"], e => e.dataset.confirm === "execute").checked, false);
    assert.strictEqual(elements["action-actor"].value, "");
  } else if (["mutation_refresh_error", "post_transport_exception"].includes(input.scenario)) {
    ws.render(input.pending); elements["action-actor"].value="reviewer";
    find(elements["approval-list"], e => e.dataset.confirm === "approve").checked=true;
    transport = async (path, options) => {
      if (options.method === "POST" && input.scenario === "post_transport_exception") throw new TypeError("connection reset after write");
      return options.method === "POST" ? reply(input.approved.approvals[0]) : reply({error:{kind:"upstream",retryable:true}},503);
    };
    await action("approve").handlers.click();
    assert.strictEqual(ws.state.detail.approvals[0].status,"pending");
    assert.strictEqual(action("approve").disabled,true,"uncertain mutation must require refresh before more writes");
    assert(elements["workspace-status"].textContent.includes("刷新"));
    const count = calls.length;
    await action("approve").handlers.click();
    assert.strictEqual(calls.length, count, "uncertain mutation must block repeated writes");
    transport = async path => reply(path.includes("timeline") ? input.approved.timeline : input.approved);
    await ws.refreshSession();
    assert.strictEqual(ws.state.detail.approvals[0].status,"approved");
    assert.strictEqual(action("execute").disabled,false);
  } else if (input.scenario === "refresh_preserves_review") {
    ws.render(input.pending);
    find(elements["approval-list"], e => e.dataset.confirm === "approve").checked=true;
    find(elements["approval-list"], e => e.dataset.reason === "reject").value="still reviewing";
    const editing = find(elements["approval-list"], e => e.dataset.reason === "reject");
    transport = async path => reply(path.includes("timeline") ? input.pending.timeline : input.pending);
    await ws.refreshSession();
    assert.strictEqual(find(elements["approval-list"], e => e.dataset.confirm === "approve").checked,true,"unchanged refresh must preserve review confirmation");
    assert.strictEqual(find(elements["approval-list"], e => e.dataset.reason === "reject").value,"still reviewing");
    assert.strictEqual(find(elements["approval-list"], e => e.dataset.reason === "reject"),editing,"unchanged polling must retain the focused DOM input");
    const changed = {...input.pending,approvals:input.pending.approvals.map(a=>({...a,risk:"changed risk"}))};
    transport = async path => reply(path.includes("timeline") ? changed.timeline : changed);
    await ws.refreshSession();
    assert.strictEqual(find(elements["approval-list"], e => e.dataset.confirm === "approve").checked,false,"changed proposal must require fresh confirmation");
  } else if (input.scenario === "stale_followup") {
    let resolve;
    transport = () => new Promise(done=>{resolve=done;});
    const old = ws.loadSession(input.pending.session_id);
    elements["tenant-id"].value="other"; ws.confirmTenant();
    calls.length=0;
    resolve(reply(input.pending)); await old;
    assert.strictEqual(calls.length,0,"late detail must not issue followup under the new tenant");
    assert.strictEqual(ws.state.detail,null);
  } else if (input.scenario === "mutation_duplicate_abort") {
    ws.render(input.pending); elements["action-actor"].value="reviewer";
    find(elements["approval-list"], e=>e.dataset.confirm === "approve").checked=true;
    let resolve;
    transport = () => new Promise(done=>{resolve=done;});
    calls.length=0;
    const old = action("approve").handlers.click();
    await action("approve").handlers.click();
    assert.strictEqual(calls.length,1,"double decision must submit once");
    elements["cancel-button"].handlers.click();
    assert.strictEqual(calls[0].options.signal.aborted,true);
    assert.strictEqual(action("approve").disabled,true);
    resolve(reply(input.approved.approvals[0])); await old;
    assert.strictEqual(ws.state.detail.approvals[0].status,"pending");
    assert.strictEqual(calls.length,1,"late mutation must not refresh or overwrite after abort");
  } else if (input.scenario === "nonretry_poll_error") {
    transport = async ()=>reply({...input.detail,phase:"investigating"});
    await ws.loadSession(input.detail.session_id);
    transport = async ()=>reply({error:{kind:"not_found",retryable:false}},404);
    const fn = timers.values().next().value; timers.clear(); await fn(); await tick();
    assert.strictEqual(timers.size,0,"nonretryable error must pause automatic polling");
    assert.strictEqual(ws.state.detail.phase,"investigating");
  } else if (input.scenario === "start_action_audit") {
    transport = async (path, options)=>reply(options.method === "POST" ? input.pending : path.includes("timeline") ? input.pending.timeline : input.pending);
    await submit("intake-form");
    assert(calls.some(c=>c.path.includes("/timeline?") && c.path.endsWith("&limit=500")),"start with action must retrieve maximum available audit window");
    assert(elements["approval-audit"].textContent.includes("created"));
  } else if (input.scenario === "tenant_drafts") {
    ws.render(input.pending); setProposal(); elements["action-actor"].value="old-reviewer";
    elements["tenant-id"].value="tenant-next"; ws.confirmTenant();
    for (const id of ["action-actor", "proposal-tool", "proposal-arguments", "proposal-evidence", "proposal-reason", "proposal-risk", "proposal-rollback"]) assert.strictEqual(elements[id].value,"","tenant switch must clear action draft: " + id);
    assert.strictEqual(elements["action-controls"].hidden,true);
  }
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode=1; }).finally(()=>require("timers").clearTimeout(watchdog));
'''


@pytest.fixture(scope="module")
def browser_payload():
    with TestClient(create_app(build_runtime(replay_settings()))) as client:
        detail = create(client)
        path = "/v2/investigations/" + detail["session_id"]
        action = proposal(detail)
        approval = client.post(path + "/approvals", json={"proposal": action}).json()
        approval_path = "/v2/approvals/" + approval["request_id"]
        pending = client.get(path).json()
        client.post(approval_path + "/approve", json={"actor": "reviewer"})
        approved = client.get(path).json()
        client.post(approval_path + "/execute", json={"actor": "reviewer"})
        executed = client.get(path).json()
        other = create(client)
        rejected_action = client.post("/v2/investigations/" + other["session_id"] + "/approvals", json={"proposal": proposal(other)}).json()
        reject_pending = client.get("/v2/investigations/" + other["session_id"]).json()
        client.post("/v2/approvals/" + rejected_action["request_id"] + "/reject", json={"actor": "reviewer", "reason": "Unsafe evidence"})
        rejected = client.get("/v2/investigations/" + other["session_id"]).json()
        insufficient_response = client.post("/v2/investigations", json={"case_id": "case-insufficient-evidence"})
        assert insufficient_response.status_code == 200
        return {"detail": detail, "proposal": action, "pending": pending, "approved": approved,
                "executed": executed, "reject_pending": reject_pending, "rejected": rejected, "insufficient": insufficient_response.json(), "elements": Document(client.get("/").text).elements,
                "script": str(Path(__file__).resolve().parents[1] / "src/ailab_ops/serving/static/v2/app.js")}


@pytest.mark.parametrize("scenario", ["replay", "online", "errors", "duplicate_cancel_stale", "poll", "proposal", "decisions", "reject_expiry_identity", "mutation_refresh_error", "refresh_preserves_review", "stale_followup", "mutation_duplicate_abort", "nonretry_poll_error", "start_action_audit", "tenant_drafts", "expiry_in_place", "reload", "post_transport_exception"])
def test_browser_flow(browser_payload, scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("Deterministic script harness requires Node; no frontend build chain")
    completed = subprocess.run([node, "-e", HARNESS], input=json.dumps({**browser_payload, "scenario": scenario}),
                               text=True, capture_output=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
