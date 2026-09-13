# 04 — Build Plan

Phase by phase, step by step, with a verification command at every checkpoint.

---

## 4.1 How to read this

- **Workstream A — Platform.** Cluster, demo stack, fleet, mirroring, actuation, chaos.
- **Workstream B — Reasoning.** Contracts, orchestrator, signals, planner, playbook,
  tournament, kernel, eval.

The split is chosen so the two streams touch disjoint directories after Phase 0. Where a
phase needs both, the handoff is a contract type that already exists, never a code
dependency on unwritten code. If you find yourself blocked on the other stream, you are
either violating ADR-006 or the phase boundary is wrong; say so rather than reaching across.

Ownership of A vs B is assigned at Phase 0 and does not change mid-phase.

**Every step ends in a commit.** Every checkpoint is a command you actually run, with the
output you should actually see. A checkpoint that "should work" has not been reached.

**Definition of done for any step:** code + unit tests at 100% coverage of the new lines +
docstrings + type annotations clean under `mypy --strict` + the checkpoint command passes +
conventional-commit PR merged. See `AGENTS.md`.

## 4.2 Cut line

Decided now, in the cold, per ADR-033.

**P0 — must exist or there is no submission.**
Phases 0–5 in full, with: k3d cluster, four demo services, real PagerDuty trigger, real
GitHub deploy history, real Slack notification, N=3 cold-forked twins with cloned
databases, live mirroring through the fan-out gateway, deterministic tournament, kernel with
K1/K2/K3/K9 proved and K6/K10 enforced, real production actuation, escalation path, and a
scripted demo covering one clean resolution and one veto.

**P1 — the difference between a working demo and a winning submission.**
Shadow mode with LLM-generated hypotheses; playbook library and retrieval; invariants
K4/K5/K7/K8; the LLM judge path and agreement metric; full eval harness with all six
metrics and Wilson intervals; the generated submission brief.

**P2 — only with everything above finished.**
Warm namespace pool; TLA+ protocol spec in `formal/`; Datadog adapter wired to a live free
account; Linkerd or Istio as an alternative mirroring path behind the same Protocol;
dependency-graph auto-discovery from traffic instead of declaration.

If a P1 item is at risk, cut the whole item, not half of it. A half-built playbook library
is worse than none: it appears in the architecture diagram and does nothing, which is
exactly the thing judges probe.

## 4.3 Global checkpoint conventions

```bash
make check          # ruff + mypy --strict + import-linter + pytest unit, 100% cov
make check-int      # integration tests, requires `make up`
ust doctor          # environment preflight: docker RAM, k3d, kubectl, secrets present
```

`make check` must pass before every PR. No exceptions, no `--no-verify`.

---

## Phase 0 — Foundations and contracts
**Both streams, together, in one sitting. Nothing else starts until this lands.**

The purpose of this phase is to make the next seven phases parallelisable. Do it properly
or the parallelism is fictional.

### Steps

**0.1** Initialise the repository. `uv init`, Python 3.12, `pyproject.toml` with the
dependency groups: `core` (pydantic, httpx, structlog, tenacity, pyyaml, typer),
`agent` (langgraph, langchain-openai, openai), `k8s` (kubernetes), `db` (psycopg[binary],
sqlalchemy, pgvector), `formal` (z3-solver), `obs` (prometheus-client), `dev` (pytest,
pytest-asyncio, pytest-cov, ruff, mypy, import-linter, detect-secrets, pre-commit).

**0.2** Commit `docs/` as-is and `AGENTS.md`. These are read by every agent session; they
exist before code.

**0.3** Write `src/understudy/contracts/` in full, exactly as specified in
`docs/02-architecture.md` §2.4. All models, all enums, frozen, fully annotated. Add
`tests/unit/contracts/test_contracts_roundtrip.py` asserting every model round-trips
through `model_dump_json` / `model_validate_json`.

**0.4** Write `src/understudy/common/`: `Settings` (layered config per §2.11), `get_logger`
(structlog JSON, `incident_id` bound), `Clock` protocol with `SystemClock` and
`FrozenClock`, id generators (`new_incident_id()` → `inc_<ulid>`, `new_plan_id()`,
`new_twin_id()`), the error hierarchy (`UnderstudyError` → `ConfigError`, `FleetError`,
`KernelError`, `ActuationError`, `EvidenceError`), and `retry()` built on tenacity.

