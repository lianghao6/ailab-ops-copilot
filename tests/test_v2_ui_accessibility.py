"""Delivered semantic and CSS contracts; these do not claim browser layout QA."""

import re
import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from ailab_ops.runtime import build_runtime
from ailab_ops.serving.app import create_app
from test_v2_runtime import replay_settings
from test_v2_ui import Document
from test_v2_ui_flows import HARNESS, browser_payload


@pytest.fixture
def assets():
    with TestClient(create_app(build_runtime(replay_settings()))) as client:
        return Document(client.get("/").text), client.get("/static/v2/app.css").text


def declarations(css, selector):
    """Read a flat rule's declared properties, rather than depend on formatting."""
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in [part.strip() for part in selectors.split(",")]:
            return dict((key.strip(), value.strip()) for key, value in
                        (part.split(":", 1) for part in body.split(";") if ":" in part))
    return {}


def test_heading_and_skip_link_reach_focusable_investigation(assets):
    doc, _ = assets
    assert sum(tag == "h1" for tag, _ in doc.elements) == 1
    ids = {attrs["id"]: attrs for _, attrs in doc.elements if "id" in attrs}
    links = [attrs for tag, attrs in doc.elements if tag == "a" and attrs.get("class") == "skip-link"]
    assert len(links) == 1
    target = ids[links[0]["href"].removeprefix("#")]
    assert target.get("tabindex") == "-1", "skip destination must receive keyboard focus"
    assert target.get("aria-labelledby") in ids


def test_static_controls_have_unique_labels_and_valid_descriptions(assets):
    doc, _ = assets
    ids = [attrs["id"] for _, attrs in doc.elements if "id" in attrs]
    assert len(ids) == len(set(ids))
    labels = {attrs.get("for") for tag, attrs in doc.elements if tag == "label"}
    for tag, attrs in doc.elements:
        if tag in {"input", "select", "textarea"}:
            assert attrs.get("id") in labels or attrs.get("aria-label"), attrs
        for target in attrs.get("aria-describedby", "").split():
            assert target in ids
        assert int(attrs.get("tabindex", "0")) <= 0


def test_status_and_native_keyboard_navigation_contracts(assets):
    doc, _ = assets
    status = [attrs for _, attrs in doc.elements if attrs.get("id") == "workspace-status"]
    assert status[0].get("role") == "status" and status[0].get("aria-live") == "polite"
    ids = {attrs["id"] for _, attrs in doc.elements if "id" in attrs}
    anchors = [attrs["href"][1:] for tag, attrs in doc.elements
               if tag == "a" and attrs.get("href", "").startswith("#")]
    assert anchors and all(target in ids for target in anchors)
    # Chapters use native anchors/disclosures rather than custom ARIA tabs.
    assert not any(attrs.get("role") == "tab" for _, attrs in doc.elements)
    assert sum(tag == "details" for tag, _ in doc.elements) == sum(tag == "summary" for tag, _ in doc.elements)
    assert any(tag == "details" for tag, _ in doc.elements)


def test_focus_and_reduced_motion_contracts(assets):
    _, css = assets
    assert declarations(css, ":focus-visible").get("outline", "none") != "none"
    reduced = css.split("@media (prefers-reduced-motion: reduce)", 1)
    assert len(reduced) == 2
    assert declarations(reduced[1], "html").get("scroll-behavior") == "auto"
    assert declarations(reduced[1], "*").get("animation", "").startswith("none")
    assert declarations(reduced[1], "*").get("transition", "").startswith("none")


def test_responsive_layout_and_long_evidence_have_shrink_and_wrap_contracts(assets):
    _, css = assets
    assert declarations(css, "body").get("overflow-wrap") == "anywhere", "dynamic prose must wrap unbroken identifiers"
    pre = declarations(css, "pre")
    assert pre.get("white-space") == "pre-wrap" and pre.get("overflow-wrap") == "anywhere"
    assert declarations(css, ".casebook").get("min-width") == "0"
    assert "minmax(0, 1fr)" in declarations(css, ".workspace").get("grid-template-columns", "")
    assert "@media (max-width: 1000px)" in css
    narrow = css.split("@media (max-width: 720px)", 1)[1]
    assert declarations(narrow, ".workspace").get("display") == "block"
    assert declarations(narrow, ".evidence-card").get("grid-template-columns") == "1fr"
    assert declarations(narrow, ".phase-rail").get("flex-wrap") == "wrap"
    for selector in ("body", ".workspace", ".casebook", ".chapter", ".evidence-card", "pre"):
        rule = declarations(css, selector)
        assert not re.fullmatch(r"\d+(?:px|rem)", rule.get("width", "")), selector
        assert not re.fullmatch(r"[1-9]\d*(?:px|rem)", rule.get("min-width", "")), selector


def test_seven_workspace_states_keep_native_controls_and_evidence_targets(browser_payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("Deterministic DOM contracts require Node; visual QA requires a browser")
    completed = subprocess.run([node, "-e", HARNESS],
                               input=json.dumps({**browser_payload, "scenario": "accessible_states"}),
                               text=True, capture_output=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
