# AILab Ops Copilot

A **simulated enterprise AIOps agent**: it reads job records, logs and metric
series from a fictional training platform and tells you why a job failed.

Built as teaching material, but not a toy. The interesting part of an agent
system is not the agent loop — that is about two hundred lines — it is
everything around it: bounded concurrency in front of an expensive model
server, rate limits and budgets, caching, a degradation ladder, tracing, and an
evaluation that measures whether any of it works. All of that is here, and all
of it is runnable offline.

Everything in the dataset is **synthetic**. No production data was read, copied
or derived. See [Synthetic data](#synthetic-data--the-one-rule).

---

## Contents

- [What it does](#what-it-does)
- [Quickstart](#quickstart)
- [Architecture](#architecture)
- [The four ideas worth looking at](#the-four-ideas-worth-looking-at)
- [Synthetic data — the one rule](#synthetic-data--the-one-rule)
- [The fault playbook](#the-fault-playbook)
- [Using a real model](#using-a-real-model)
- [Concurrency, limits and degradation](#concurrency-limits-and-degradation)
- [Evaluation](#evaluation)
- [Project layout](#project-layout)
- [Configuration](#configuration)
- [Limitations](#limitations)
- [Teaching notes](#teaching-notes)

---

## What it does

You give it a job id. It investigates and answers with a structured diagnosis.

```
$ ailab-ops demo

world      loaded:./data/generated  (boot 139ms)
backend    mock-diagnoser-v1
question   Why did job-68c151b5-0272 (pretrain-speech-990) fail?
           Give me the root cause and what to do about it.

agent steps:
   1. [ok ] get_job({"job_id": "job-68c151b5-0272"})                0ms
   2. [ok ] search_logs({"job_id": "...", "level": "ERROR"})       1ms
   3. [ok ] get_metrics({"job_id": "..."})                        1ms
   4. [ok ] search_runbooks({"query": "AssertionError gpu_util_pct cliff_to_zero"})  2ms
   5. [ok ] get_exit_code_meaning({"code": 134})                  0ms
   6. [llm] final answer                                          2ms
==============================================================================
ROOT CAUSE   rank_crash_assert   Rank crashed on a device-side assert
confidence   0.97

Job job-68c151b5-0272 (FAILED, exit 134) — rank_crash_assert: Rank crashed on a
device-side assert. Decided by unique signature under cascade.

EVIDENCE
  · status=FAILED exit=134 duration=7239s cluster=ailab-prod-b queue=gpu-preemptible
  · first non-cascade error (rank 0, 2025-09-10T12:17:44Z): AssertionError
  · cascade signature: 9 timeout lines across ranks [2,5,6,7,4,1,3,0];
    treated as a symptom, not the cause
  · metric gpu_util_pct: cliff_to_zero (ran near 52.53 then collapsed to zero)

RULED OUT
  · collective_timeout — scored lower: cascade-shaped logs but no unique signature
  · watchdog_hang — scored lower

REMEDIATION
  · Guard the numerics that fed the assert (clamp, nan_to_num, fp32 for the
    reduction). Because the assert kills one rank, expect a collective timeout
    cascade afterwards — fix the assert, the cascade disappears.

GROUND TRUTH (held out from the agent, shown here for teaching)
  root cause   rank_crash_assert   difficulty hard   confounders ['port_conflict']
  agent said   rank_crash_assert   -> AGREES
```

Nine out of ten of these cases look like this: one rank dies, every surviving
rank then reports a collective timeout, and the timeout is the error that
appears *most often*. Reporting it is the single most common misdiagnosis in
distributed training, and getting it right is the point of the exercise.

---

## Quickstart

Requires Python 3.10+. No network, no GPU, no API key, no container.

```bash
git clone <your-repo-url> ailab-ops-copilot
cd ailab-ops-copilot
pip install -e ".[dev]"

make data          # generate the synthetic platform dataset (~2s)
make demo          # one diagnosis end to end, offline
make test          # 159 unit tests, ~3.5s

make serve         # HTTP API + a single-file web UI on :8080
make eval          # accuracy against ground truth
make bench         # concurrency benchmark (needs `make serve` running)
```

Or through the CLI directly:

```bash
ailab-ops gen-data --seed 1234 --jobs 500
ailab-ops inspect job-68c151b5-0272      # dump a job with ground truth + scores
ailab-ops ask "why did job-68c151b5-0272 fail?"
ailab-ops compare                        # retrieval fusion modes, side by side
```

### The web UI

`make serve` then open <http://127.0.0.1:8080>. It shows the live concurrency
gate, the rate-limit and budget state, cache hit rate and circuit breakers, the
agent's step sequence, and a **fault-injection button** that pretends the model
service has died so you can watch the system degrade to its deterministic
evidence-only path and recover.

---

## Architecture

```
                       ┌──────────────────────────────────────────┐
   client ── HTTP ──▶  │  serving/                                │
                       │    limits.py    per-user QPS, tenant      │
                       │                 concurrency, tokens, $    │
                       │    cache.py     semantic cache (per-tenant)│
                       │                 circuit breaker + ladder   │
                       │    gate.py      upstream admission control │
                       │    service.py   the request pipeline       │
                       │    app.py       FastAPI + SSE              │
                       └───────────────┬──────────────────────────┘
                                       │  (one slot per model call)
                       ┌───────────────▼──────────────────────────┐
                       │  agent/    bounded tool-calling loop      │
                       │    ↕ llm/     mock | OpenAI-compatible    │
                       └───────────────┬──────────────────────────┘
                                       │
              ┌────────────────────────┼────────────────────────┐
              ▼                        ▼                        ▼
      ┌───────────────┐        ┌───────────────┐       ┌───────────────┐
      │ tools/        │        │ rag/          │       │ signals.py    │
      │ 7 read-only   │        │ BM25F + dense │       │ shape classify│
      │ tools         │        │ RRF fusion    │       │ cascade rule  │
      └───────┬───────┘        └───────┬───────┘       │ hypothesis    │
              │                        │               │ scoring       │
              ▼                        ▼               └───────────────┘
      ┌───────────────────────────────────────┐
      │  datagen/  the synthetic world         │
      │   faults.yaml ─▶ generator ─▶ world    │
      └───────────────────────────────────────┘
```

Read it in this order: `datagen/` (where the data comes from) → `signals.py`
(what reasoning looks like) → `tools/` (what the agent may look at) → `agent/`
(the loop) → `serving/` (everything that makes it survivable in production).

---

## The four ideas worth looking at

### 1. The cascade rule, implemented rather than described

`signals.py::extract_log_evidence` reduces a multi-rank log to the evidence a
human would use: it sorts errors by timestamp, separates cascade lines from
causal ones, and returns the first *non-cascade* error. `decide()` then refuses
to answer when the only support for a hypothesis is a timeout several scenarios
share. Every runbook in the knowledge base states this rule; this is the code
that enforces it, and `tests/test_signals.py` has a case for each branch.

### 2. Confidence calibration as a first-class metric

`eval/` reports abstention precision and a false-confidence rate alongside
accuracy, because a system that never abstains scores *better* on accuracy and
*worse* on both of the others. The dataset deliberately includes cases whose
correct answer is "insufficient evidence" — telemetry was not collected, verbose
logs rotated away — and refusing to guess is scored as correct. Watch the
tradeoff move when you change `MIN_MARGIN` in `signals.py`.

### 3. The upstream gate

`serving/gate.py` bounds how many model calls are in flight at once, with a
bounded queue, a wait deadline, and priority. This is the component that turns
"the model server fell over and every request failed" into "most requests
succeeded and the excess were told to retry". It is also the easiest to leave
out and the most expensive to leave out.

### 4. The degradation ladder

When the model is unavailable, the system does not fail — it descends:
`full` → `evidence_only` → `unavailable`. The middle rung is the interesting
one: the tools are cheap reads over local data and the scoring is deterministic
code, so **the same evidence pipeline still produces an answer without any model
call**. Press the fault-injection button in the UI and watch it happen.

---

## Synthetic data — the one rule

**Nothing in this repository comes from a real system.** Every entity is
generated locally from `datagen/faults.yaml` by a seeded RNG:

- company, clusters, queues, teams, users, node names, job ids, workdirs,
  container images — all invented;
- log lines, metric series, incident timelines, cost figures — all synthesized;
- failure *patterns* are abstracted from generally-known distributed-training
  failure modes, so the exercise feels real; the artefacts are not.

Consequences worth knowing:

- **Reproducible.** `--seed` fixes the whole world. The same seed gives the
  same dataset byte for byte (`tests/test_world.py::test_generation_is_deterministic`).
- **Private per learner.** Every person runs `make data` with their own seed and
  gets a self-consistent world with its own answers, so nobody can copy a
  neighbour's.
- **Tested for leakage.** `tests/test_world.py` and `tests/test_tools.py` assert
  that no obviously-real identifier and no ground-truth field is reachable
  through any tool.

### Ground truth is an artifact, not a hint

`data/generated/ground_truth.jsonl` holds the answer for every failed job. It is
written to its own file, and the tools never read it. "The agent does not see
the answer" is a property of the data layout, which is checkable, rather than of
the agent's good behaviour, which is not.

---

## The fault playbook

`src/ailab_ops/datagen/faults.yaml` is the project's single source of truth.
**29 failure scenarios** across eight categories:

| category | scenarios | examples |
|---|---|---|
| `memory` | 3 | GPU OOM, host OOM-kill, slow leak |
| `network` | 4 | dependency download timeout, unreachable route, DNS, collective timeout |
| `runtime` | 5 | device-side assert, watchdog hang, deadlock, port conflict, unattributed SIGKILL |
| `storage` | 4 | local disk full, remote quota, object-store read error, stale NFS handle |
| `scheduling` | 4 | preemption, eviction, unschedulable, image pull failure |
| `code` | 5 | compile error, missing dependency, bad config, schema drift, checkpoint mismatch |
| `external` | 3 | 429 rate limit, upstream 5xx, hard quota |
| `unknown` | 1 | no decisive evidence available |

Everything downstream is derived from this file: the generator (which failures
exist, how often, what they look like), the knowledge base (each scenario's
runbook), the mock reasoner (matching evidence to hypotheses), and the
evaluation (the ground-truth labels). **Add a scenario there and it appears
everywhere** — including in the tests, which fan out over the playbook.

Each scenario declares:

```yaml
- id: collective_timeout
  category: network
  difficulty: hard          # easy | medium | hard -> drives the eval split
  prevalence: 5             # relative sampling weight within its difficulty
  distractors: [...]        # rival scenarios whose evidence overlaps
  signals:
    log_patterns: [{ pattern: "Watchdog caught collective operation timeout", weight: 1.00 }]
    metric_shapes: [{ name: gpu_util_pct, shape: staircase, weight: 0.70 }]
    exit_codes: [1, 137, 143]
  remediation: >
    Find the rank that stopped first — it is usually not rank 0 ...
  runbook:
    title: "Runbook: collective timeout is usually a cascade, not a cause"
    body: > ...    # becomes a retrievable knowledge-base document
```

### Difficulty comes from the data, not from the model

- **easy** — one clear signature.
- **medium** — a plausible rival is also present.
- **hard** — two overlapping signatures *plus* a genuine distractor, so the
  agent has to discriminate rather than pattern-match. Verified by
  `test_metrics_gap...`-style assertions in `tests/test_signals.py`.
- **unknown** — deliberately no telemetry at all. Refusing to answer is the
  correct answer, and is scored as such.

---

## Using a real model

The default backend is a **deterministic offline reasoner** (`llm/mock.py`) that
performs the same work a served model would: it plans which tool to call, reads
the results, scores competing hypotheses, and writes a structured answer. It is
not a stub — it goes through the same tool interface, so swapping in a model
exercises identical plumbing and the mock's accuracy is a genuine ceiling for
the architecture.

To point it at a real endpoint:

```bash
export AILAB_LLM_BACKEND=openai
export AILAB_LLM_BASE_URL=http://your-gateway/v1
export AILAB_LLM_MODEL=your-model
export AILAB_LLM_API_KEY=...

ailab-ops ask "why did job-... fail?"
```

Any OpenAI-compatible `/chat/completions` endpoint works — vLLM, SGLang, TGI,
lmdeploy, LiteLLM, one-api, or an internal gateway. The client
(`llm/openai_compat.py`) handles split connect/read timeouts, retry with jittered
backoff, and streamed tool calls whose arguments arrive as JSON string
fragments.

**To try it without any infrastructure**, there is a local stub:

```bash
ailab-ops llm-stub --port 8001 &          # a real HTTP OpenAI-compatible server
export AILAB_LLM_BACKEND=openai
export AILAB_LLM_BASE_URL=http://127.0.0.1:8001/v1
export AILAB_LLM_MODEL=stub-reasoner-v1
ailab-ops serve
```

It also injects failures, so you can watch the retry and breaker logic work:

```bash
curl -X POST "http://127.0.0.1:8001/v1/admin/fail-rate?rate=0.3"
```

---

## Concurrency, limits and degradation

Four independent ceilings, because conflating them is the mistake this part of
the project exists to prevent:

| limiter | the question it answers | what happens without it |
|---|---|---|
| per-user QPS | is one caller abusing the endpoint? | one client starves everyone |
| per-tenant concurrency | how much of the model is one org using? | a noisy tenant saturates the cluster |
| per-tenant tokens/min | what is the sustained generation volume? | overload that never clears |
| per-tenant $/day | what will this cost us? | a surprise invoice |
| **upstream gate** | how many calls may be in flight? | **the model server falls over** |

The gate is the important one. A typical inference server holds a small number
of sequences in flight; past that, latency rises superlinearly and eventually it
rejects or restarts. So the failure mode under load is not "requests get slower",
it is "**every** request fails, including the ones that would have succeeded".
The gate bounds in-flight calls, bounds the queue, and gives a wait deadline.
It is deliberately not the same mechanism as the rate limits, and the benchmark
below measures it in isolation.

### Benchmark

```bash
make serve                       # in one terminal
PYTHONPATH=src python -m ailab_ops.cli serve --llm-latency-ms 400   # if you want the gate to bite
make bench                       # in another
```

Three scenarios, each isolating one mechanism:

- **steady** — unique questions, one worker per user, every ceiling raised.
  Expect ~120/120 succeeded and no rejections; a rejection here would be a bug.
- **burst** — closed-loop clients, per-user limit bypassed, gate shrunk to a
  4-slot queue so the offered load is genuinely bigger than the queue. Expect
  ~100 queue-full and ~8 queue-timeout rejections, and `in_flight` pinned at
  the configured ceiling — never above it.
- **hotkey** — 85% of questions describe the same incident. Expect most traffic
  served from cache and `gate.admitted` barely moving.

The benchmark configures these limits at runtime (`/v1/admin/limits`,
`/v1/admin/gate`) rather than shipping three configs, because a load test that
trips four limits at once produces a rejection count and no understanding.

---

## Evaluation

```bash
make eval                                    # 200 cases, stratified
ailab-ops eval --limit 500 --out runs/r1.json
ailab-ops eval --difficulty hard             # just the hard cases
```

The report is built around the distinctions that map onto real decisions:

```
cases evaluated      150 / 150
exact accuracy       100.0%  (150 correct)
  by category        code:100%  external:100%  memory:100%  network:100% ...
  by difficulty      easy:100%  medium:100%  hard:100%

abstentions          25
  abstention precision 100.0%  (25/25 refusals were correct refusals)
  unknown-case recall  100.0%  (25/25 unknowable cases correctly refused)
false-confidence rate 0.0%   (0 wrong answers given at >= 70% confidence)
answer parse rate    100.0%  (150/150 parseable)
cost                 $0.2347 total, $0.001565 per diagnosis
latency              p50 4ms  p90 5ms  p99 7ms
```

**Read that 100% with suspicion, and then find out why it is not a problem.**
The dataset and the scorer share an author: the same playbook defines both the
signatures the generator emits and the patterns the scorer matches, so the mock
reasoner is being graded on a closed loop. That is worth saying out loud
precisely because a perfect number invites disbelief.

Two things keep it honest anyway:

* **The corpus is noisy on purpose.** ~28% of failed jobs carry warning lines
  belonging to *other* faults — a transient allreduce retry, a brief
  nameserver hiccup, a prefetch queue hitting its limit. They match patterns
  from rival scenarios, so the classification is a real discrimination rather
  than a single substring test. What stops the noise from deciding anything is
  that pattern *weights* act like IDF: the generic warning weighs 0.2 while the
  causal line weighs 1.0. Same insight as BM25, applied to a hand-written
  pattern list.
* **The calibration metrics cannot be gamed by a closed loop.** A system tuned
  to never abstain scores differently here than one tuned to abstain freely, and
  the tradeoff is visible in the report. That is a property of the *measurement*,
  not of the data.

Where the number would genuinely mislead you is as a statement about a real
model on real data. Which is why `ailab-ops compare` measures retrieval on its
own, and why pointing `AILAB_LLM_BACKEND` at a served model is worth doing
before believing anything the offline path says about a served one.

Two of these are unusual and are the most useful:

- **Abstention precision.** A system that refuses everything scores 6% accuracy
  and 100% abstention precision; one that refuses nothing scores 94% and 0%.
  Reporting both makes the tradeoff visible instead of hiding it in one number.
- **False-confidence rate.** A wrong answer delivered at 0.97 confidence costs
  an engineer real time. Accuracy hides this; this does not.

`ailab-ops compare` measures the retriever on its own, over real failure logs,
so you can tell a retrieval problem from a reasoning problem before you start
tuning prompts.

---

## Project layout

```
src/ailab_ops/
  config.py        settings, all environment-driven with offline defaults
  signals.py       ★ shape classification, cascade rule, hypothesis scoring
  runtime.py       one object graph, shared by server / CLI / eval / bench

  datagen/         the synthetic world
    faults.yaml    ★ the source of truth: 29 scenarios
    taxonomy.py    loading and validation
    world.py       entity + telemetry synthesis, serialisation

  rag/             BM25F + hashed-dense retrieval, RRF fusion
    bm25.py  embed.py  store.py  kb.py

  tools/           7 read-only tools, output caps, typed errors
  llm/             backend protocol, offline reasoner, OpenAI-compatible client
    mock.py  openai_compat.py  stub_server.py
  agent/           ★ the bounded tool-calling loop (~200 lines)
  serving/         FastAPI, SSE, gate, limits, cache, degradation
  obs/             tracing, metrics, cost accounting
  eval/            accuracy, calibration, confusion pairs, cost
  bench/           concurrent load generation
  cli.py           every entry point

tests/             159 tests: playbook, world, rag, signals, tools, agent,
                   serving, eval — plus a quality floor on end-to-end accuracy
```

★ = read these first.

---

## Configuration

Everything has a working default; `.env.example` documents the lot. The knobs
that change behaviour most:

| variable | default | effect |
|---|---|---|
| `AILAB_LLM_BACKEND` | `mock` | `mock` (offline) or `openai` |
| `AILAB_LLM_LATENCY_MS` | `0` | simulated think time per call; **set it to ~400 for a meaningful benchmark** |
| `AILAB_UPSTREAM_CONCURRENCY` | `8` | in-flight model calls — the most important number here |
| `AILAB_QUEUE_MAXSIZE` | `64` | how much excess load is queued rather than rejected |
| `AILAB_TENANT_CONCURRENCY` | `16` | per-organisation in-flight limit |
| `AILAB_TENANT_COST_PER_DAY_USD` | `5.0` | daily budget, and the rejection does not clear on retry |
| `AILAB_SEMANTIC_CACHE_THRESHOLD` | `0.86` | near-duplicate question threshold |
| `AILAB_AGENT_MAX_STEPS` | `8` | the loop's step budget |
| `AILAB_SEED` / `AILAB_N_JOBS` | `20260929` / `400` | the generated world |

---

## Limitations

Stated plainly, because a teaching project that oversells itself teaches the
wrong lesson:

- **The world is small.** 400 jobs in memory. The concurrency numbers measure
  the architecture's behaviour, not throughput at scale.
- **The dataset and the classifier share an author.** Round-trip accuracy is
  ~100%, which means the metrics scores are optimistic in a way a real dataset
  would not be. This is why `ailab-ops compare` reports retrieval in isolation,
  and why a real model backend is worth trying.
- **The session store is in-process.** It does not survive a restart and does
  not span replicas. That limitation is deliberate — it is why the production
  answer is a shared store rather than sticky sessions — but it is a
  limitation.
- **The cache invalidates on a TTL, not on evidence change.** A finer key would
  be better and harder to get right.
- **The hashed encoder is not a real embedding model.** It exists so the hybrid
  retrieval lesson works with no download; swapping in a real model changes one
  class.
- **No authentication.** Identity comes from the request body, and the code says
  so at the point where a real deployment would verify a gateway header.

---

## Teaching notes

The project is arranged so that each component can be taught on its own, with
its own failure mode visible in the tests:

| lesson | where | what to change to see it |
|---|---|---|
| retrieval fusion does not automatically beat its parts | `rag/store.py` | set `lexical_weight=0.5` and run the retrieval tests |
| confidence calibration vs accuracy | `signals.py` | move `MIN_MARGIN` and re-run `make eval` |
| the cascade rule | `signals.py::extract_log_evidence` | comment out the cascade filter, watch accuracy fall |
| admission control | `serving/gate.py` | lower `AILAB_UPSTREAM_CONCURRENCY` under `make bench` |
| graceful degradation | `serving/cache.py` | press the fault-injection button in the UI |
| why output caps belong in the tool | `tools/__init__.py` | raise `MAX_LOG_LINES` and watch context and cost grow |
| tool errors must not kill the run | `agent/__init__.py` | the loop's error handling, with tests for each path |

The single most useful exercise: run `make eval`, read the confusion pairs, and
fix the top one. The confusion matrix tells you *which two causes* are being
mixed up; everything else is guesswork until you know that.

---

## License

MIT. See `LICENSE`.
