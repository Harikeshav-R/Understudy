# 02 — Architecture

This document is the contract between the two workstreams. If code and this document
disagree, one of them is a bug; find out which before continuing.

---

## 2.1 Repository layout

```
understudy/
├── AGENTS.md
├── README.md
├── Makefile
├── pyproject.toml
├── .pre-commit-config.yaml
├── docs/                          # this doc set
├── formal/
│   └── protocol.tla               # P2: offline TLA+ spec of the loop protocol
├── src/understudy/
│   ├── contracts/                 # Pydantic models only. Imports nothing internal.
│   ├── common/                    # config, logging, ids, errors, clock, retry
│   ├── signals/                   # pagerduty, prometheus, loki, datadog, github adapters
│   ├── graph/                     # dependency DAG construction + blast radius
│   ├── planner/                   # candidate generation
│   ├── playbook/                  # library, embeddings, retrieval, confirmation
│   ├── fleet/                     # twin lifecycle: fork, register, teardown
│   ├── mirror/                    # fan-out gateway client + registry
│   ├── tournament/                # probes, deterministic scorer, llm judge, arbiter
│   ├── kernel/                    # invariant DSL, Z3 encoding, veto
│   │   └── invariants/            # one file per invariant
│   ├── actuator/                  # production application of a plan
│   ├── notify/                    # slack, pagerduty escalation
│   ├── shadow/                    # speculative injection loop
│   ├── store/                     # postgres access, migrations, run records
│   ├── orchestrator/              # LangGraph graph, nodes, state
│   ├── eval/                      # harness, metrics, reporting
│   └── cli.py                     # `ust` entrypoint
├── services/                      # the demo stack (four services + mirror-gateway + loadgen + egress-stub)
│   ├── edge_gateway/
│   ├── auth_service/
│   ├── data_service/
│   ├── worker/
│   ├── mirror_gateway/
│   ├── egress_stub/
│   └── loadgen/
├── deploy/
│   ├── k3d/                       # cluster config, registry
│   ├── prod/                      # ust-prod manifests
│   ├── system/                    # ust-system: postgres, prometheus, loki, mirror-gateway
│   ├── twin/                      # twin manifest templates (rendered at fork time)
│   └── policies/                  # RBAC, NetworkPolicies
├── scenarios/                     # chaos corpus: seed/ and generated/
├── eval/                          # report.json, report.md (committed outputs)
└── tests/
    ├── unit/
    ├── integration/               # needs cluster; marked
    └── e2e/                       # needs cluster + full stack; marked
```

## 2.2 The control loop

```
                 ┌─────────────────────── shadow mode (continuous) ───────────────────────┐
                 │  dependency graph → LLM hypothesis → validate → fork idle twin →       │
                 │  inject → candidate → score → file playbook + evidence                 │
                 └───────────────────────────────┬───────────────────────────────────────┘
                                                 │ writes
                                          ┌──────▼──────┐
                                          │  playbook   │
                                          │   library   │
                                          └──────┬──────┘
PagerDuty webhook                                │ reads
        │                                        │
        ▼                                        │
   [ingest] ──► [gather_context] ──► [plan_candidates] ◄┘
                (prometheus, loki,          │ N plans (+1 playbook match)
                 github, dep graph)         ▼
                                       [fork_fleet] ──► N twin namespaces + N cloned DBs
                                            │
                                            ▼
                                    [register_mirrors] ──► mirror-gateway fans out live traffic
                                            │
                                            ▼
                                     [apply_candidates] ──► each plan applied to its own twin
                                            │
                                            ▼
                                        [observe] ──► probes, metrics, drop ratios, blast measurement
                                            │
                                            ▼
                            ┌──── [tournament] ────┐
                            │  deterministic (auth) │
                            │  llm judge (advisory) │
                            └───────────┬───────────┘
                                        │
                        ambiguous ──────┼────── winner
                            │           ▼
                            │      [safety_kernel]  (Z3)
                            │       │        │        │
                            │     PASS    VETO   UNCERTAIN
                            │       │        │        │
                            │       ▼        └───┬────┘
                            │   [actuate]        │
                            │   prod apply       │
                            │       │            │
                            └───────┼────────────┘
                                    ▼            ▼
                              [notify_slack]  [escalate_pagerduty]
                                    │            │
                                    └─────┬──────┘
                                          ▼
                                    [teardown_fleet]
                                          │
                                          ▼
                                    [record_run]  (append-only)
```

