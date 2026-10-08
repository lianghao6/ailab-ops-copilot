"""V2 delivery and browser safety boundaries, without a frontend build chain."""

from html.parser import HTMLParser
from pathlib import Path
import ast
import re
import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from ailab_ops.runtime import build_runtime
from ailab_ops.serving.app import create_app
from test_v2_runtime import replay_settings
from test_v2_api import create


class Document(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


@pytest.fixture
def client():
    with TestClient(create_app(build_runtime(replay_settings()))) as client:
        yield client


def test_root_delivers_semantic_investigation_workspace(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    doc = Document(response.text)
    ids = {attrs.get("id") for _, attrs in doc.elements}
    assert {"intake", "mode-banner", "phase-rail", "evidence-board", "hypotheses",
            "cited-report", "timeline", "action-panel", "session-id"} <= ids
    assert any(tag == "main" for tag, _ in doc.elements)
    assert any(tag == "form" for tag, _ in doc.elements)
    assert "回放" in response.text and "在线" in response.text
    assert any(attrs.get("role") == "status" for _, attrs in doc.elements)
    assert response.headers["cache-control"] == "no-store"


def test_root_references_only_local_external_assets(client):
    doc = Document(client.get("/").text)
    scripts = [attrs for tag, attrs in doc.elements if tag == "script"]
    assert scripts and all(attrs.get("src", "").startswith("/static/v2/") for attrs in scripts)
    styles = [attrs for tag, attrs in doc.elements if tag == "link" and attrs.get("rel") == "stylesheet"]
    assert styles and all(attrs["href"].startswith("/static/v2/") for attrs in styles)
    assert all(not name.startswith("on") for _, attrs in doc.elements for name in attrs)


@pytest.mark.parametrize("asset,mime", [("app.css", "text/css"), ("app.js", "javascript")])
def test_assets_are_cacheable_and_have_content_types(client, asset, mime):
    response = client.get("/static/v2/" + asset)
    assert response.status_code == 200
    assert mime in response.headers["content-type"]
    assert "max-age=" in response.headers["cache-control"]
    assert response.headers["etag"]
    assert client.get("/static/v2/" + asset, headers={"If-None-Match": response.headers["etag"]}).status_code == 304


def test_ui_csp_rejects_remote_inline_and_embedding(client):
    response = client.get("/")
    policy = dict(part.strip().split(" ", 1) for part in response.headers["content-security-policy"].split(";") if part.strip())
    assert policy["script-src"] == "'self'"
    assert policy["connect-src"] == "'self'"
    assert policy["object-src"] == policy["base-uri"] == policy["frame-ancestors"] == "'none'"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/static/v2/app.js").headers["content-security-policy"] == response.headers["content-security-policy"]


def test_browser_has_no_html_execution_sinks_or_secret_persistence(client):
    script = client.get("/static/v2/app.js").text
    assert "textContent" in script and "createElement" in script
    assert not re.search(r"\b(innerHTML|outerHTML|insertAdjacentHTML|eval)\b|document\.write|new\s+Function", script)
    assert "encodeURIComponent" in script and "URLSearchParams" in script
    assert "AbortController" in script
    for kind in ("empty", "loading", "error", "replay_miss"):
        assert kind in script
    assert "localStorage.setItem" in script
    assert not re.search(r"\b(api_key|authorization|access_token|password)\b", script, re.I)


def test_packaged_distribution_includes_all_v2_assets():
    root = Path(__file__).resolve().parents[1]
    config = (root / "pyproject.toml").read_text()
    section = config.split("[tool.setuptools.package-data]", 1)[1].split("\n[", 1)[0]
    patterns = ast.literal_eval(re.search(r"ailab_ops\s*=\s*(\[[^]]*\])", section, re.S).group(1))
    for asset in ("index.html", "app.css", "app.js"):
        relative = Path("serving/static/v2") / asset
        assert any(relative.match(pattern) for pattern in patterns)


def test_browser_renders_real_presentation_as_text_and_keeps_only_context(client):
    """Execute the shipped JS; unsafe sinks, lost trace kinds and URL mixups fail."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional browser-script smoke test requires Node, not a build chain")
    detail = create(client)
    detail["evidence"][0]["excerpt"] = "<img src=x onerror=alert(1)>"
    detail["timeline"]["events"].append({"kind": "tool", "event": "hostile <script>", "payload": {}})
    document = Document(client.get("/").text)
    ids = [attrs["id"] for _, attrs in document.elements if "id" in attrs]
    root = Path(__file__).resolve().parents[1]
    payload = {"ids": ids, "detail": detail, "script": str(root / "src/ailab_ops/serving/static/v2/app.js")}
    harness = r'''
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {}; this.value = ""; this.text = ""; this.handlers = {}; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(x => x.textContent).join(""); }
  set innerHTML(_) { throw Error("Unsafe HTML sink"); }
  set outerHTML(_) { throw Error("Unsafe HTML sink"); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.text = ""; this.children = children; }
  setAttribute(key, value) { this.attrs[key] = value; }
  removeAttribute(key) { delete this.attrs[key]; }
  addEventListener(key, callback) { this.handlers[key] = callback; }
}
const elements = Object.fromEntries(input.ids.map(id => [id, new Element("div")]));
elements["phase-rail"].children = ["intake", "investigating", "validating", "awaiting_approval", "completed", "stopped"].map(phase => {
  const item = new Element("li"); item.dataset.phase = phase; return item;
});
const created = [];
global.document = { getElementById: id => elements[id], createElement: tag => { const element = new Element(tag); created.push(element); return element; }, dispatchEvent: () => {} };
global.window = {};
global.CustomEvent = class { constructor(type, options) { this.type = type; this.detail = options.detail; } };
let saved;
global.localStorage = { getItem: () => null, setItem: (_, value) => { saved = JSON.parse(value); } };
global.fetch = async () => ({ ok: true, status: 200, json: async () => ({model_mode: "replay"}) });
vm.runInThisContext(fs.readFileSync(input.script, "utf8"));
setImmediate(async () => {
  const workspace = window.AILabWorkspace;
  workspace.render(input.detail);
  assert(elements["evidence-list"].textContent.includes("<img src=x onerror=alert(1)>"));
  assert(elements["timeline-list"].textContent.includes("tool / hostile <script>"));
  assert(!created.some(element => ["img", "script", "iframe"].includes(element.tag)));
  assert.deepStrictEqual(Object.keys(saved).sort(), ["sessionId", "tenantId"]);
  workspace.state.tenantId = "tenant&other/中文";
  assert.strictEqual(workspace.sessionPath("id/with?query#fragment"), "/v2/investigations/id%2Fwith%3Fquery%23fragment?tenant_id=tenant%26other%2F%E4%B8%AD%E6%96%87");
  // Superseding a restore must abort it, and ignore a response even if a
  // transport finishes after abort. The current tenant remains authoritative.
  const pending = [];
  global.fetch = (path, options) => new Promise(resolve => pending.push({path, options, resolve}));
  const first = workspace.loadSession("old");
  const second = workspace.loadSession("new");
  assert.strictEqual(pending[0].options.signal.aborted, true);
  pending[1].resolve({ok: true, status: 200, json: async () => ({...input.detail, session_id: "new"})});
  await second;
  pending[0].resolve({ok: true, status: 200, json: async () => ({...input.detail, session_id: "old"})});
  await first;
  assert.strictEqual(workspace.state.sessionId, "new");
  global.fetch = async () => ({ok: false, status: 422, json: async () => ({error: {kind: "replay_miss"}})});
  await workspace.loadSession("miss");
  assert.strictEqual(elements["workspace-status"].dataset.state, "replay_miss");
});
'''
    completed = subprocess.run([node, "-e", harness], input=json.dumps(payload), text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr
