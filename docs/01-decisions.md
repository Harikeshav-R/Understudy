# 01 — Decisions (ADRs)

Every decision here is **locked**. An agent or team member who wants to change one must
open an issue titled `ADR-NNN revisit: <reason>` and get explicit sign-off before writing
code. Silently deviating from an ADR is the single most damaging thing that can happen to
a two-person parallel build, because the other person's component is already written
against it.

Format: **Decision** / **Why** / **Rejected**.

---

## Environment and constraints

### ADR-001 — Everything runs locally on k3d. No cloud spend.
**Decision.** A single k3d (k3s-in-Docker) cluster on the developer's machine hosts
production, all twins, and observability. No managed Kubernetes, no cloud provider APIs.
**Why.** Hard constraint: zero budget. k3d starts in seconds, supports multiple nodes if
needed, ships with a local registry, and is the lightest real Kubernetes available on
macOS. "Real Kubernetes API" is preserved, which is what makes the K8s integration honest.
**Rejected.** kind (heavier, slower image loading), minikube (heaviest), Docker Compose
(no Kubernetes API, kills the actuation story), any managed cluster (costs money).

### ADR-002 — Production is a namespace, and the boundary is enforced, not implied.
**Decision.** `ust-prod` is production. Twins live in `ust-twin-*`. The boundary is
enforced by three mechanisms: a dedicated ServiceAccount per role with RBAC scoped to its
namespaces, NetworkPolicies blocking twin→prod traffic, and invariant K2 in the safety
kernel.
**Why.** A separate cluster per environment does not fit in 18 GB. A namespace boundary
with real RBAC and real NetworkPolicies is a defensible simplification, and it makes the
"never touch prod from twin code" rule machine-checkable rather than a coding convention.
**Rejected.** Separate clusters (RAM), separate cloud accounts (money), honour-system
separation (indefensible under judging).

### ADR-003 — 18 GB RAM is a first-class design constraint with a written budget.
**Decision.** The system operates under an explicit memory budget (see
`docs/02-architecture.md` §2.10). Every workload sets requests and limits. Docker Desktop
is allocated 12 GB. If a component exceeds its budget line, that is a bug, not a tuning
opportunity.
**Why.** Four services × four environments plus two Postgres instances plus Prometheus,
Loki and the control plane is genuinely close to the ceiling. Discovering this at hour 30
would be fatal; discovering it in a table on day one is arithmetic.
**Rejected.** Optimism.

### ADR-004 — Python everywhere, 3.12.
**Decision.** Agent runtime, demo services, load generator, mirror gateway, eval harness:
all Python 3.12. `uv` for dependency and venv management.
**Why.** Stated preference; single-language repo halves the toolchain surface for a
two-person build; the mirror gateway's throughput requirements are trivially inside
Python's reach at demo scale (hundreds of RPS, not tens of thousands).
**Rejected.** Go for the mirror gateway and fleet controller. Genuinely the better
language for both, and rejected only because a second toolchain, a second test runner and
a second CI lane cost more than they buy here. If the mirror gateway ever becomes the
bottleneck, ADR-018 says what to do instead.

---

## Runtime and orchestration

### ADR-005 — LangGraph is the orchestration layer.
**Decision.** The control loop is a LangGraph `StateGraph` over a single typed state
object. Every node is a pure-ish function `(State) -> StateDelta`, and every node's real
work lives in a component module that LangGraph merely calls.
**Why.** Chosen by the operator. It gives durable checkpointing, a visualisable graph for
the demo, and conditional-edge routing that matches the branching in the control loop.
**Consequence, and this is the important part.** LangGraph must not become where the logic
lives. Nodes are thin adapters. Every component under `src/understudy/<component>/` must
be importable, testable and runnable with no LangGraph import anywhere in it. This is what
makes parallel work possible and what stops a framework upgrade from being a rewrite.
**Rejected.** Hand-rolled async state machine (more control, less demo value, operator
overruled), Temporal (excellent durability, an entire extra server in the RAM budget).