**0.5** Write `api.py` Protocol stubs for **every** component in §2.3, each with full
signatures and docstrings and `raise NotImplementedError`. This is the single most important
step in the phase: after it, both streams can write against every interface they need.

**0.6** Write the deterministic fake for every Protocol under
`src/understudy/<pkg>/fakes.py`. Fakes are importable in tests and in
`ust demo --fake` mode. They must be deterministic given a seed.

**0.7** Tooling: `.pre-commit-config.yaml` (ruff format, ruff check, mypy, detect-secrets,
conventional-commit message hook), `Makefile` targets, `importlinter` contracts encoding the
hard rules in §2.3, GitHub Actions workflow running `make check` on every PR.

**0.8** `ust doctor`: checks Docker Desktop memory ≥ 12 GB, `k3d`/`kubectl`/`uv` present,
required secrets resolvable, Python 3.12.

### Checkpoint 0
```bash
make check
# expect: ruff ok; mypy --strict: no issues; import-linter: contracts kept;
#         pytest: N passed, coverage 100%

ust doctor
# expect: all preflight lines OK, exit 0

python -c "from understudy.orchestrator.api import Orchestrator; print('protocols importable')"
```
**Exit criteria.** Every Protocol exists. Every fake exists. CI is green on a PR. Both
people can now open files that the other will never touch.

---

## Phase 1 — The world (A) and the skeleton loop (B)

### Workstream A — cluster and demo stack

**A1.1** `deploy/k3d/cluster.yaml`: single-server k3d cluster, local registry on :5001,
port mappings for the gateway and mirror gateway. `make cluster-up` / `make cluster-down`.

**A1.2** Build the four demo services in `services/`. One FastAPI app each, shared
`services/_common/` for metrics, health, fault-injection middleware and the
`UNDERSTUDY_ROLE` guard. Keep each service under 200 lines of real logic; they are a
fixture, not a product.

**A1.3** Fault injection: `POST /admin/fault` accepting `{kind, magnitude, ttl_seconds}`
where kind ∈ `latency | error_rate | cpu_spin | memory_leak | pool_exhaustion | stall`.
Implemented as middleware plus per-kind handlers. Guard: refuses in `prod` role unless
`UNDERSTUDY_FAULT_INJECTION_ENABLED=true`.

**A1.4** The `:regression` image variant of `data-service` with a genuine N+1 query in
`GET /items`. Two Dockerfiles or one with a build arg; both tags pushed to the local
registry.

**A1.5** `deploy/prod/`: Deployments, Services, ConfigMaps (including the feature-flag
map), resource requests and limits per the §2.10 budget, `dependencies.yaml`.

**A1.6** `deploy/system/`: `prod-postgres`, `twin-postgres`, `system-postgres` (pgvector
image), Prometheus with scrape configs for all namespaces, Loki plus promtail.

**A1.7** `deploy/policies/`: ServiceAccounts and Roles for `understudy-prod` (Deployments
and ConfigMaps in `ust-prod` only) and `understudy-twin` (full rights in `ust-twin-*`),
plus the twin NetworkPolicy template.

**A1.8** `services/loadgen/`: seeded deterministic request script, configurable RPS,
targets the mirror gateway's address (which in Phase 1 is just the prod gateway; the
gateway arrives in Phase 3).

#### Checkpoint A1
```bash
make cluster-up && make deploy-prod
kubectl -n ust-prod get pods
# expect: 4/4 Running, no restarts

curl -s localhost:8080/api/items | jq '.items | length'      # expect: > 0
curl -s localhost:9090/api/v1/query?query=up | jq '.data.result | length'   # expect: >= 6

make loadgen RPS=20 DURATION=30
# expect: p99 < 400ms, error rate 0.00 in the summary line

curl -sX POST localhost:8081/admin/fault -d '{"kind":"latency","magnitude":300,"ttl_seconds":20}'
# expect: p99 rises above 400ms within 5s, recovers after ttl

free -m equivalent: docker stats --no-stream | awk '{print $4}' | ...
# expect: total container memory under 3.5 GB with prod only
```

### Workstream B — orchestrator skeleton on fakes