Every arrow is a LangGraph edge. Every box is a node that delegates to exactly one
component. `teardown_fleet` and `record_run` run on every terminal path, including failure
paths, via LangGraph's finally-style routing; see §2.9.

## 2.3 Components

Each entry: **owns**, **depends on**, **exposes**. The `exposes` line is the Protocol in
that package's `api.py`, and it is the only thing other packages may import.

| Package | Owns | Depends on (contracts only) | Exposes |
|---|---|---|---|
| `contracts` | All shared types | nothing | — |
| `common` | Config, structured logging, id generation, clock, retry, errors | nothing internal | `Settings`, `get_logger`, `new_incident_id`, `Clock` |
| `store` | Postgres schema, migrations, repositories | contracts | `RunStore`, `PlaybookStore`, `EvalStore` |
| `signals` | PagerDuty webhook receiver, Prometheus/Loki/Datadog readers, GitHub reader | contracts | `AlertSource`, `ObservabilityAdapter`, `DeployHistory` |
| `graph` | Dependency DAG from K8s services + declared manifest, blast-radius computation | contracts | `DependencyGraph`, `BlastRadiusCalculator` |
| `playbook` | Signature embedding, pgvector retrieval, LLM confirmation | contracts, store | `PlaybookLibrary` |
| `planner` | LLM candidate generation, schema validation, plan normalisation | contracts, graph, playbook | `Planner` |
| `fleet` | Twin namespace lifecycle, DB cloning, readiness | contracts | `FleetController` |
| `mirror` | Twin registration with the gateway, drop-stat retrieval | contracts | `MirrorRegistry` |
| `tournament` | SLO probes, recovery timing, deterministic scorer, LLM judge, arbiter | contracts, graph | `Tournament` |
| `kernel` | Fact extraction, invariant DSL, Z3 encoding, verdict | contracts | `SafetyKernel` |
| `actuator` | Applying a plan to a namespace (prod or twin), inverse application | contracts | `Actuator` |
| `notify` | Slack posts, PagerDuty escalation | contracts | `Notifier` |
| `shadow` | Hypothesis generation, validation, idle-capacity scheduling | contracts, fleet, tournament, playbook | `ShadowLoop` |
| `orchestrator` | LangGraph state graph, nodes, wiring, checkpointing | everything above | `build_graph`, `run_incident` |
| `eval` | Corpus runner, metric computation, report rendering | contracts, store | `EvalHarness` |

**Hard rule (ADR-005/006).** No package except `orchestrator` may import `langgraph`. No
package except `store` may import `psycopg`/`sqlalchemy`. No package except `fleet`,
`actuator` and `graph` may import `kubernetes`. Enforced by an import-linter rule in CI.

## 2.4 Contracts

These are the frozen types. They are written here in full because both workstreams code
against them from Phase 0. Types are Pydantic v2 models, `model_config =
ConfigDict(frozen=True)` unless noted.

```python
# contracts/enums.py
class ActionType(StrEnum):
    ROLLBACK_DEPLOY   = "rollback_deploy"    # to a specific commit SHA / image digest
    RESTART_WORKLOAD  = "restart_workload"   # rolling restart
    SCALE_WORKLOAD    = "scale_workload"     # replicas delta
    DISABLE_FLAG      = "disable_flag"       # feature flag off
    REVERT_CONFIG     = "revert_config"      # ConfigMap key to prior value
    NO_ACTION         = "no_action"          # explicit "do nothing" candidate

class FailureClass(StrEnum):
    BAD_DEPLOY            = "bad_deploy"
    RESOURCE_EXHAUSTION   = "resource_exhaustion"
    DEPENDENCY_DEGRADATION = "dependency_degradation"
    CONFIG_DRIFT          = "config_drift"

class KernelVerdictType(StrEnum):
    PASS = "pass"; VETO = "veto"; UNCERTAIN = "uncertain"

class TournamentOutcome(StrEnum):
    DECIDED = "decided"; AMBIGUOUS = "ambiguous"; NO_VIABLE_CANDIDATE = "no_viable_candidate"

class RunOutcome(StrEnum):
    EXECUTED = "executed"; ESCALATED = "escalated"; FAILED = "failed"

class InvariantTier(StrEnum):
    PROOF = "proof"; RUNTIME = "runtime"
```