### ADR-006 — Component contracts are Pydantic models in a dependency-free package.
**Decision.** `src/understudy/contracts/` contains only Pydantic v2 models and enums. It
imports nothing from the rest of the codebase. Every inter-component call passes contract
types. Contracts are frozen at Phase 0 and changed only by ADR.
**Why.** This is the mechanism that lets two people build in parallel. Person A writes
against `TwinHandle`; person B returns a `TwinHandle`; neither needs the other's code to
exist. It also gives every component a free fake: construct the contract object directly.
**Rejected.** Duck typing, dicts, protobuf (build step for no benefit at this scale).

### ADR-007 — Every component ships a real implementation and a fake, behind a Protocol.
**Decision.** Each component exposes a `typing.Protocol` in its `api.py`, a real
implementation, and a deterministic in-memory fake used by every test outside that
component's own test module.
**Why.** 100% coverage with a cluster in the loop is impossible unless components can be
tested without the cluster. Also lets Phase 1 wire the whole graph end to end with fakes
before any real integration exists, which means integration bugs appear one at a time
instead of all at once at the end.
**Rejected.** Mocking libraries at call sites (brittle, and hides interface drift).

---

## The twin fleet

### ADR-008 — Twin forking is namespace clone at pinned digests, not process checkpointing.
**Decision.** Forking a twin means: read the live `ust-prod` workloads via the Kubernetes
API, extract image digests, resource specs and config, render them into a fresh
`ust-twin-<incident>-<candidate>` namespace, and attach a freshly cloned database.
**Why.** It is the only option in the list that is buildable, debuggable and explicable in
a demo. It reproduces exactly what matters for remediation testing — the same code at the
same version with the same config against the same data shape — and nothing else.
**Rejected.** vcluster (an extra control plane per twin, RAM), CRIU / container checkpoint
(fragile on macOS-hosted Docker, would consume the entire build), full event-sourced
replay (would require the demo services to be event-sourced, which is a different project).
**Honest limitation to state in the brief.** In-flight request state and in-memory caches
are not carried across the fork. Twins start cold. This is disclosed, and the recovery-time
probe's warm-up window (§2.6) exists to stop it from biasing scores.

### ADR-009 — Twin databases are Postgres template clones on one shared instance.
**Decision.** One Postgres instance in `ust-prod` serves production. A second instance,
`twin-postgres` in `ust-system`, holds a `snapshot_template` database refreshed from
production every 60 seconds. A fork issues `CREATE DATABASE twin_<id> TEMPLATE
snapshot_template`, which is a fast filesystem-level copy.
**Why.** N separate Postgres StatefulSets is roughly 250 MB each and slow to start.
Template cloning gives per-twin write isolation, sub-second fork times for demo-sized
datasets, and a single instance to operate.
**Rejected.** One Postgres per twin (RAM, startup latency), shared database with schema
separation (write isolation becomes a convention rather than a boundary), no database
(kills any remediation whose effect is data-visible).
**Constraint this imposes.** `CREATE DATABASE ... TEMPLATE` requires no active connections
to the template. The refresher must therefore restore into a staging database and swap by
rename, never hold an open connection to `snapshot_template`. This is a real trap; it is
called out again in `docs/04-build-plan.md` Phase 2.

### ADR-010 — N = 3 candidates, configurable, fixed for the demo.
**Decision.** `UNDERSTUDY_CANDIDATE_COUNT=3`. The planner emits exactly N candidates. The
number is read from config everywhere; nothing hardcodes 3.
**Why.** Three fits the RAM budget, is enough to make a tournament meaningful, and reads
clearly on a demo scoreboard.
**Rejected.** Planner-chosen N (variable RAM ceiling, harder to demo), N=5 (does not fit),
N=2 (a tournament of two is a coin flip with extra steps).