**B1.1** `orchestrator/state.py`: the LangGraph state object. It is a Pydantic model
holding `incident_id`, `context`, `plans`, `twins`, `evidence`, `tournament`, `verdict`,
`outcome`, `errors`. Reducers are explicit; no implicit dict merging.

**B1.2** `orchestrator/nodes/`: one module per node in §2.2. Each node is
`async def node(state: State, deps: Deps) -> dict`. `Deps` is a frozen dataclass holding
one instance of every component Protocol — this is the dependency-injection seam that lets
the whole graph run on fakes.

**B1.3** `orchestrator/graph.py`: `build_graph(deps) -> CompiledGraph`. Conditional edges
for tournament outcome and kernel verdict. Error edge to `handle_failure`. Terminal nodes
guarantee `teardown_fleet` and `record_run`.

**B1.4** `orchestrator/checkpoint.py`: LangGraph checkpointing into Postgres via `store`
(fake store for now), keyed by `incident_id`.

**B1.5** `ust demo --fake`: runs a synthetic alert through the entire graph with every
component faked, printing each node transition.

#### Checkpoint B1
```bash
ust demo --fake --seed 42
# expect: node transitions printed in order:
#   ingest -> gather_context -> plan_candidates -> fork_fleet -> register_mirrors ->
#   apply_candidates -> observe -> tournament -> safety_kernel -> actuate ->
#   notify_slack -> teardown_fleet -> record_run
# expect: final line "outcome=executed plan=<plan_id>"

ust demo --fake --seed 42 --force-veto
# expect: ... -> safety_kernel -> escalate_pagerduty -> teardown_fleet -> record_run
# expect: final line "outcome=escalated reason=K3 ..."

ust graph --render docs/generated/graph.png     # visual for the demo
make check                                       # 100% coverage maintained
```
**Exit criteria for Phase 1.** A real cluster serves real traffic; a fake-backed agent
traverses the entire loop including both terminal paths. Neither stream has blocked the
other.

---

## Phase 2 — Fleet (A) and signals (B)

### Workstream A — the fleet controller

**A2.1** `fleet/k8s.py`: read `ust-prod` workloads via the Kubernetes API, extracting image
digests (not tags — resolve to digest), resource specs, env, ConfigMap references.

**A2.2** `fleet/render.py`: produce twin manifests from that snapshot into
`ust-twin-<incident>-<candidate>`, rewriting: namespace, `UNDERSTUDY_ROLE=twin`, database
DSN to the twin's cloned DB, external URLs to `egress-stub`, and applying the twin
NetworkPolicy.

**A2.3** `fleet/database.py`: the snapshot refresher and the cloner.
- Refresher: every 60s, `pg_dump` prod → restore into `snapshot_staging` → `DROP DATABASE
  snapshot_template` → `ALTER DATABASE snapshot_staging RENAME TO snapshot_template`.
  **Never hold a connection to `snapshot_template`** (ADR-009). Record
  `forked_from_snapshot_at`.
- Cloner: `CREATE DATABASE twin_<id> TEMPLATE snapshot_template`, with a retry on the
  "source database is being accessed by other users" error.

**A2.4** `fleet/controller.py`: `fork(incident_id, n) -> list[TwinHandle]` running the N
forks concurrently, waiting for readiness with a 120s timeout, and marking each twin ready
only after its NetworkPolicy is confirmed present (K6 precondition).

**A2.5** `fleet/teardown.py`: idempotent teardown by label selector
`understudy.dev/incident=<id>`, dropping the twin database too. `ust fleet gc` reaps
anything labelled and older than 1 hour. `atexit` and SIGTERM handlers registered.