```python
# contracts/incident.py
class Alert(BaseModel):
    alert_id: str
    source: Literal["pagerduty", "synthetic"]
    title: str
    service: str                  # primary affected service name
    severity: Literal["critical", "error", "warning"]
    fired_at: datetime
    raw: dict[str, Any]

class ErrorSignature(BaseModel):
    fingerprint: str              # stable hash of normalised message
    message: str
    service: str
    count: int
    first_seen: datetime
    last_seen: datetime

class DeployRef(BaseModel):
    commit_sha: str
    image_digests: dict[str, str] # service name -> digest
    deployed_at: datetime
    pr_number: int | None
    contains_migration: bool

class IncidentContext(BaseModel):
    incident_id: str
    alert: Alert
    signatures: list[ErrorSignature]
    metrics_window: MetricWindow
    recent_deploys: list[DeployRef]
    dependency_graph: DependencyGraphSnapshot
    inferred_failure_class: FailureClass | None
    gathered_at: datetime
```

```python
# contracts/plan.py
class ActionParams(BaseModel):
    workload: str                       # e.g. "data-service"
    target_commit: str | None = None    # ROLLBACK_DEPLOY
    replica_delta: int | None = None    # SCALE_WORKLOAD
    flag_name: str | None = None        # DISABLE_FLAG
    config_key: str | None = None       # REVERT_CONFIG
    config_value: str | None = None

class RemediationPlan(BaseModel):
    plan_id: str
    candidate_index: int
    action: ActionType
    params: ActionParams
    target_resources: list[ResourceRef]   # exactly what will be mutated
    declared_blast_set: list[str]         # service names expected to be affected
    inverse: "RemediationPlan | None"     # K9 requires non-null except for NO_ACTION
    rationale: str                        # planner's stated reasoning, for the Slack post
    origin: Literal["planner", "playbook", "shadow"]
    playbook_id: str | None = None

class ResourceRef(BaseModel):
    namespace: str
    kind: Literal["Deployment", "ConfigMap", "Secret"]
    name: str
```

```python
# contracts/twin.py
class TwinHandle(BaseModel):
    twin_id: str
    incident_id: str
    candidate_index: int
    namespace: str
    database: str
    forked_from_snapshot_at: datetime
    ready_at: datetime | None
    state: Literal["forking", "ready", "applied", "observing", "torn_down", "failed"]

class MirrorStats(BaseModel):
    twin_id: str
    delivered: int
    dropped: int
    @property
    def drop_ratio(self) -> float: ...
```

```python
# contracts/evidence.py
class ProbeSample(BaseModel):
    at: datetime
    healthy: bool
    p99_latency_ms: float
    error_rate: float

class CandidateEvidence(BaseModel):
    plan_id: str
    twin_id: str
    applied_at: datetime
    probes: list[ProbeSample]
    recovered: bool
    recovery_seconds: float | None       # None if never recovered
    observed_blast_set: list[str]
    downstream_error_delta: float        # post-apply error rate minus pre-apply, downstream only
    invariant_violations: list[str]      # runtime-tier violations observed in the twin
    mirror_stats: MirrorStats
    evidence_complete: bool              # false if drop ratio or sample count fails fidelity check

class CandidateScore(BaseModel):
    plan_id: str
    composite: float                     # 0.0 best .. 1.0 worst
    components: dict[str, float]         # named subscores, all normalised
    disqualified: bool
    disqualification_reason: str | None

class TournamentResult(BaseModel):
    incident_id: str
    outcome: TournamentOutcome
    scores: list[CandidateScore]          # deterministic, authoritative
    llm_ranking: list[str] | None         # advisory, ordered plan_ids
    llm_agreement: bool | None            # top-1 agreement with deterministic
    winner_plan_id: str | None
    runner_up_plan_id: str | None
    margin: float | None
    decided_at: datetime
```