### ADR-011 — A warm namespace pool is P2, not P0.
**Decision.** Cold forking is the shipped path. A pre-warmed pool of namespaces is an
optimisation deferred to P2 and built only if the demo feels slow.
**Why.** Fork latency does not matter for correctness and the operator confirmed it does
not matter for the demo. Warm pools add lifecycle bugs (stale pool members, leaked
namespaces) that are exactly the kind of thing that breaks live.
**Rejected.** Building it up front.

---

## Traffic mirroring

### ADR-012 — Traffic is mirrored by a purpose-built fan-out gateway, not by a service mesh.
**Decision.** `mirror-gateway` is a component we build. It sits in front of production,
forwards each request synchronously to `ust-prod`, and fans out asynchronous copies to
every registered active twin over bounded per-twin queues.
**Why.** Three reasons, in order of weight. (1) Istio's `mirror` directive supports one
mirror destination per route; fanning out to N twins requires generating N VirtualServices
at fork time and reconciling them, which is more moving parts than writing the fan-out
directly. (2) Istio's control plane plus sidecars across four environments does not fit
comfortably in the RAM budget. (3) We need per-twin drop accounting as a first-class
reliability metric (see K8 and the mirror-fidelity metric), and a mesh does not expose
that in the shape we need.
**Rejected.** Istio (RAM, fan-out awkwardness), Linkerd (lighter, but same fan-out
limitation and still a control plane), Envoy tap filter (correct tool, but configuring and
consuming the tap stream is more work than the gateway itself), goreplay (Go binary,
capture-and-replay rather than live mirroring).
**Honesty requirement.** The brief must describe this as "an application-layer mirroring
gateway", never as "service mesh mirroring". Overclaiming the mesh would be the kind of
thing a judge who builds sandbox infrastructure notices immediately.

### ADR-013 — Mirrored traffic is fire-and-forget with bounded queues and measured drops.
**Decision.** Twin delivery never blocks the production path. Each twin has a bounded
asyncio queue (default 1000). On overflow, drop and increment
`understudy_mirror_dropped_total{twin_id}`. Drop ratio per twin is attached to the
tournament evidence.
**Why.** A mirroring layer that can degrade production is worse than no mirroring layer.
Bounded-and-measured is the only defensible design, and the drop ratio doubles as a
fidelity signal: a candidate whose twin dropped 30% of traffic did not get a fair trial and
must not win.
**Rejected.** Blocking fan-out (production coupling), unbounded queues (memory blowup
under load), sampling (breaks the "all twins see the same traffic" property that makes the
tournament a fair comparison).

### ADR-014 — Twin egress is blocked at three layers.
**Decision.** Twins may not write to production or call external systems. Enforced by:
NetworkPolicy denying egress from `ust-twin-*` to `ust-prod` and to the internet; an
`egress-stub` service in `ust-system` that twin config points all external URLs at; and a
middleware in every demo service that refuses outbound calls when
`UNDERSTUDY_ROLE=twin`. Invariant K6 asserts the policy exists before any fork is accepted.
**Why.** This is the single most dangerous failure mode in the entire design. A twin that
writes to the production database during a rehearsal turns a safety system into an
incident. Three independent layers, one of them machine-checked, is proportionate.
**Rejected.** Any single layer.

### ADR-015 — Load is generated deterministically, and we say so.
**Decision.** `loadgen` replays a fixed, seeded request script at a configured RPS against
the mirror gateway. Same seed, same traffic, every run.
**Why.** Reproducible evaluation is impossible with organic traffic, and every metric in
`docs/05-evaluation.md` depends on runs being comparable. Determinism is a feature here,
not a shortcut.
**Rejected.** Organic or randomised traffic. Disclosed in the brief as a stated
simplification, alongside a note on what would change with real traffic (mirror drop rates
rise, twin–prod correlation intervals widen).

---

## Planning, tournament, playbooks

