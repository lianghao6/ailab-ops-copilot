"""Emit authored simulation fixtures as JSONL; never called by the runtime.

These responses were manually authored to demonstrate the protocol, NOT captured
from a live model. Regenerate explicitly after deliberate prompt/tool changes:
PYTHONPATH=src python scripts/build_v2_replays.py
Review stdout before replacing data/v2/replays/investigations.jsonl.
"""

from dataclasses import asdict
import json

from ailab_ops.cases.loader import load_case
from ailab_ops.investigation import Budget, InvestigationOrchestrator
from ailab_ops.llm.base import LLMResponse, ToolCall, FinishReason, Usage
from ailab_ops.tools.cases import build_case_registry


REPORTS = {
    "case-gpu-assert": {
        "root_cause": "gpu_device_assert", "confidence": 0.9,
        "summary": "Rank 2 reports a local indexing assertion before peers time out in a collective.",
        "claims": ["The indexing assertion on rank 2 precedes the peer watchdog errors.",
                   "Retained GPU and host memory samples do not show capacity exhaustion."],
        "ruled_out": ["Peer collective watchdogs as the first observed failure", "Capacity exhaustion in retained samples"],
        "unknowns": ["The offending input and precise kernel origin remain unproven."],
        "recommendations": ["Retain all worker logs and inspect the embedding input indices."],
    },
    "case-collective-timeout": {
        "root_cause": "collective_transport_failure", "confidence": 0.85,
        "summary": "Transport errors and interface counters corroborate a communication interruption before the watchdog.",
        "claims": ["RDMA link and completion errors occur before the collective watchdog.",
                   "Interface error counters independently support a transport interruption."],
        "ruled_out": ["A collective watchdog alone identifies a physical component"],
        "unknowns": ["The specific cable, adapter or switch fault remains unidentified."],
        "recommendations": ["Request a node and network health inspection with retained counters."],
    },
    "case-insufficient-evidence": {
        "root_cause": "insufficient_evidence", "confidence": 0.0,
        "summary": "The retained shutdown tail does not establish the initiating failure.",
        "claims": ["SIGTERM and exit 143 describe termination without identifying its initiator.",
                   "Earlier worker stderr, scheduler events and resource telemetry are unavailable."],
        "ruled_out": [],
        "unknowns": ["worker stderr before shutdown", "scheduler termination events",
                     "signal sender or container termination reason", "resource and node health telemetry"],
        "recommendations": ["Request earlier logs and scheduler termination events before attributing a cause."],
    },
}


class AuthoringGateway:
    mode = "replay"
    model = "authored-simulation-v2"

    def __init__(self, case_id):
        self.case_id, self.index = case_id, 0

    def complete(self, messages, tools=None, *, max_tokens=1024):
        index = self.index
        self.index += 1
        if index == 0:
            response = LLMResponse(content=json.dumps({"type": "plan", "plan": [
                "Read case context and telemetry coverage", "Compare worker logs and metric chronology", "Report only cited findings"]}))
        elif index == 1:
            response = LLMResponse(tool_calls=[ToolCall(f"read-{i}", name, {"case_id": self.case_id})
                for i, name in enumerate(["get_case_snapshot", "get_case_logs", "get_case_metrics"])], finish_reason=FinishReason.TOOL_CALLS)
        else:
            ids = [item["evidence_id"] for message in messages if message.role == "tool"
                   for item in json.loads(message.content)["evidence_items"]]
            report = REPORTS[self.case_id]
            if index == 2:
                response = LLMResponse(content=json.dumps({"type": "hypotheses", "hypotheses": [{
                    "hypothesis_id": "h1", "title": report["summary"], "confidence": report["confidence"],
                    "supporting_evidence_ids": ids}]}))
            else:
                response = LLMResponse(content=json.dumps({"type": "report", "report": {**report,
                    "claims": [{"text": claim, "evidence_ids": ids} for claim in report["claims"]]}}))
        response.usage = Usage(120, 80)
        response.model = self.model
        raw = asdict(response)
        raw["finish_reason"] = response.finish_reason.value
        print(json.dumps({"provenance": "manually-authored simulation; not live model output",
            "messages": [asdict(message) for message in messages],
            "available_tool_names": [tool.name for tool in tools or []], "response": raw}, ensure_ascii=False))
        return response


if __name__ == "__main__":
    for case_id in REPORTS:
        orchestrator = InvestigationOrchestrator(AuthoringGateway(case_id), build_case_registry(load_case(case_id)))
        state = orchestrator.run(f"Diagnose {case_id}.", case_id=case_id, budget=Budget(8, 32768, None))
        assert state.report is not None, state.stop_reason