```python
# contracts/kernel.py
class Fact(BaseModel):
    name: str
    value: Any
    source: Literal["k8s", "github", "tournament", "config", "graph"]
    observed_at: datetime

class InvariantResult(BaseModel):
    invariant_id: str
    tier: InvariantTier
    satisfied: bool | None                # None == could not decide
    unsat_core: list[str] | None
    reason: str

class KernelVerdict(BaseModel):
    incident_id: str
    plan_id: str
    verdict: KernelVerdictType
    results: list[InvariantResult]
    missing_facts: list[str]
    solver_ms: float
    human_reason: str                     # rendered for Slack/PagerDuty
```

```python
# contracts/run.py
class RunRecord(BaseModel):
    run_id: str
    incident_id: str
    scenario_id: str | None               # set when driven by the eval harness
    started_at: datetime
    finished_at: datetime | None
    outcome: RunOutcome
    context: IncidentContext
    plans: list[RemediationPlan]
    evidence: list[CandidateEvidence]
    tournament: TournamentResult | None
    verdict: KernelVerdict | None
    prod_applied_plan_id: str | None
    prod_outcome: Literal["resolved", "not_resolved", "worsened"] | None
    escalation_reason: str | None
```

`prod_outcome` is the field the twin–prod correlation metric is computed from. It is
written by the actuator's post-apply verification, using the same probe logic as the
tournament so twin and prod verdicts are measured identically.

## 2.5 The demo stack

Four Python FastAPI services, hand-written (ADR: default accepted), deliberately small.