### ADR-016 — Remediation plans are declarative, typed, and reversible.
**Decision.** The planner never emits shell commands or free-text instructions. It emits a
`RemediationPlan` containing a typed action from a closed enum, its parameters, a declared
target set, a declared blast set, and a declared inverse plan.
**Why.** A closed action space is what makes the kernel's job tractable: you cannot write
an SMT constraint over arbitrary bash. Declared blast sets make K4 checkable. Declared
inverses make K9 checkable and make rollback of a rollback a real capability rather than a
hope.
**Rejected.** Free-form tool-calling, shell execution, LLM-generated manifests.

### ADR-017 — Scoring is deterministic and authoritative; the LLM judge is advisory only.
**Decision.** Two scorers run on identical evidence. `DeterministicScorer` produces the
score that decides the tournament. `LLMJudge` produces an independent ranking that is
recorded, displayed, and never acted upon. Their disagreement rate is a reported metric.
**Why.** The operator asked for both paths. Making the deterministic path authoritative is
what lets us answer "why did it pick that one" with arithmetic. Keeping the LLM path
running gives a genuinely interesting reliability number — how often does an LLM judge
disagree with measurement — which is more useful as evidence than as a decision procedure.
**Rejected.** LLM-only scoring (unauditable), deterministic-only (discards the operator's
requirement and a good metric), averaging the two (produces a number that means nothing).

### ADR-018 — Ambiguity is an outcome, not a tie-break.
**Decision.** The winner must beat the runner-up by at least `AMBIGUITY_MARGIN` (default
0.15 on the normalised composite) *and* have complete evidence. Otherwise the tournament
returns `AMBIGUOUS` and the incident escalates to a human with all candidates attached.
**Why.** A system that always produces an answer is a system that sometimes produces a
confident wrong answer. Escalation precision is a scored metric precisely because
declining to act is a correct behaviour.
**Rejected.** Always picking the top score, random tie-break, re-running the tournament.

### ADR-019 — A playbook match is a candidate, never a shortcut.
**Decision.** Playbook retrieval (pgvector similarity over incident signature embeddings,
then an LLM confirmation step) injects at most one additional candidate into the
tournament. It never bypasses rehearsal, tournament or kernel.
**Why.** The value of the playbook library is that it supplies a *good* candidate quickly,
not that it licenses skipping verification. Skipping verification on a cache hit is how
this class of system fails in production.
**Rejected.** Confidence-thresholded fast path.

### ADR-020 — Shadow-mode hypotheses are LLM-generated from the live dependency graph.
**Decision.** The speculative injector reads the current dependency DAG and service
metadata, asks an LLM to propose failure modes not yet in the corpus, filters them through
a schema validator and a safety filter (no injection targeting `ust-prod`), and runs the
survivors against idle twins.
**Why.** Chosen by the operator, and it is the more original of the two options. A fixed
enumerated list is a test suite; a generator that proposes new failure modes from the
system's own topology is a system that gets better the longer it runs, which is the
compounding-value claim the product makes.
**Rejected.** Fixed enumeration (defensible but inert). Note the tradeoff we accept: the
generator can produce nonsense, so every generated scenario is validated against a schema
and marked `origin: generated` in the corpus, and generated scenarios are reported
separately from seed scenarios in the eval so no metric is inflated by easy self-authored
cases.

---

## Safety kernel

### ADR-021 — Z3 via `z3-solver`, in-process, sub-second.
**Decision.** Invariants are discharged by Z3 inside the agent process, with a hard solver
timeout of 5 seconds. Timeout is treated as UNCERTAIN, which escalates.
**Why.** It is the only formal tool in the candidate set that runs inside a runtime control
loop at incident latency. TLC and Alloy are batch tools; putting either on the critical
path would mean minutes per incident.
**Rejected.** TLA+/TLC as the runtime checker (batch, minutes), Alloy (same), CBMC (checks C
programs; wrong shape entirely).
**Kept as an optional P2 artifact.** A TLA+ specification of the *control loop protocol*
itself — that the loop cannot execute on production without a kernel PASS, cannot execute
two plans concurrently, and always terminates in EXECUTED or ESCALATED — checked offline
with TLC and committed as `formal/protocol.tla`. This is honest formal work at a different
level of the system, and it is clearly labelled as offline.