#### Checkpoint A2
```bash
ust fleet fork --incident inc_test --count 3
# expect: three namespaces ust-twin-inc-test-0..2, all pods Ready within 120s
# (namespace names are DNS-1123 labels, so the incident's underscores become hyphens;
#  the understudy.dev/incident label keeps the raw ID)
kubectl get ns -l understudy.dev/incident=inc_test        # expect: 3

# data isolation
psql -h localhost -p 5433 -c "\l" | grep twin_            # expect: 3 twin databases
curl -s localhost:8090/api/items | jq '.items | length'   # twin 0; expect: equals prod count

# write isolation
curl -sX POST localhost:8090/api/items -d '{"name":"twin-only"}'
curl -s localhost:8080/api/items | jq '[.items[] | select(.name=="twin-only")] | length'
# expect: 0   <-- production must not see the twin's write

# egress denial (the service images are python:3.12-slim, which has no curl)
kubectl -n ust-twin-inc-test-0 exec deploy/edge-gateway -- python -c \
  "import socket; socket.create_connection(('auth-service.ust-prod',8000),3)"
# expect: ConnectionRefusedError / TimeoutError, non-zero exit
# two controls, or the check is vacuous:
#   kubectl -n ust-twin-inc-test-0 exec deploy/edge-gateway -- python -c \
#     "import socket; socket.create_connection(('auth-service',8000),3)"     # expect: succeeds
#   kubectl -n ust-prod exec deploy/worker -- python -c \
#     "import socket; socket.create_connection(('auth-service.ust-prod',8000),3)"  # succeeds
# (prod's edge-gateway Service publishes 8080, not 8000, so it is the wrong probe target)

ust fleet teardown --incident inc_test
kubectl get ns -l understudy.dev/incident=inc_test        # expect: 0
psql -h localhost -p 5433 -c "\l" | grep twin_            # expect: none
```
The write-isolation and egress-denial lines are the two most important assertions in the
entire build. If either fails, stop and fix before any further phase.

### Workstream B — signals and store

**B2.1** `store/`: SQLAlchemy models and Alembic migrations for §2.12, `RunStore` (append
only, with the SQL rules), `PlaybookStore`, `EvalStore`. Integration test against a real
Postgres asserting UPDATE and DELETE on `runs` are no-ops.

**B2.2** `signals/pagerduty.py`: webhook receiver (FastAPI app on :9108), HMAC signature
verification, mapping to `Alert`. A tunnel (`cloudflared`/`ngrok` free tier) exposes it;
`ust tunnel` prints the URL to paste into the PagerDuty webhook config. Fallback:
`ust alert inject --scenario X` produces an identical `Alert` with
`source="synthetic"` so the eval harness never depends on the tunnel.

**B2.3** `signals/prometheus.py` + `signals/loki.py` implementing `ObservabilityAdapter`:
`metric_window(service, since)`, `error_signatures(service, since)` (log grouping by
normalised message hash), `service_health(namespace, service)`. The same adapter is pointed
at a twin namespace by parameter, which is what lets probes reuse it.

**B2.4** `signals/github.py`: `DeployHistory.recent_deploys(limit)` from the real repo —
commits, associated PRs, image digests from the deploy manifest history, and
`contains_migration` by path match on `migrations/`.

**B2.5** `signals/datadog.py`: same Protocol, free-tier account, used only by
`ust demo --with-datadog`. P2, but the file and Protocol conformance land here.

**B2.6** `graph/`: build the dependency DAG from `dependencies.yaml`, cross-check against
observed traffic via Prometheus request labels, expose `dependents(svc)`,
`reachable_set(svc)`, `request_share(svc)`, and `BlastRadiusCalculator` per §2.6.

#### Checkpoint B2
```bash
ust signals deploys --limit 5
# expect: 5 real commits from the GitHub repo, with digests and migration flags

ust signals context --service data-service --minutes 10
# expect: JSON IncidentContext with real Prometheus numbers and Loki signatures

ust graph show
# expect: edge-gateway -> auth-service -> data-service, worker -> data-service,
#         and "declared graph matches observed traffic: OK"

ust tunnel &   # then trigger a test PagerDuty incident
# expect: log line "alert received alert_id=... service=..." within 5s

pytest tests/integration/test_run_store_append_only.py    # expect: passed
```
**Exit criteria for Phase 2.** Twins fork and are provably isolated. Every external signal
is real. The store cannot be rewritten.

---

## Phase 3 — Mirroring (A) and planning (B)

### Workstream A — the mirror gateway

**A3.1** `services/mirror_gateway/`: async FastAPI/httpx service. Synchronous proxy to
`ust-prod` edge-gateway; per-twin `asyncio.Queue(maxsize=1000)`; a worker task per twin
draining its queue with a 2s per-request timeout; drop-and-count on full queue. Headers
`X-Understudy-Shadow: 1`, `X-Understudy-Twin: <twin_id>`, `X-Understudy-Incident: <id>`.

