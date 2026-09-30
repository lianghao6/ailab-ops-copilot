"""Knowledge base construction.

The corpus has two kinds of documents, and the split is deliberate:

1. **Runbooks**, one per fault scenario, derived from the playbook. They are
   written in the voice of a real runbook: symptom, discriminator, fix. They
   deliberately do NOT contain the machine label (`oom_gpu`), because a real
   runbook does not know your classifier's vocabulary. The agent has to map
   "Runbook: GPU out of memory" onto a label, which is a small but genuine
   reasoning step, and one that shows up in the evaluation as retrieval-vs-
   classification error.
2. **Platform documents**: inventory, metric glossary, triage procedure, job
   lifecycle, permissions. These are the documents that answer the questions
   surrounding the diagnosis ("what does this exit code mean anywhere",
   "which queue is preemptible"), and they are what makes a multi-hop question
   interesting: the answer needs evidence from a job record AND a glossary.

Nothing in the corpus is derived from a real platform. Every line is either
generated from `faults.yaml` or written for the exercise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..datagen.taxonomy import Playbook, load_playbook
from .store import Document


@dataclass
class KnowledgeBase:
    retriever: object  # HybridRetriever; typed loosely to avoid a circular import
    docs: list[Document] = field(default_factory=list)

    def search(self, *args, **kwargs):
        return self.retriever.search(*args, **kwargs)  # type: ignore[attr-defined]

    def __len__(self) -> int:
        return len(self.docs)


def build_knowledge_base(
    playbook: Playbook | None = None,
    fusion: str = "rrf",
):
    """Build the retriever and populate it with the full corpus."""
    from .store import HybridRetriever  # local import keeps the module graph flat

    pb = playbook or load_playbook()
    r = HybridRetriever(fusion=fusion)  # type: ignore[arg-type]

    for doc in build_runbook_docs(pb):
        r.add(doc)
    for doc in build_platform_docs(pb):
        r.add(doc)

    r.build()
    return KnowledgeBase(retriever=r, docs=list(r.docs.values()))


def build_runbook_docs(pb: Playbook) -> list[Document]:
    docs: list[Document] = []
    for s in pb.scenarios.values():
        if s.is_unknown:
            continue
        # The verbatim symptom strings are indexed as their own field, because in
        # this domain the query is usually a log fragment rather than a
        # description. A document whose only claim to relevance is that a long
        # body discusses the topic twice should lose to one that contains the
        # exact line, and field weighting is what makes that happen.
        signatures = [p.pattern for p in s.log_patterns if p.weight > 0]
        signatures += [m.name for m in s.metric_shapes if m.weight > 0]
        signatures += [f"{m.name} {m.shape}" for m in s.metric_shapes if m.weight > 0]
        signatures += [f"exit code {c}" for c in s.exit_codes]
        signatures.append(s.name)

        signals = ", ".join(p.pattern for p in s.log_patterns) or "no distinctive log line"
        metrics = (
            ", ".join(f"{m.name} ({m.shape})" for m in s.metric_shapes) or "no metric signature"
        )
        exit_codes = ", ".join(str(c) for c in s.exit_codes) or "not fixed"
        body = (
            f"{s.runbook_body}\n\n"
            f"Typical log evidence: {signals}.\n"
            f"Metric signature: {metrics}.\n"
            f"Exit codes seen: {exit_codes}.\n"
            f"Remediation: {s.remediation}"
        )
        docs.append(
            Document(
                doc_id=f"rb-{s.category}-{s.id}",
                title=s.runbook_title,
                body=body,
                source="runbook",
                tags=(s.category, s.difficulty, "runbook"),
                metadata={"scenario": s.id, "category": s.category, "difficulty": s.difficulty},
                signatures=tuple(dict.fromkeys(signatures)),
            )
        )
    return docs


def build_platform_docs(pb: Playbook) -> list[Document]:
    docs: list[Document] = [
        Document(
            doc_id="plat-lifecycle",
            title="Platform: job lifecycle and status semantics",
            body=(
                "A job moves through SUBMITTED -> SCHEDULED -> RUNNING -> terminal. The terminal "
                "states are SUCCEEDED, FAILED and NEVER_STARTED, and the distinction matters when "
                "diagnosing. NEVER_STARTED means no container ever ran: there is no runtime "
                "telemetry, no metric series, and the only evidence is scheduler output. FAILED "
                "means a container ran and exited non-zero. A job that FAILED within seconds of "
                "starting died during initialisation -- an image, dependency, configuration or "
                "build problem -- and should not be investigated as a runtime failure. Duration is "
                "therefore diagnostic in itself: seconds means startup, minutes means early "
                "training or data loading, hours means a steady-state problem such as a slow "
                "resource leak or a hang. Note that a missing metric series is normally NOT a data "
                "problem; it usually means the job never ran or metrics export was off."
            ),
            source="platform",
            tags=("platform", "lifecycle"),
        ),
        Document(
            doc_id="plat-exit-codes",
            title="Platform: exit code reference",
            body=(
                "Exit code 0 is success. Exit code 1 is a generic application error and tells you "
                "nothing on its own -- read the last log lines. Exit code 2 is commonly a CLI or "
                "argument-parsing failure. Exit code 137 means the process received SIGKILL; it is "
                "a symptom, not a diagnosis, and has several unrelated causes: the kernel OOM "
                "killer, node eviction under resource pressure, and scheduler preemption. Exit code "
                "143 means SIGTERM, and most often indicates a graceful shutdown request from the "
                "scheduler, i.e. preemption or eviction. Exit code 134 is an abort, frequently from "
                "a failed assertion. Because 137 and 143 are shared by several scenarios, an exit "
                "code alone is never a sufficient basis for a root cause: always look for a kernel "
                "OOM line, an eviction event, or a preemption event before deciding. If none of "
                "those is present, the honest conclusion is that the evidence is insufficient."
            ),
            source="platform",
            tags=("platform", "exit-codes", "runtime", "scheduling"),
        ),
        Document(
            doc_id="plat-metric-glossary",
            title="Platform: metric glossary and how each shape reads",
            body=(
                "gpu_mem_used_pct -- GPU memory utilisation. A monotone ramp that reaches the "
                "ceiling followed by a stop indicates out-of-memory at or near the first step; a "
                "rising sawtooth whose floor climbs over hours indicates a leak. gpu_util_pct -- "
                "compute utilisation. An instant cliff to zero is a crash; a long flat plateau is a "
                "hang; descending steps indicate a workload shrinking rather than dying. "
                "host_mem_used_pct -- host memory. A ramp to the ceiling with an accompanying "
                "kernel OOM-kill line is host memory exhaustion; a flat series alongside a SIGKILL "
                "argues against it. disk_used_pct -- local disk. Reaching 100% and then a failed "
                "write is local disk exhaustion. If a write fails while disk_used_pct stays low, "
                "the ceiling is remote, i.e. a quota. disk_read_mbps and net_rx_mbps -- throughput. "
                "Staircase patterns usually mean retry backoff, not a slow link; a simultaneous "
                "collapse of both to zero means traffic stopped, which points at a route or "
                "connectivity failure rather than a slow peer. net_tx_retrans_pct -- a rise with "
                "flat application throughput means bytes are not arriving; a collapse to zero means "
                "nothing is being sent at all. step_time_s -- time per training step. Elevated and "
                "erratic is different from flat: flat means no progress (a hang), erratic means "
                "progress with interference. upstream_error_rate and upstream_qps -- the health of "
                "an external dependency. A rising error rate with QPS pinned at a ceiling is "
                "throttling; an error rate that collapses to zero is a hard stop such as a quota "
                "exhausted or a service that stopped answering."
            ),
            source="platform",
            tags=("platform", "metrics", "glossary"),
        ),
        Document(
            doc_id="plat-triage-procedure",
            title="Procedure: how to triage a failed job",
            body=(
                "Step 1. Fetch the job record. Read status, exit code, submitted/started/finished "
                "timestamps and duration. NEVER_STARTED and a duration of seconds mean the failure "
                "is in scheduling or startup, and runtime telemetry will not exist. Step 2. Fetch "
                "the log tail. Locate the FIRST error line, not the last: the last line is often a "
                "framework shutdown message or a validation error produced by the shutdown itself. "
                "Step 3. If multiple ranks are present and several report the same error, that is a "
                "cascade signature. Do not treat the loudest rank as the cause; rank 0 usually logs "
                "the most, and therefore the least usefully. Diff per-rank logs and find the single "
                "rank whose first error is earliest and whose message is unique. Step 4. Fetch "
                "metrics and classify each series as flat, ramp, cliff, sawtooth or staircase. "
                "Step 5. Retrieve the matching runbook and check that the discriminator described "
                "there is actually present in the evidence. Step 6. State the root cause with the "
                "evidence that supports it, and name the rival explanation you ruled out and why. "
                "Step 7. If the evidence does not discriminate between two explanations, say so "
                "and list the specific evidence that would resolve it. Guessing is worse than "
                "declining: a confident wrong diagnosis sends an engineer down the wrong path and "
                "costs more time than an honest gap."
            ),
            source="platform",
            tags=("platform", "procedure", "triage"),
        ),
        Document(
            doc_id="plat-cluster-inventory",
            title="Platform: clusters, node pools and queues",
            body=(
                "ailab-prod-a and ailab-prod-b are the production training clusters and host the "
                "gpu-h800 and gpu-a100 pools. ailab-research hosts a smaller a100 pool and is the "
                "usual home of exploratory work. ailab-eval hosts the cpu-batch pool and runs "
                "evaluation suites, which do not use GPUs and therefore never fail with GPU "
                "memory problems. Queues: gpu-p0-training is the non-preemptible priority queue -- "
                "work here is not evicted by scheduler policy; gpu-p1-training is standard priority; "
                "gpu-preemptible is the queue where the preemption scenario occurs by design, and a "
                "preemption in that queue is expected behaviour rather than an incident; "
                "npu-training targets the npu-910b pool; eval-batch is for CPU evaluation work. A "
                "useful correlation when several jobs fail together: group the incidents by "
                "cluster and by queue. Failures spanning one node pool indicate infrastructure; "
                "failures spanning one queue indicate policy or quota; failures spanning one team "
                "indicate a shared dependency or a shared configuration change."
            ),
            source="platform",
            tags=("platform", "inventory", "scheduling"),
        ),
        Document(
            doc_id="plat-cascade-rule",
            title="Procedure: the cascade rule for multi-rank failures",
            body=(
                "When a distributed job fails, several ranks frequently log the same error, and the "
                "error they share is almost never the cause. The general shape is: one rank fails "
                "for a genuine reason; the surviving ranks block in a collective operation; after a "
                "timeout window every survivor emits an identical timeout message. The result is a "
                "log where the timeout appears dozens of times and the real error appears once. "
                "Two consequences follow. First, frequency is anti-correlated with causality here: "
                "the error that appears most often is the cascade, and the error that appears once "
                "is the cause. Second, the rank that reports the cascade is not informative -- every "
                "rank reports it -- so use the unique message, not the rank number. Practically, "
                "sort the per-rank first-error timestamps and take the earliest, then check whether "
                "that message is unique to one rank. This rule applies whenever the evidence "
                "includes watchdog, allreduce or NCCL timeout wording alongside a different, "
                "earlier error."
            ),
            source="platform",
            tags=("platform", "procedure", "network", "runtime"),
        ),
        Document(
            doc_id="plat-unknown-policy",
            title="Policy: when to answer 'insufficient evidence'",
            body=(
                "Some failures cannot be diagnosed from the retained telemetry: the log level was "
                "raised so detail was never written, metrics export was off, or the verbose output "
                "rotated away before anyone looked. In these cases the correct answer is "
                "insufficient evidence, together with a short list of what would resolve it -- "
                "re-run with debug logging, enable metrics export, enable crash dumps, or check the "
                "kernel log on the node. This is not a failure of the diagnosis. A system that "
                "always produces a confident root cause is less useful than one that knows when to "
                "stop, because a wrong confident answer costs an engineer more time than an honest "
                "unknown, and it erodes trust in every answer that follows. Practical test: if the "
                "only evidence is a generic exit code such as 137 or 143, and there is no kernel OOM "
                "line, no eviction event and no preemption event, there is nothing to discriminate "
                "on. Note that 'it is probably the most common cause' is not evidence, even when the "
                "guess is statistically reasonable."
            ),
            source="platform",
            tags=("platform", "policy", "unknown", "calibration"),
        ),
        Document(
            doc_id="plat-retry-policy",
            title="Policy: retry, backoff and what must not be retried",
            body=(
                "Transient dependency failures should be retried with exponential backoff and "
                "jitter: HTTP 5xx from an upstream service, object store throttling (SlowDown), and "
                "short-lived connection resets. Rate limiting (HTTP 429) is retryable but only with "
                "the server-supplied retry-after interval and with jitter; retrying immediately "
                "adds load and makes the limit last longer. Two classes must NOT be blindly "
                "retried. First, a hard quota or budget exhaustion: no amount of retrying creates "
                "quota, so retrying simply burns the window and then fails anyway, which is worse "
                "than failing fast. Second, deterministic failures -- a compile error, a missing "
                "dependency, an invalid configuration, a schema mismatch. Retrying a deterministic "
                "failure wastes the whole window and delays the engineer. A useful heuristic: if the "
                "error text is identical on the second attempt, it is deterministic; if it varies, "
                "it is transient. Also note that retrying a request whose first attempt may have "
                "succeeded on the server is only safe when the operation is idempotent."
            ),
            source="platform",
            tags=("platform", "policy", "external", "network"),
        ),
        Document(
            doc_id="plat-permissions",
            title="Platform: what this assistant may and may not do",
            body=(
                "This assistant is read-only by design. It can read job records, job logs, metric "
                "series, incident history and the knowledge base. It cannot restart or resubmit a "
                "job, modify a queue or quota, cancel another user's work, delete checkpoints, or "
                "write to any production system. Anyone can use it to investigate any job in any "
                "team, because job metadata and logs are considered platform-wide operational data "
                "rather than private tenant data. Every tool call is recorded with the caller "
                "identity, the arguments and the result, so a diagnosis can always be audited "
                "afterwards. If a user asks it to perform an action rather than answer a question, "
                "it should say what the correct action is and who owns it, rather than attempting "
                "the action. This boundary is not a limitation of the implementation; it is the "
                "correct scope for a diagnostic assistant, because an assistant that can restart "
                "jobs will eventually restart the wrong one."
            ),
            source="platform",
            tags=("platform", "policy", "permissions", "safety"),
        ),
    ]
    return docs