### ADR-022 — Invariants are written in a Python DSL that compiles to Z3.
**Decision.** One file per invariant under `src/understudy/kernel/invariants/`, each
declaring its id, human statement, required facts, tier (PROOF or RUNTIME), and a
`build(ctx) -> z3.BoolRef` function.
**Why.** Raw SMT-LIB is unreadable to a judge and unmaintainable by an agent. YAML cannot
express quantification. A thin Python DSL keeps the constraint next to its prose statement
and its fact requirements, which is what makes the invariant catalogue in
`docs/03-invariants.md` generatable from code rather than hand-maintained and wrong.
**Rejected.** SMT-LIB files, YAML predicates, runtime assertions only.

### ADR-023 — The kernel reasons over a symbolic pre-state plus the declared diff, with twin observations as bounded facts.
**Decision.** The kernel's model is: facts extracted from the Kubernetes API and GitHub at
incident time, the `RemediationPlan`'s declared diff, and a small set of observation facts
from the tournament (recovery achieved, drop ratio, evidence age). It proves that applying
the diff to the pre-state cannot violate any PROOF-tier invariant.
**Why.** This is the strongest claim that is actually true. Claiming to verify the real
cluster would be false: the model is an abstraction, and the abstraction gap is enumerated
explicitly in `docs/03-invariants.md` §3.6.
**Rejected.** Verifying observed twin outcomes only (that is measurement, not verification,
and would make the kernel redundant with the scorer). Claiming whole-system verification.

### ADR-024 — Kernel output is three-valued and both non-PASS values escalate.
**Decision.** `PASS`, `VETO(invariant_id, unsat_core, human_reason)`, `UNCERTAIN(missing_facts)`.
Only PASS permits production actuation.
**Why.** Two-valued output forces the kernel to guess when facts are missing, and guessing
in the safety component is the worst possible place to guess. Distinguishing "I proved this
is unsafe" from "I could not obtain the facts to decide" is also directly useful in the
escalation message.
**Rejected.** Boolean pass/fail.

---

## Observability, integrations, actuation

### ADR-025 — Prometheus and Loki are the signal source; Datadog is a free-tier mirror for optics.
**Decision.** All observability logic reads through `ObservabilityAdapter`. The primary
implementation is Prometheus (metrics) plus Loki (logs). A Datadog implementation exists
behind the same Protocol and is wired to a free-tier account so the demo can show it, but
nothing depends on it and the eval never uses it.
**Why.** Zero budget, and Datadog's free tier is too rate-limited to sit on a hot control
loop. The adapter keeps the integration story honest — a real Datadog account, really
receiving data — without making the system depend on a metered third party.
**Rejected.** Datadog as primary (rate limits, cost risk), no Datadog at all (loses a
genuinely free integration the brief can claim).

### ADR-026 — PagerDuty is the trigger and the escalation channel; Slack is the narrative channel.
**Decision.** Alerts arrive as PagerDuty webhooks through a local tunnel. Escalations
create or annotate a PagerDuty incident. Slack receives the reasoning post: candidates,
scores, kernel verdict, decision.
**Why.** Matches how on-call actually works and gives two distinct, real integrations
rather than one integration used twice.
**Rejected.** Slack-only (loses the trigger story), polling PagerDuty (webhooks are free
and immediate).

### ADR-027 — GitHub is deploy history and rollback target, via the real API.
**Decision.** The GitHub adapter reads commits, PRs, and the deploy manifest history of the
demo stack from a real repository, and rollback plans reference real commit SHAs and real
image digests.
**Why.** It is the integration most likely to be faked by other submissions, and the least
expensive one to make real.
**Rejected.** Local git only.