| Service | Role | Depends on | Fault knobs |
|---|---|---|---|
| `edge-gateway` | HTTP entry, fans out to auth then data | auth, data | latency, error rate, CPU spin |
| `auth-service` | Token validation: seeded + synthetic tokens in-memory today (`# MOCKED:`, tracked in #16); Postgres-backed lookup not yet built | data-db | latency, error rate, cache poisoning, memory leak |
| `data-service` | CRUD over Postgres, connection pool | postgres | pool exhaustion, latency, error rate, bad-deploy variant |
| `worker` | Polls a job table (`FOR UPDATE SKIP LOCKED`), processes | postgres | stall, backlog growth, crash loop |

Dependency DAG: `edge-gateway → auth-service → data-service`, `edge-gateway → data-service`,
`worker → data-service`. Declared in `deploy/prod/dependencies.yaml` and cross-checked
against observed call patterns by `graph`.

Every service exposes:
- `GET /healthz` — liveness
- `GET /readyz` — readiness, including dependency reachability
- `GET /metrics` — Prometheus
- `POST /admin/fault` — apply a fault spec (guarded: refuses when `UNDERSTUDY_ROLE=prod`
  unless `UNDERSTUDY_FAULT_INJECTION_ENABLED=true`, which the eval harness sets)
- `DELETE /admin/fault` — clear

`error_rate` fault outcomes are drawn from a `random.Random` seeded per process by the
`FAULT_INJECTION_SEED` env var (default `1337`), never the unseeded global `random` module,
so two runs of the same scenario with the same seed inject errors on the same requests
(ADR-015, CLAUDE.md §5.7).

Each service ships two image tags: `:good` and `:regression`. The `:regression` tag of
`data-service` contains a genuine performance regression (an N+1 query in the list
endpoint) so `BAD_DEPLOY` scenarios are real code differences, not simulated ones.

**Feature flags** are ConfigMap keys read at request time (not startup), so
`DISABLE_FLAG` and `REVERT_CONFIG` are meaningful actions with immediate effect.

## 2.6 Probes, recovery, blast radius

**SLO probe.** Every 1 second against the environment's `edge-gateway`:
`healthy := p99_latency_ms < 400 AND error_rate < 0.02 AND readyz == 200`.
Values come from the environment's own Prometheus scrape, not from the probe's own request.

**Warm-up window.** The first 20 seconds after a fork are excluded from scoring. Twins
start cold (ADR-008) and cold-start latency is not information about the candidate.

**Recovery.** Measured from `applied_at`. Recovered when the probe reports healthy for
`RECOVERY_CONSECUTIVE_SECONDS = 15` consecutive samples. Timeout
`RECOVERY_TIMEOUT_SECONDS = 180`, after which `recovered = false`, `recovery_seconds = None`,
and the candidate scores worst-possible on that component rather than being dropped.

**Blast radius.** For a candidate:
```
blast = Σ over services s in reachable_set(target, dep_graph)  request_share(s) * affected(s)
```
where `reachable_set` is the transitive closure of dependents of the mutated workload,
`request_share(s)` is s's share of total requests in the pre-incident window, and
`affected(s)` is 1 if s's error rate or p99 degraded by more than 10% relative to its
pre-apply baseline, else 0. This yields a number in [0, 1].

`observed_blast_set` is `{s : affected(s) = 1}`. Invariant K4 compares it to
`declared_blast_set`.

## 2.7 Deterministic scoring

```
components (each normalised to [0,1], lower is better):
  recovery        = 1.0 if not recovered else recovery_seconds / RECOVERY_TIMEOUT_SECONDS
  blast           = blast radius as computed above
  downstream      = clamp(downstream_error_delta / DOWNSTREAM_ERROR_CEILING, 0, 1)
  violations      = min(runtime_violation_count / VIOLATION_CEILING, 1.0)

composite = 0.40*recovery + 0.25*blast + 0.20*downstream + 0.15*violations

disqualification (composite := 1.0, disqualified := true) if ANY of:
  - evidence_complete == false          (mirror drop ratio > MIRROR_DROP_CEILING = 0.05,
                                         or probe sample count < MIN_PROBE_SAMPLES = 60)
  - twin failed to reach ready state
  - candidate caused a hard invariant violation in the twin
```

Weights live in `config/scoring.yaml`, are read at runtime, and are printed in the eval
report so the reviewer can see them.

`NO_ACTION` is always available as a candidate and is scored identically. If doing nothing
wins, doing nothing wins; that is a correct outcome, and the Slack post says so.

## 2.8 The LLM judge (advisory)

Receives the same `CandidateEvidence` list, serialised, with plan rationales stripped of
which one the deterministic scorer preferred. Returns an ordered ranking with reasons.
Recorded in `TournamentResult.llm_ranking`. `llm_agreement` is top-1 agreement.

It never influences `winner_plan_id`. Guarded by an assertion in `Tournament.arbitrate`
that the winner is derived only from `scores`.

## 2.9 Failure handling in the loop

Every node may raise. The graph has a single error edge to `handle_failure`, which:
1. marks the run `FAILED`,
2. calls `FleetController.teardown_all(incident_id)` (idempotent, safe to call twice),
3. escalates to PagerDuty with the traceback and whatever partial evidence exists,
4. writes the run record.

Teardown is also registered as a process-level `atexit` and signal handler so a killed
agent does not leak namespaces and databases. `ust fleet gc` reaps orphans by label.

Timeouts, all configurable: fork 120s, candidate apply 60s, observation 200s, kernel 5s,
whole incident 600s. Exceeding the incident timeout is an escalation, never a
best-effort action.

## 2.10 Memory budget

Docker Desktop allocated 12 GB of the 18 GB. Budget lines are enforced as Kubernetes
`limits`; a workload exceeding its line is a bug (ADR-003).

| Workload | Count | Limit each | Total |
|---|---|---|---|
| k3s control plane + system | 1 | — | ~900 MB |
| Demo services in `ust-prod` | 4 | 150 MB | 600 MB |
| Demo services per twin | 4 × 3 | 150 MB | 1800 MB |
| `prod-postgres` | 1 | 400 MB | 400 MB |
| `twin-postgres` (all twin DBs) | 1 | 700 MB | 700 MB |
| `system-postgres` (+pgvector) | 1 | 400 MB | 400 MB |
| Prometheus | 1 | 700 MB | 700 MB |
| Loki | 1 | 400 MB | 400 MB |
| `mirror-gateway` | 1 | 250 MB | 250 MB |
| `egress-stub`, `loadgen` | 2 | 100 MB | 200 MB |
| **Cluster total** | | | **≈ 6.4 GB** |
| Understudy agent (host process) | 1 | — | ~600 MB |

Headroom is deliberate: shadow mode may hold twins while an incident arrives, and Phase 6
must not be the first time we discover the ceiling. `ust doctor` checks Docker's
allocation and refuses to start if it is below 12 GB.

## 2.11 Configuration and secrets

- Config: `config/default.yaml`, overridden by `config/local.yaml` (gitignored), overridden
  by `UNDERSTUDY_*` env vars. Loaded once into a frozen `Settings` object in `common`.
- Secrets: `.env` (gitignored) for local, macOS Keychain via `keyring` for anything
  long-lived. Never in YAML, never in manifests, never in a commit. Pre-commit runs
  `detect-secrets`.
- Required secrets: `GITHUB_TOKEN`, `SLACK_BOT_TOKEN`, `PAGERDUTY_ROUTING_KEY`,
  `PAGERDUTY_WEBHOOK_SECRET`, `OPENROUTER_API_KEY`, `DATADOG_API_KEY` (optional).
- LLM and embeddings provider: OpenRouter via OpenAI-compatible endpoints
  (`https://openrouter.ai/api/v1`). The runtime is model-agnostic; models for planning,
  advisory judging, shadow hypothesis generation, and embeddings are read from `Settings`
  (`llm_model`, `embedding_model`, overridable by `UNDERSTUDY_LLM_MODEL`,
  `UNDERSTUDY_EMBEDDING_MODEL`).

## 2.12 Database schema (system-postgres)

```sql
-- append-only
CREATE TABLE runs (
  run_id            TEXT PRIMARY KEY,
  incident_id       TEXT NOT NULL,
  scenario_id       TEXT,
  started_at        TIMESTAMPTZ NOT NULL,
  finished_at       TIMESTAMPTZ,
  outcome           TEXT NOT NULL,
  prod_applied_plan TEXT,
  prod_outcome      TEXT,
  escalation_reason TEXT,
  payload           JSONB NOT NULL      -- full RunRecord
);
CREATE INDEX ON runs (scenario_id);
CREATE INDEX ON runs (started_at DESC);
CREATE RULE runs_no_update AS ON UPDATE TO runs DO INSTEAD NOTHING;
CREATE RULE runs_no_delete AS ON DELETE TO runs DO INSTEAD NOTHING;

CREATE TABLE playbooks (
  playbook_id    TEXT PRIMARY KEY,
  failure_class  TEXT NOT NULL,
  signature_text TEXT NOT NULL,
  embedding      VECTOR(1024) NOT NULL,
  plan           JSONB NOT NULL,        -- RemediationPlan template
  evidence_refs  TEXT[] NOT NULL,       -- run_ids supporting it
  successes      INT NOT NULL DEFAULT 0,
  failures       INT NOT NULL DEFAULT 0,
  origin         TEXT NOT NULL,         -- 'shadow' | 'incident'
  created_at     TIMESTAMPTZ NOT NULL,
  updated_at     TIMESTAMPTZ NOT NULL
);
CREATE INDEX ON playbooks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE scenario_results (
  id            BIGSERIAL PRIMARY KEY,
  scenario_id   TEXT NOT NULL,
  run_id        TEXT NOT NULL REFERENCES runs(run_id),
  repeat_index  INT NOT NULL,
  twin_predicted_success BOOLEAN,
  prod_actual_success    BOOLEAN,
  expected_escalation    BOOLEAN NOT NULL,
  did_escalate           BOOLEAN NOT NULL,
  runner_up_plan_id      TEXT,
  runner_up_prod_success BOOLEAN,       -- counterfactual, see 05 §5.3
  created_at    TIMESTAMPTZ NOT NULL
);
```

Playbook `successes`/`failures` are the only mutable counters in the system, and they live
outside `runs` on purpose.

## 2.13 Observability of Understudy itself

The agent emits its own Prometheus metrics on `:9107`, all prefixed `understudy_`:
`incidents_total{outcome}`, `fork_seconds`, `tournament_seconds`, `kernel_seconds`,
`kernel_verdicts_total{verdict}`, `mirror_dropped_total{twin_id}`,
`llm_judge_agreement_total{agrees}`, `playbooks_total{origin}`.

Structured JSON logs, one line per event, always carrying `incident_id`. This is what makes
the demo's live log pane readable.