**A3.2** Registration API: `POST /twins` `{twin_id, base_url}`, `DELETE /twins/{twin_id}`,
`GET /twins/{twin_id}/stats` → `MirrorStats`. In-memory registry; the gateway is stateless
across restarts by design, and a restart mid-incident is an incident failure, not a
recovery case.

**A3.3** Prometheus metrics: `understudy_mirror_delivered_total{twin_id}`,
`understudy_mirror_dropped_total{twin_id}`, `understudy_mirror_latency_seconds{target}`.

**A3.4** `mirror/registry.py` in the agent: the `MirrorRegistry` client implementation.

**A3.5** Repoint `loadgen` at the mirror gateway; the gateway becomes the only ingress.

**A3.6** Backpressure test: verify that a twin which stops responding entirely does not
raise production latency by more than 5 ms at p99.

#### Checkpoint A3
```bash
ust fleet fork --incident inc_mirror --count 3
ust mirror register --incident inc_mirror
make loadgen RPS=50 DURATION=60

ust mirror stats --incident inc_mirror
# expect: three rows, delivered ≈ 3000 each, drop_ratio < 0.01

# fidelity
ust mirror compare --incident inc_mirror
# expect: per-twin request count within 2% of prod, path distribution identical

# backpressure
kubectl -n ust-twin-inc_mirror-1 scale deploy/edge-gateway --replicas=0
make loadgen RPS=50 DURATION=30
# expect: prod p99 delta < 5ms; twin-1 drop_ratio rises toward 1.0; no gateway errors
```

### Workstream B — planner and playbook library

**B3.1** `planner/prompt.py`: the candidate-generation prompt. Inputs: `IncidentContext`
serialised, the closed action enum, the dependency graph, the available deploy refs, and
any playbook match. Output: strict JSON, N plans, each with action, params,
declared blast set, inverse, and rationale.

**B3.2** `planner/validate.py`: parse to `RemediationPlan`, reject and re-ask on schema
failure (max 2 retries), then *normalise*: resolve commit refs to real SHAs and digests,
resolve workload names against the live cluster, and compute the declared blast set's
validity against the graph. A plan referencing a nonexistent workload or commit is dropped,
not repaired.

**B3.3** Always append a `NO_ACTION` candidate if the planner did not produce one, so
"doing nothing is best" is always on the ballot.

**B3.4** Inverse synthesis: for each action type, derive the inverse deterministically
(rollback ↔ roll-forward to the pre-incident digest, scale ±delta, flag on/off, config
prior value). Never ask the LLM for the inverse.

**B3.5** `playbook/`: signature text construction (failure class + top error fingerprints +
affected service + deploy proximity), embeddings via the OpenRouter-compatible embedding
path configured in `common`, pgvector cosine retrieval top-k=3, then an LLM confirmation
call that must answer with a single retained id or none. The retained playbook enters as one
extra candidate with `origin="playbook"` (ADR-019).

**B3.6** `playbook/write.py`: after any run whose winner succeeded on prod, upsert a
playbook keyed by signature, appending the `run_id` to `evidence_refs` and incrementing
`successes`.

#### Checkpoint B3
```bash
ust plan --context fixtures/context_bad_deploy.json --count 3
# expect: exactly 4 plans (3 + NO_ACTION); every plan has a non-null inverse except NO_ACTION;
#         every target workload exists in the cluster; JSON validates against RemediationPlan

ust plan --context fixtures/context_bad_deploy.json --count 3 --seed 1 --twice
# expect: report of action-type stability across two calls (not required to be identical,
#         but recorded — this number goes in the brief as planner variance)

ust playbook seed --from fixtures/playbooks_seed.json
ust playbook match --context fixtures/context_bad_deploy.json
# expect: one match with cosine > 0.8 and a confirmation reason

pytest tests/unit/planner -q      # expect: passed, 100% cov
```
**Exit criteria for Phase 3.** Traffic really fans out to three twins with measured
fidelity. The planner produces N validated, normalised, invertible plans against the real
cluster.

---

## Phase 4 — Tournament (A) and kernel (B)

### Workstream A — probes, scoring, arbitration