### ADR-028 — Production actuation is real, gated, and rate-limited.
**Decision.** The actuator applies the winning plan to `ust-prod` through the Kubernetes
API using a ServiceAccount scoped to that namespace and to the resource kinds the action
enum needs. Invariant K7 caps production mutations per rolling window. Every actuation
writes an immutable run record before and after.
**Why.** "Executes on real production" is a load-bearing claim in the demo; if it is
simulated, the demo is a lie. Making it real and bounded is the only version of this that
survives scrutiny.
**Rejected.** Dry-run mode as the default path.

### ADR-029 — Human-in-the-loop is the escalation path, not the happy path.
**Decision.** On PASS, the agent acts autonomously and notifies. On VETO or UNCERTAIN or
AMBIGUOUS, it does nothing to production and escalates. There is no approval button on the
happy path.
**Why.** An approval gate on every action makes the twin fleet decorative. The product
claim is that rehearsal plus formal veto is what earns autonomy; adding a human gate on top
concedes the claim.
**Rejected.** Slack approve/deny buttons for all actions. (A kill switch —
`UNDERSTUDY_ACTUATION_ENABLED=false` — exists and is a different thing.)

---

## Persistence, evaluation, delivery

### ADR-030 — Postgres with pgvector is the only datastore, and run records are append-only.
**Decision.** One Postgres in `ust-system` holds playbooks, embeddings, run records,
tournament results, kernel verdicts and eval outputs. Run records are append-only; nothing
in the system issues an UPDATE against them.
**Why.** The eval metrics are all queries over run records. Append-only means the evidence
cannot be quietly rewritten by a later phase, which matters because the evidence is the
submission.
**Rejected.** SQLite (concurrent writers from shadow mode and incident loop), separate
vector store, mutable run records.

### ADR-031 — Evaluation is one command, unattended, and its output is committed.
**Decision.** `make eval` runs the whole corpus without human input and writes
`eval/report.json` and `eval/report.md`, both committed to the repo. The submission brief
is generated from the JSON.
**Why.** 25% of the score. A reviewer must be able to reproduce the numbers, and the
numbers in the brief must be traceable to a run rather than typed by a person.
**Rejected.** Manual metric collection, hand-written brief numbers.

### ADR-032 — Proportions are reported as Wilson intervals, and small n is stated.
**Decision.** Every proportion metric reports point estimate, Wilson score interval at 95%,
and n. Where n is small, the report says so in prose next to the number.
**Why.** Wilson behaves correctly at small n and at proportions near 0 or 1, where the
normal approximation produces intervals that extend past the unit interval. Reporting a
tight-looking percentage on n=12 would be the exact failure the reliability criterion is
testing for.
**Rejected.** Bare percentages, normal-approximation intervals, bootstrap (fine, but more
machinery for no gain on binomial proportions).

### ADR-033 — Three-tier cut line, declared before building.
**Decision.** P0 must demo. P1 makes the rubric sing. P2 only if everything else is done.
The membership of each tier is fixed in `docs/04-build-plan.md` §4.2 and does not change
under time pressure without an explicit decision.
**Why.** Deciding what to cut while tired at 2am produces bad cuts. Deciding now produces
good ones.
**Rejected.** Building in dependency order and hoping.

### ADR-034 — OpenRouter is the model-agnostic LLM and embeddings provider.
**Decision.** Understudy uses OpenRouter as its LLM and embeddings provider via its
OpenAI-compatible API (`https://openrouter.ai/api/v1`). The entire runtime is model-agnostic:
model identifiers for candidate generation, advisory judging, shadow hypothesis generation,
and embeddings are configurable via settings and environment variables.
**Why.** OpenRouter provides a unified gateway to diverse foundation models without vendor
SDK lock-in. A model-agnostic architecture ensures that Understudy's safety and reliability
claims (closed action enum, deterministic tournament scoring, Z3 formal kernel) stand on
their own mechanisms rather than on undocumented capabilities or quirks of a specific model.
**Rejected.** Direct single-vendor SDK integration (e.g., Anthropic-only), hardcoding model
identifiers in component code.
