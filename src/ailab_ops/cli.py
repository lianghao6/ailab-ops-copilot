"""Command-line entry point.

    ailab-ops gen-data [--seed N] [--jobs N] [--out DIR]
    ailab-ops demo [--job JOB_ID] [--json]
    ailab-ops ask "question"
    ailab-ops serve [--host H] [--port P]
    ailab-ops llm-stub [--port P] [--fail-rate F]
    ailab-ops eval [--limit N] [--difficulty D] [--category C] [--out FILE]
    ailab-ops bench [--base-url URL] [--concurrency N] [--requests N] [--scenario S]
    ailab-ops compare [--rrf-only]         retrieval comparison across fusion modes
    ailab-ops inspect JOB_ID               dump everything about one job, with ground truth

Every command works offline. `serve` is the only one that opens a port, and
`bench` is the only one that needs a server already running.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import PROJECT_ROOT, Settings, get_settings, reset_settings


def _print_step(step) -> None:
    if step.kind == "tool":
        mark = "ok " if step.ok else "ERR"
        extra = f"  {step.note}" if step.note else ""
        print(
            f"  {step.index:>2}. [{mark}] {step.tool}({_short_json(step.arguments)}) "
            f"{step.duration_ms:.0f}ms{extra}",
            file=sys.stderr,
        )
        if step.error:
            print(f"      error: {step.error}", file=sys.stderr)
    else:
        print(f"  {step.index:>2}. [llm] {step.note or 'response'} {step.duration_ms:.0f}ms", file=sys.stderr)


def _short_json(d) -> str:
    s = json.dumps(d, ensure_ascii=False, default=str)
    return s if len(s) <= 90 else s[:87] + "…"


# --------------------------------------------------------------------------


def cmd_gen_data(args: argparse.Namespace) -> int:
    from .datagen import generate_world, write_timeseries_csv, write_world
    from .datagen.taxonomy import load_playbook

    s = get_settings()
    seed = args.seed if args.seed is not None else s.seed
    n = args.jobs if args.jobs is not None else s.n_jobs
    out = Path(args.out) if args.out else s.resolved_data_dir()

    pb = load_playbook()
    print(f"generating {n} jobs (seed={seed}, {len(pb.scenarios)} fault scenarios) …")
    world = generate_world(seed=seed, n_jobs=n, playbook=pb)
    written = write_world(world, out)
    write_timeseries_csv(world, out / "metrics_sample.csv")

    summ = world.summary()
    print(f"\nwrote {len(written)} artifacts to {out}")
    print(f"  jobs        {summ.n_jobs}  (failed {summ.n_failed}, ok {summ.n_succeeded}, never started {summ.n_never_started})")
    print(f"  incidents   {summ.n_incidents}")
    print(f"  log records {summ.n_log_records}")
    print(f"  metric series {summ.n_metric_series}")
    print("\nfailures by category:")
    for cat, k in sorted(summ.category_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {cat:<12} {k}")
    print("\nby difficulty:")
    for d, k in sorted(summ.difficulty_counts.items()):
        print(f"  {d:<12} {k}")
    print("\nby cause:")
    for c, k in sorted(summ.scenario_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {c:<28} {k}")
    print(
        "\nAll of this is synthetic. Names, hosts, job ids, logs and metrics were generated "
        "locally from faults.yaml; no production data was read, copied or derived."
    )
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .runtime import build_runtime, default_question

    rt = build_runtime()
    job_id = args.job
    if not job_id:
        job_id, question = default_question(rt.world)
    else:
        job = rt.world.job(job_id)
        if job is None:
            print(f"job {job_id!r} not found", file=sys.stderr)
            return 2
        question = f"Why did {job_id} ({job.name}) fail? Give me the root cause and what to do about it."

    job = rt.world.job(job_id)
    print(f"world      {rt.source}  (boot {rt.boot_ms:.0f}ms)")
    print(f"backend    {rt.llm.model}")
    print(f"question   {question}\n")
    print("agent steps:", file=sys.stderr)

    result, tracer = rt.diagnose(question)
    for st in result.steps:
        _print_step(st)

    print("\n" + "=" * 78)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    elif result.parsed:
        _print_parsed(result.parsed)
    else:
        print(result.answer)

    print("=" * 78)
    print(
        f"\nstop={result.stop_reason}  llm_calls={result.n_llm_calls}  tool_calls={result.n_tool_calls}  "
        f"tokens={result.tokens_total}  {result.elapsed_ms:.0f}ms"
    )
    if job:
        verdict = (result.parsed or {}).get("root_cause")
        print("\n" + "-" * 78)
        print(f"GROUND TRUTH (held out from the agent, shown here for teaching):")
        print(f"  root cause   {job.root_cause}  ({job.root_cause_name})")
        print(f"  category     {job.category}")
        print(f"  difficulty   {job.difficulty}")
        if job.confounders:
            print(f"  confounders  {job.confounders}   <- a second, plausible signature is present on purpose")
        if job.is_insufficient_evidence:
            print("  NOTE         this case is designed to be undiagnosable: the correct answer is")
            print("               'insufficient evidence', and refusing to guess is scored as correct")
        agree = verdict == job.root_cause
        print(f"\n  agent said   {verdict}   -> {'AGREES' if agree else 'DISAGREES'}")
        print("-" * 78)
    return 0


def _print_parsed(p: dict) -> None:
    rc = p.get("root_cause")
    label = p.get("root_cause_label") or ""
    conf = p.get("confidence")
    print(f"ROOT CAUSE   {rc}   {label}")
    print(f"confidence   {conf}")
    if p.get("insufficient_evidence"):
        print("             (the agent declined to guess — see the evidence below)")
    print()
    if p.get("summary"):
        print(p["summary"])
        print()
    for name, key in (("EVIDENCE", "evidence"), ("RULED OUT", "ruled_out"),
                      ("REMEDIATION", "remediation"), ("SOURCES", "citations")):
        items = p.get(key) or []
        if not items:
            continue
        print(f"{name}")
        for it in items:
            if isinstance(it, dict):
                cause = it.get("cause") or it.get("scenario_id") or ""
                why = it.get("why") or it.get("rationale") or ""
                print(f"  · {cause}" + (f" — {why}" if why else ""))
            else:
                print(f"  · {it}")
        print()


def cmd_ask(args: argparse.Namespace) -> int:
    from .runtime import build_runtime

    rt = build_runtime()
    result, _ = rt.diagnose(args.question)
    for st in result.steps:
        _print_step(st)
    if result.parsed:
        print()
        _print_parsed(result.parsed)
    else:
        print(result.answer)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .serving import create_app

    if args.llm_latency_ms is not None:
        os.environ["AILAB_LLM_LATENCY_MS"] = str(args.llm_latency_ms)
        reset_settings()
    s = get_settings()
    host = args.host or s.host
    port = args.port or s.port
    print(
        f"starting AILab Ops Copilot on http://{host}:{port}\n"
        f"  UI       http://127.0.0.1:{port}/\n"
        f"  OpenAPI  http://127.0.0.1:{port}/docs\n"
        f"  backend  {s.llm_backend} ({s.llm_model})\n"
        f"  upstream concurrency {s.upstream_concurrency}, queue {s.queue_maxsize}\n"
        f"  simulated model latency {s.llm_latency_ms:.0f}ms per call"
        + (
            "\n  (0ms: fine for the UI, but a benchmark will not see the gate contended — "
            "use --llm-latency-ms 600)"
            if s.llm_latency_ms <= 0
            else ""
        )
    )
    uvicorn.run(create_app(), host=host, port=port, log_level=s.log_level.lower())
    return 0


def cmd_llm_stub(args: argparse.Namespace) -> int:
    import uvicorn

    from .llm.stub_server import create_stub_app

    print(
        f"local OpenAI-compatible stub on http://127.0.0.1:{args.port}/v1\n"
        f"  point the copilot at it with:\n"
        f"    export AILAB_LLM_BACKEND=openai\n"
        f"    export AILAB_LLM_BASE_URL=http://127.0.0.1:{args.port}/v1\n"
        f"    export AILAB_LLM_MODEL=stub-reasoner-v1\n"
        f"  inject failures with  POST /v1/admin/fail-rate?rate=0.3"
    )
    uvicorn.run(
        create_stub_app(model=args.model, fail_rate=args.fail_rate, latency_s=args.latency),
        host=args.host, port=args.port, log_level="warning",
    )
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from .eval import build_cases, run_eval, write_cases, write_report
    from .runtime import build_runtime

    rt = build_runtime()
    cases = build_cases(
        rt,
        limit=args.limit,
        include_unknown=not args.no_unknown,
        difficulty=args.difficulty,
        category=args.category,
    )
    print(f"evaluating {len(cases)} cases with backend={rt.settings.llm_backend} …")
    report = run_eval(rt, cases, max_steps=args.max_steps)

    for line in report.summary_lines():
        print(line)

    if args.out:
        p = write_report(report, args.out)
        write_cases(Path(args.out), cases)
        print(f"\nwrote {p} and eval_cases.jsonl")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """Retrieval-only comparison across fusion modes.

    Separating this from the end-to-end evaluation is the single most useful
    diagnostic split in the project: if changing the retriever moves end-to-end
    accuracy, you have a retrieval problem; if it moves nothing, your problem is
    elsewhere and you can stop tuning the retriever.
    """
    from .datagen.taxonomy import load_playbook
    from .rag import build_knowledge_base
    from .signals import extract_log_evidence
    from .runtime import build_runtime

    rt = build_runtime()
    pb = load_playbook()

    # Build a query set from actual failure logs: the retrieval query in
    # production is the observed symptom, so that is what it must be measured
    # against.
    queries: list[tuple[str, str]] = []
    for job in rt.world.jobs:
        if job.status == "SUCCEEDED" or not job.root_cause or job.is_insufficient_evidence:
            continue
        logs = rt.world.logs_for(job.job_id)
        ev = extract_log_evidence(logs, pb)
        q = (ev.first_error or "")[:200]
        if q:
            queries.append((job.job_id, q))

    modes = ["rrf"] if args.rrf_only else ["rrf", "linear"]
    print(f"retrieval comparison over {len(queries)} real failure queries "
          f"({len(rt.kb.docs)} docs)\n")
    header = f"{'mode':<10} {'top1':>7} {'top3':>7} {'mrr':>7}"
    print(header)
    print("-" * len(header))

    for mode in modes:
        kb = build_knowledge_base(pb, fusion=mode)
        top1 = top3 = 0
        rr_sum = 0.0
        for job_id, q in queries:
            truth = rt.world.job(job_id).root_cause
            res = kb.retriever.search(q, top_k=5)
            ids = [h.doc.metadata.get("scenario") for h in res.hits]
            if truth in ids:
                rank = ids.index(truth) + 1
                rr_sum += 1.0 / rank
                top3 += 1 if rank <= 3 else 0
                top1 += 1 if rank == 1 else 0
        n = max(len(queries), 1)
        print(f"{mode:<10} {top1 / n:>6.1%} {top3 / n:>6.1%} {rr_sum / n:>7.3f}")

    print(
        "\nwhat this means:\n"
        "  · top1/top3 are whether the runbook for the TRUE cause was retrieved.\n"
        "  · MRR (mean reciprocal rank) rewards putting it first, not merely present.\n"
        "  · low top3 with high end-to-end accuracy = the agent reasoned its way past bad\n"
        "    retrieval; low top3 and low end-to-end accuracy = fix retrieval before prompts."
    )
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    import asyncio

    from .bench import run_benchmark, write_bench_report

    scenarios = [s.strip() for s in args.scenario.split(",") if s.strip()]
    job_ids = args.jobs or []

    if not job_ids:
        # Ask the server which jobs exist, so the benchmark is not coupled to a
        # local dataset that may differ from the one the server is serving.
        import httpx

        from .bench import _is_local

        try:
            with httpx.Client(timeout=10.0, trust_env=not _is_local(args.base_url)) as c:
                r = c.get(f"{args.base_url}/v1/jobs", params={"limit": 40})
                r.raise_for_status()
                job_ids = [j["job_id"] for j in r.json()["jobs"] if j["failed"]][:24]
        except Exception as exc:
            print(f"cannot reach {args.base_url} to discover jobs: {exc}", file=sys.stderr)
            print("start the server first:  make serve", file=sys.stderr)
            return 2

    if not job_ids:
        print("no failed jobs found on the server", file=sys.stderr)
        return 2

    print(
        f"benchmarking {args.base_url}\n"
        f"  scenarios   {scenarios}\n"
        f"  clients     {args.concurrency}\n"
        f"  requests    {args.requests}\n"
        f"  job pool    {len(job_ids)}\n\n"
        "Note: the model backend is the offline reasoner, so these numbers measure the\n"
        "architecture — admission control, queueing, caching, degradation — not inference\n"
        "throughput. Point it at a real model to measure the model.\n"
    )
    reports = asyncio.run(
        run_benchmark(
            base_url=args.base_url,
            scenarios=scenarios,
            concurrency=args.concurrency,
            n_requests=args.requests,
            job_ids=job_ids,
            rate_per_client=args.rate,
        )
    )
    for r in reports:
        for line in r.summary_lines():
            print(line)
        print()
    if args.out:
        p = write_bench_report(reports, args.out)
        print(f"wrote {p}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Dump one job in full, including the ground truth the agent never sees.

    This is the debugging view for the dataset itself: when the evaluation says
    the agent is wrong, this is where you check whether the data was actually
    diagnosable.
    """
    from .datagen.taxonomy import load_playbook
    from .runtime import build_runtime
    from .signals import classify_series, extract_log_evidence, score_hypotheses, decide

    rt = build_runtime()
    job = rt.world.job(args.job_id)
    if job is None:
        print(f"job {args.job_id!r} not found", file=sys.stderr)
        return 2

    pb = load_playbook()
    print("=" * 78)
    print(f"JOB {job.job_id}  {job.name}")
    print("=" * 78)
    for k in ("team_id", "submitter", "kind", "cluster", "queue", "status", "exit_code",
              "submitted_at", "started_at", "finished_at", "duration_s", "image", "workdir",
              "world_size"):
        print(f"  {k:<14} {getattr(job, k)}")
    print(f"  nodes          {job.node_ids}")

    print("\nGROUND TRUTH (held out from the agent)")
    print(f"  root_cause     {job.root_cause}  ({job.root_cause_name})")
    print(f"  category       {job.category}")
    print(f"  difficulty     {job.difficulty}")
    print(f"  confounders    {job.confounders or 'none'}")
    print(f"  undiagnosable  {job.is_insufficient_evidence}")

    inc = rt.world.incident_for(job.job_id)
    if inc:
        print(f"\nINCIDENT {inc.incident_id} ({inc.severity})")
        for t in inc.timeline:
            print(f"  {t['ts']}  {t['actor']:<14} {t['event']}")
        print(f"  resolution: {inc.resolution[:200]}")

    logs = rt.world.logs_for(job.job_id)
    print(f"\nLOG ({len(logs)} records retained)")
    for r in logs[-args.logs:]:
        print(f"  {r.ts} {r.level:<8} r{r.rank:<3} {r.message[:150]}")

    series = rt.world.metrics_for(job.job_id)
    print(f"\nMETRICS ({len(series)} series)")
    metrics_for_scoring: dict[str, list[float]] = {}
    for s in series:
        v = classify_series(s.values, s.metric)
        metrics_for_scoring[s.metric] = s.values
        print(f"  {s.metric:<22} {v.shape:<16} {v.detail[:60]}")
        print(f"    {s.values[:10]}{' …' if len(s.values) > 10 else ''}")

    ev = extract_log_evidence(logs, pb)
    print("\nEXTRACTED LOG EVIDENCE")
    print(f"  first non-cascade error  {ev.first_error}")
    print(f"  cascade shaped           {ev.is_cascade_shaped} (ranks {ev.ranks_reporting_cascade})")
    print(f"  signatures matched       {sorted(ev.matched)}")
    print(f"  truncation hints         {ev.truncation_hints}")

    hyps = score_hypotheses(pb, ev, metrics=metrics_for_scoring,
                            exit_code=job.exit_code, status=job.status)
    print("\nHYPOTHESIS SCORES (the deterministic part of the reasoning)")
    for h in hyps[:6]:
        flag = "  <-- truth" if h.scenario_id == job.root_cause else ""
        print(f"  {h.scenario_id:<28} {h.score:>6.2f}{flag}")
        if h.log_support or h.metric_support:
            print(f"      {', '.join(h.log_support + h.metric_support)}")
        if h.penalty:
            print(f"      penalty {h.penalty:.2f}: {h.rationale}")

    v = decide(pb, ev, hyps, status=job.status)
    print("\nDETERMINISTIC VERDICT")
    print(f"  root_cause   {v.root_cause}")
    print(f"  confidence   {v.confidence:.3f}   margin {v.margin:.3f}   decided_by {v.decided_by}")
    for n in v.notes:
        print(f"  note: {n}")
    agree = v.root_cause == job.root_cause
    print(f"\n  verdict vs truth: {'AGREES' if agree else 'DISAGREES'}")
    if not agree and job.difficulty == "hard":
        print("  (a hard case: a second plausible signature is present by design)")
    print("=" * 78)
    return 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ailab-ops",
        description="AILab Ops Copilot — a simulated enterprise AIOps agent (all data is synthetic).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen-data", help="generate the synthetic platform dataset")
    g.add_argument("--seed", type=int, default=None)
    g.add_argument("--jobs", type=int, default=None)
    g.add_argument("--out", type=str, default=None)
    g.set_defaults(fn=cmd_gen_data)

    d = sub.add_parser("demo", help="run one diagnosis end to end, offline")
    d.add_argument("--job", type=str, default=None)
    d.add_argument("--json", action="store_true")
    d.set_defaults(fn=cmd_demo)

    a = sub.add_parser("ask", help="ask one question")
    a.add_argument("question", type=str)
    a.set_defaults(fn=cmd_ask)

    s = sub.add_parser("serve", help="start the HTTP API and the demo UI")
    s.add_argument("--host", type=str, default=None)
    s.add_argument("--port", type=int, default=None)
    s.add_argument(
        "--llm-latency-ms", type=float, default=None,
        help="simulated think time per model call; use ~600 for a meaningful benchmark",
    )
    s.set_defaults(fn=cmd_serve)

    ls = sub.add_parser("llm-stub", help="run a local OpenAI-compatible endpoint (optional)")
    ls.add_argument("--host", type=str, default="127.0.0.1")
    ls.add_argument("--port", type=int, default=8001)
    ls.add_argument("--model", type=str, default="stub-reasoner-v1")
    ls.add_argument("--fail-rate", type=float, default=0.0)
    ls.add_argument("--latency", type=float, default=0.0)
    ls.set_defaults(fn=cmd_llm_stub)

    e = sub.add_parser("eval", help="evaluate against ground truth")
    e.add_argument("--limit", type=int, default=100)
    e.add_argument("--difficulty", type=str, default=None, choices=["easy", "medium", "hard"])
    e.add_argument("--category", type=str, default=None)
    e.add_argument("--max-steps", type=int, default=None)
    e.add_argument("--no-unknown", action="store_true", help="exclude the unknowable cases")
    e.add_argument("--out", type=str, default=None)
    e.set_defaults(fn=cmd_eval)

    c = sub.add_parser("compare", help="compare retrieval fusion modes")
    c.add_argument("--rrf-only", action="store_true")
    c.set_defaults(fn=cmd_compare)

    b = sub.add_parser("bench", help="run the concurrency benchmark against a running server")
    b.add_argument("--base-url", type=str, default="http://127.0.0.1:8080")
    b.add_argument("--concurrency", type=int, default=32)
    b.add_argument("--requests", type=int, default=200)
    b.add_argument("--scenario", type=str, default="steady,burst,hotkey")
    b.add_argument("--rate", type=float, default=None, help="per-client req/s; omit for closed loop")
    b.add_argument("--jobs", nargs="*", default=None)
    b.add_argument("--out", type=str, default=None)
    b.set_defaults(fn=cmd_bench)

    i = sub.add_parser("inspect", help="dump one job with ground truth and hypothesis scores")
    i.add_argument("job_id", type=str)
    i.add_argument("--logs", type=int, default=25)
    i.set_defaults(fn=cmd_inspect)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # `.env` is optional and never fatal: the project must run with an empty
    # environment, so a missing or malformed file must not stop it.
    _load_dotenv()
    reset_settings()
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


def _load_dotenv() -> None:
    for name in (PROJECT_ROOT / ".env", Path.cwd() / ".env"):
        if not name.exists():
            continue
        for line in name.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


if __name__ == "__main__":
    raise SystemExit(main())