**A4.1** `tournament/probe.py`: 1 Hz sampler per environment using `ObservabilityAdapter`,
producing `ProbeSample`. Warm-up exclusion of 20s. Recovery detection at 15 consecutive
healthy samples, timeout 180s.

**A4.2** `tournament/blast.py`: implement §2.6's blast computation, pre-apply baselines
captured per environment before the candidate is applied.

**A4.3** `tournament/scorer.py`: the deterministic scorer of §2.7, weights from
`config/scoring.yaml`, disqualification rules included. Pure function over
`list[CandidateEvidence]` — no I/O, trivially testable.

**A4.4** `tournament/judge.py`: the advisory LLM judge (P1). Same evidence, ordered
ranking, no access to deterministic scores.

**A4.5** `tournament/arbiter.py`: apply the ambiguity margin (0.15), produce
`TournamentResult`. Assert the winner derives only from `scores`.

#### Checkpoint A4
```bash
pytest tests/unit/tournament/test_scorer_properties.py
# expect: property tests pass — monotone in recovery time; disqualified candidates never win;
#         NO_ACTION wins when all others are worse

ust tournament replay --fixture fixtures/evidence_three_candidates.json
# expect: scoreboard table, winner, runner-up, margin, outcome=decided

ust tournament replay --fixture fixtures/evidence_near_tie.json
# expect: outcome=ambiguous, no winner

ust tournament replay --fixture fixtures/evidence_high_drop.json
# expect: the high-drop candidate disqualified with reason "evidence_incomplete"
```

### Workstream B — the safety kernel

**B4.1** `kernel/dsl.py`: the `Invariant` Protocol and `KernelContext` holding facts as Z3
constants, with typed accessors that raise `MissingFact` (→ UNCERTAIN) rather than
defaulting.

**B4.2** `kernel/facts.py`: fact extraction from K8s, GitHub, the store, the graph and the
tournament evidence, per §3.3. Every fact stamped with `observed_at`.

**B4.3** `kernel/invariants/`: K1, K2, K3, K9 first (P0), then K4, K5, K7, K8 (P1). One
file each, each with a positive and a negative unit test where the negative test asserts
the specific counterexample.

**B4.4** `kernel/verify.py`: build the solver, assert facts, then for each PROOF invariant
assert its negation in a scope and check. `unsat` → satisfied. `sat` → veto with the model
rendered into `human_reason`. `unknown`/timeout → UNCERTAIN. 5s timeout, tracked in
`solver_ms`.

**B4.5** `kernel/catalogue.py`: `ust kernel catalogue --markdown` regenerating §3.4 of
`docs/03-invariants.md`, plus the CI test asserting docs and code agree.

**B4.6** `kernel/explain.py`: render a veto into prose a human on-call can act on — which
invariant, which fact made it fail, what the counterexample was.

#### Checkpoint B4
```bash
ust kernel verify --plan fixtures/plan_rollback_safe.json --facts fixtures/facts_ok.json
# expect: verdict=pass, 8 invariants (or 4 in P0), solver_ms < 500

ust kernel verify --plan fixtures/plan_rollback_across_migration.json --facts fixtures/facts_ok.json
# expect: verdict=veto invariant=K3
# expect human_reason to name the migration commit and the rollback target time

ust kernel verify --plan fixtures/plan_scale_to_zero.json --facts fixtures/facts_ok.json
# expect: verdict=veto invariant=K1

ust kernel verify --plan fixtures/plan_rollback_safe.json --facts fixtures/facts_missing_migration.json
# expect: verdict=uncertain missing_facts=["last_migration_commit_time"]

ust kernel catalogue --markdown | diff - <(sed -n '/^### K1/,/^## 3.5/p' docs/03-invariants.md)
# expect: no diff
```
**Exit criteria for Phase 4.** The tournament decides from evidence alone and refuses to
decide when it should not. The kernel proves, vetoes, and abstains, and can explain each.

---

## Phase 5 — Actuation and end-to-end (both streams, converging)

This is the first phase where the streams merge. Pair on it; do not split it.

**5.1** `actuator/apply.py`: apply a `RemediationPlan` to a namespace via the Kubernetes
API. One handler per action type. Idempotent. Used for both twins and production, with the
namespace and ServiceAccount as parameters.

**5.2** `actuator/production.py`: `apply_to_production(plan, verdict)`. Asserts K10 (verdict
is PASS, plan_id matches, verdict age < 60s). Writes the pre-actuation run record, applies,
then runs the same probe logic against `ust-prod` for up to 180s to determine
`prod_outcome` ∈ `resolved | not_resolved | worsened`. Kill switch:
`UNDERSTUDY_ACTUATION_ENABLED`.

**5.3** `notify/slack.py`: the reasoning post. Blocks: incident summary, candidate table
with scores, why the winner beat the others, kernel verdict, action taken, mirror fidelity
line, link to the run record. This is a demo artifact as much as a feature; make it
readable.

**5.4** `notify/pagerduty.py`: escalation. Adds a note to the triggering incident with the
full comparative evidence and the kernel's reason, and sets urgency.

**5.5** Replace every fake in `Deps` with the real implementation. Keep `--fake` working;
it is how the unit suite stays fast and how the demo has a fallback.

**5.6** `orchestrator/timeouts.py`: enforce the §2.9 timeouts as node-level wrappers, with
the whole-incident timeout as a watchdog task.

**5.7** End-to-end wiring test on the real cluster.

### Checkpoint 5 (the P0 gate)
```bash
make up
ust run --scenario seed/bad_deploy_data_service --live
# expect, in order, within 10 minutes:
#   - PagerDuty alert received (real, via tunnel)
#   - context gathered with real Loki signatures and the real regression commit
#   - 4 candidates, one of which is rollback to the pre-regression digest
#   - 3 twin namespaces Ready, 3 twin databases created
#   - mirror stats show < 5% drop on all three
#   - scoreboard printed, winner = rollback, margin > 0.15
#   - kernel verdict = pass
#   - production rolled back; prod probe healthy within 180s; prod_outcome=resolved
#   - Slack post present in the channel with the candidate table
#   - all twin namespaces and databases gone
#   - run record written and immutable

ust run --scenario seed/bad_deploy_with_migration --live
# expect: kernel verdict = veto (K3), no production change, PagerDuty note with
#         all four candidates and their scores attached

kubectl get ns | grep ust-twin        # expect: none
ust store verify --last 2             # expect: both run records complete and append-only
```
**Exit criteria.** P0 is complete. The submission is now possible even if everything after
this fails. Tag the commit `p0-complete`.

---

## Phase 6 — Shadow mode (B) and the evaluation harness (A)

### Workstream B — shadow mode

**6.B1** `shadow/hypothesis.py`: read the dependency graph and service metadata, prompt for
failure modes not present in the corpus, validate against the scenario schema, reject
anything targeting `ust-prod` or using an unsupported fault kind, dedupe by embedding
similarity against existing scenarios.

**6.B2** `shadow/scheduler.py`: hold at most `SHADOW_MAX_TWINS` (default 1) twins when no
incident is active; yield immediately on alert arrival (K5 guarantees no target overlap, and
the scheduler additionally cancels shadow work when an incident starts).

**6.B3** `shadow/loop.py`: fork idle twin → inject the hypothesised fault → run the planner →
apply candidates → score → if a candidate recovers, write a playbook with
`origin="shadow"` and the evidence attached. Never actuates production. Ever. Assert it.

**6.B4** `ust shadow run --once` and `ust shadow daemon`.

#### Checkpoint 6B
```bash
ust shadow run --once --seed 7
# expect: a new scenario file in scenarios/generated/, schema-valid;
#         one twin forked and torn down; one playbook written with origin=shadow
#         and non-empty evidence_refs

ust playbook list
# expect: the new playbook, with its failure class and success count

grep -r "apply_to_production" src/understudy/shadow/    # expect: no matches
pytest tests/unit/shadow/test_shadow_never_actuates.py  # expect: passed
```

### Workstream A — chaos corpus and eval harness

**6.A1** `scenarios/seed/`: 12 scenarios, 3 per failure class, each a YAML with `id`,
`failure_class`, `injection` (target service, fault kind, magnitude, ttl), `alert_template`,
`expected_remediation_class`, `expected_escalation` (bool), `notes`. Include at least two
where escalation is the correct answer: one rollback-across-migration, one where no
candidate can recover (a dependency is hard-down).

**6.A2** `eval/runner.py`: for each scenario × `repeats` (default 3): reset the cluster to a
known state, inject, synthesise the alert, run the full loop live, record the
`scenario_results` row including the counterfactual fields.

**6.A3** `eval/counterfactual.py`: after the winner's production outcome is known, apply the
*runner-up* to production in a controlled follow-up (reset, re-inject, apply runner-up
directly), so tournament efficiency is measured against a real counterfactual rather than
assumed. This doubles run time and is worth it; it is the only honest way to compute the
metric. See `docs/05-evaluation.md` §5.3.

**6.A4** `eval/metrics.py`: the six metrics with Wilson intervals.

**6.A5** `eval/report.py`: render `eval/report.json` and `eval/report.md`, including the
scoring weights, the corpus manifest, seed-vs-generated breakdown, and the n for every
proportion.

**6.A6** `make reset`: return the cluster to the canonical pre-incident state
(images at `:good`, replicas at baseline, flags default, database restored from the
canonical dump, faults cleared). Idempotent, under 60 seconds. Everything in Phase 6
depends on this being reliable.

#### Checkpoint 6A
```bash
make reset && ust eval run --scenarios seed --repeats 1 --dry-run
# expect: 12 scenarios enumerated with their expected outcomes, no execution

make reset && ust eval run --scenarios seed --repeats 1
# expect: 12 runs complete; 12 scenario_results rows; no leaked namespaces;
#         wall clock under ~2 hours

ust eval report
# expect: eval/report.md with all six metrics, each carrying n and a Wilson interval
cat eval/report.json | jq '.metrics.twin_prod_correlation'
# expect: {point: 0.xx, low: 0.xx, high: 0.xx, n: 12}
```
**Exit criteria for Phase 6.** The system improves itself between incidents, and the
evidence for every claim we intend to make is produced by one command.

---

## Phase 7 — Full evaluation, brief, demo (both)

**7.1** Full eval: `ust eval run --scenarios all --repeats 3`. Expect this to surface real
bugs; budget for a fix-and-rerun cycle. Commit the final `eval/report.json` and
`eval/report.md`.

**7.2** `ust brief generate` populates `docs/08-submission-brief.md` from `eval/report.json`.
Prose is templated; numbers are injected. No number in the brief is typed by hand
(ADR-031).

**7.3** Rehearse the demo per `docs/06-demo.md` end to end at least three times, on a cold
machine, timed. Record a backup screen capture of a successful run.

**7.4** Repository hygiene: README accurate, `make bootstrap` works from a clean clone,
`.env.example` complete, docs regenerated, `AGENTS.md` current.

**7.5** Write the known-limitations section of the brief from `docs/00-product.md` §0.7 and
`docs/03-invariants.md` §3.6. Do not soften it.

### Checkpoint 7
```bash
git clone <repo> /tmp/fresh && cd /tmp/fresh
make bootstrap && make up && make demo
# expect: a stranger's machine reaches the same demo, with only secrets to fill in

ust brief generate --check
# expect: "brief is current with eval/report.json" and exit 0
```

---

## 4.4 Risk register

| Risk | Signal it is happening | Response |
|---|---|---|
| RAM ceiling hit at N=3 twins | Pods OOMKilled during Phase 2 or 3 | Drop demo services in twins to 1 replica; drop Loki retention; last resort N=2 with the ADR amended in writing |
| `CREATE DATABASE ... TEMPLATE` blocked by connections | Fork fails intermittently | Already handled by rename-swap; if it persists, use `pg_dump`/restore per twin and accept slower forks |
| PagerDuty free tier rate-limits the eval | 429s during Phase 6 | Eval uses `source="synthetic"` alerts by default; the real webhook path is exercised in the demo only |
| LLM planner produces invalid plans repeatedly | High reject rate in Phase 3 checkpoint | Tighten the prompt with a worked example per action type; never loosen the validator |
| Mirror gateway becomes the latency bottleneck | prod p99 rises with twins registered | Profile; move fan-out to a background task group; if still bad, ADR-004's rejected Go option is reopened by ADR amendment |
| Counterfactual eval doubles runtime past patience | Phase 6 wall clock | Reduce repeats to 2 and report the smaller n honestly; never estimate the counterfactual |
| Scope creep into P2 before P1 is done | Any PR touching `formal/` before Phase 7 | Reject the PR, cite ADR-033 |
