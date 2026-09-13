# CHECKLIST

The single tracking file. Every build-plan step and every checkpoint assertion is one
checkbox. `docs/04-build-plan.md` is the instruction; this is the state.

---

## How to use this file

1. Find the first unchecked box. That is the work.
2. Do it. Follow `AGENTS.md` for how, `docs/04-build-plan.md` for what.
3. Tick the box **in the same PR that does the work**, never before, never in a separate
   "update checklist" commit.
4. A checkpoint assertion is ticked only when you ran the command and saw the stated
   output. If you could not run it, leave it unticked and write
   `checkpoint: unverified (<reason>)` in the PR body. Never tick a box you did not verify.
5. If a checkpoint's stated expectation does not match reality, **stop and ask**
   (`AGENTS.md` §8, item 5). Do not edit the expectation to match what you observed.

Box states, used literally:

```
- [ ] not started
- [~] in progress, owner and branch noted inline
- [x] done, verified, merged
- [-] cut, with the ADR or decision that cut it noted inline
```

Tier tags: **[P0]** must exist or there is no submission. **[P1]** wins the rubric.
**[P2]** only after P1 is complete (ADR-033).

Streams: **A** = Platform, **B** = Reasoning. Do not pick up the other stream's boxes
without agreeing the handoff out loud first.

---

## Definition of done for every step

Copy this block into the PR body for each step. All seven or the step is not done.

```
- [ ] Implementation complete for the named build-plan step
- [ ] Unit tests written; 100% line and branch coverage of the added lines
- [ ] `make check` green (ruff, mypy --strict, import-linter, pytest)
- [ ] Checkpoint command run and output matches, OR marked unverified with a reason
- [ ] Docs updated where this change made them untrue (or "none required", with why)
- [ ] No new `# MOCKED:` marker without a row in docs/MOCKS.md
- [ ] Conventional commit(s), conventional branch, PR opened, never pushed to main
```

---

## Phase 0 — Foundations and contracts  **[P0]**

Both people, one sitting. Nothing else starts until Checkpoint 0 is green.

- [x] 0.1 Repo init: `uv`, Python 3.12, `pyproject.toml` with all dependency groups
- [x] 0.2 `docs/` and `AGENTS.md` and this file committed before any source
- [x] 0.3 `contracts/` written in full per architecture §2.4, frozen, annotated
- [x] 0.3a Round-trip test for every contract model
- [x] 0.4 `common/`: Settings, structlog logger, Clock (+FrozenClock), id generators, error hierarchy, retry
- [x] 0.5 `api.py` Protocol stubs for **every** component in architecture §2.3
- [x] 0.6 `fakes.py` deterministic fake for every Protocol
- [x] 0.7 Tooling: pre-commit, Makefile, import-linter contracts, GitHub Actions
- [x] 0.8 `ust doctor` preflight

**Checkpoint 0**
- [x] `make check` — ruff ok, mypy --strict clean, import-linter contracts kept, coverage 100%
- [x] `ust doctor` exits 0 with all preflight lines OK
- [x] Every Protocol importable; graph deps constructible entirely from fakes
- [ ] CI green on a real PR (not on a local run)

**Gate:** both people can now open files the other will never touch. If that is not true,
Phase 0 is not done.

---

## Phase 1A — Cluster and demo stack (Stream A)  **[P0]**

- [x] A1.1 `deploy/k3d/cluster.yaml`, local registry, `make cluster-up` / `cluster-down`
- [x] A1.2 Four FastAPI demo services + `services/_common/` (metrics, health, role guard)
- [x] A1.3 Fault injection endpoints, all six kinds, with the prod-role guard
- [x] A1.4 `:regression` image variant of data-service with the real N+1 query
- [x] A1.5 `deploy/prod/` manifests with limits per architecture §2.10, `dependencies.yaml`
- [x] A1.6 `deploy/system/`: three Postgres instances, Prometheus, Loki + promtail
- [x] A1.7 `deploy/policies/`: ServiceAccounts, Roles, twin NetworkPolicy template
- [x] A1.8 `services/loadgen/` seeded deterministic generator

**Checkpoint A1**
- [x] `kubectl -n ust-prod get pods` → 4/4 Running, zero restarts
- [x] `curl localhost:8080/api/items` returns items; Prometheus has ≥ 6 targets up
- [x] `make loadgen RPS=20 DURATION=30` → p99 < 400 ms, error rate 0.00
- [x] Latency fault raises p99 above the SLO within 5 s and recovers at TTL
- [x] Container memory with prod only is under 3.5 GB

---

## Phase 1B — Orchestrator skeleton on fakes (Stream B)  **[P0]**

- [x] B1.1 `orchestrator/state.py` with explicit reducers
- [x] B1.2 One node module per node in architecture §2.2; `Deps` injection seam
- [x] B1.3 `build_graph()` with conditional edges, error edge, guaranteed terminal nodes
- [x] B1.4 LangGraph checkpointing into the store, keyed by `incident_id`
- [x] B1.5 `ust demo --fake`

**Checkpoint B1**
- [x] `ust demo --fake --seed 42` traverses all 13 nodes in order, ends `outcome=executed`
- [x] `ust demo --fake --seed 42 --force-veto` ends `outcome=escalated` via the escalate path
- [x] `ust graph --render` produces the diagram used in the demo
- [x] `make check` still at 100% coverage

**Phase 1 gate:** real cluster serving real traffic; fake-backed agent traverses both
terminal paths. Neither stream blocked the other.

---

## Phase 2A — Fleet controller (Stream A)  **[P0]**

- [x] A2.1 Read prod workloads via K8s API, resolve tags to **digests**
- [x] A2.2 Twin manifest rendering: namespace, role, DSN, egress-stub, NetworkPolicy
- [ ] A2.3 Snapshot refresher (staging + rename, never a live connection to the template)
- [ ] A2.3a Twin DB cloner with retry on "source database is being accessed"
- [ ] A2.4 `fork(incident_id, n)` concurrent, 120 s readiness, K6 policy confirmed before ready
- [ ] A2.5 Idempotent teardown by label, `ust fleet gc`, atexit + SIGTERM handlers

**Checkpoint A2**
- [ ] Three twin namespaces Ready within 120 s; three twin databases exist
- [ ] Twin item count equals prod item count at fork time
- [ ] **A write to a twin is invisible in production** ← if this fails, stop everything
- [ ] **Twin cannot reach `ust-prod`** (curl from twin pod times out) ← same
- [ ] Teardown removes all namespaces and all twin databases

---

## Phase 2B — Signals and store (Stream B)  **[P0]**

- [ ] B2.1 `store/` models, migrations, RunStore / PlaybookStore / EvalStore, append-only rules
- [ ] B2.2 PagerDuty webhook receiver, HMAC verification, `ust tunnel`, synthetic-alert fallback
- [ ] B2.3 Prometheus + Loki `ObservabilityAdapter`, namespace-parameterised
- [ ] B2.4 GitHub `DeployHistory` with digests and migration detection
- [ ] B2.5 Datadog adapter conforming to the same Protocol **[P2 to wire live]**
- [ ] B2.6 `graph/`: DAG from declaration, cross-check against observed traffic, blast calculator

**Checkpoint B2**
- [ ] `ust signals deploys --limit 5` returns five real commits with digests and migration flags
- [ ] `ust signals context` returns a valid IncidentContext with real Prometheus and Loki data
- [ ] `ust graph show` prints the DAG and "declared matches observed: OK"
- [ ] A real PagerDuty test incident reaches the receiver within 5 s
- [ ] `test_run_store_append_only` passes against a real Postgres

---

## Phase 3A — Mirror gateway (Stream A)  **[P0]**

- [ ] A3.1 Sync proxy to prod + per-twin bounded queues + drain workers + drop counting
- [ ] A3.2 Registration API and `MirrorStats` endpoint
- [ ] A3.3 Prometheus metrics: delivered, dropped, latency
- [ ] A3.4 `mirror/registry.py` client in the agent
- [ ] A3.5 Loadgen repointed; gateway is the only ingress
- [ ] A3.6 Backpressure behaviour implemented and measured

**Checkpoint A3**
- [ ] Three twins registered; drop ratio < 0.01 at 50 RPS for 60 s
- [ ] `ust mirror compare` shows per-twin counts within 2% of prod, identical path distribution
- [ ] A dead twin does not raise prod p99 by more than 5 ms
- [ ] A dead twin's drop ratio rises toward 1.0 without gateway errors

---

## Phase 3B — Planner and playbooks (Stream B)

- [ ] B3.1 **[P0]** Candidate-generation prompt over the closed action enum
- [ ] B3.2 **[P0]** Parse, validate, retry ≤ 2, then normalise against the live cluster; drop, never repair
- [ ] B3.3 **[P0]** `NO_ACTION` always appended if absent
- [ ] B3.4 **[P0]** Deterministic inverse synthesis per action type (never LLM-generated)
- [ ] B3.5 **[P1]** Playbook signature, embeddings, pgvector retrieval, LLM confirmation
- [ ] B3.6 **[P1]** Playbook write-back with evidence refs and success counters

**Checkpoint B3**
- [ ] `ust plan` returns N+1 plans; every one validates; every one has an inverse except NO_ACTION
- [ ] Every target workload and commit resolves against the live cluster and repo
- [ ] Planner action-type stability across two seeded calls recorded (number goes in the brief)
- [ ] **[P1]** `ust playbook match` returns a match with cosine > 0.8 and a confirmation reason

**Phase 3 gate:** traffic really fans out with measured fidelity; the planner produces
validated, normalised, invertible plans against the real cluster.

---

## Phase 4A — Tournament (Stream A)

- [ ] A4.1 **[P0]** 1 Hz probe, 20 s warm-up exclusion, 15-sample recovery, 180 s timeout
- [ ] A4.2 **[P0]** Blast-radius computation with per-environment pre-apply baselines
- [ ] A4.3 **[P0]** Deterministic scorer, weights from config, disqualification rules
- [ ] A4.4 **[P1]** Advisory LLM judge (no access to deterministic scores)
- [ ] A4.5 **[P0]** Arbiter with the 0.15 ambiguity margin; winner derives only from `scores`

**Checkpoint A4**
- [ ] Property tests pass: monotone in recovery time; disqualified never wins; NO_ACTION can win
- [ ] `tournament replay` on the three-candidate fixture → decided, with margin
- [ ] `tournament replay` on the near-tie fixture → **ambiguous, no winner**
- [ ] `tournament replay` on the high-drop fixture → that candidate disqualified as `evidence_incomplete`

---

## Phase 4B — Safety kernel (Stream B)

- [ ] B4.1 **[P0]** Invariant Protocol + KernelContext; missing facts raise, never default
- [ ] B4.2 **[P0]** Fact extraction from K8s, GitHub, store, graph, evidence; all timestamped
- [ ] B4.3a **[P0]** K1 replica floor (+ positive and negative tests)
- [ ] B4.3b **[P0]** K2 namespace scope (+ tests)
- [ ] B4.3c **[P0]** K3 migration boundary (+ tests) ← the demo's veto depends on this
- [ ] B4.3d **[P0]** K9 reversibility (+ tests)
- [ ] B4.3e **[P1]** K4 blast containment (+ tests)
- [ ] B4.3f **[P1]** K5 single writer (+ tests, atomic claim under transaction)
- [ ] B4.3g **[P1]** K7 mutation budget (+ tests)
- [ ] B4.3h **[P1]** K8 evidence sufficiency and freshness (+ tests)
- [ ] B4.4 **[P0]** `verify()`: assert negation per invariant, 5 s timeout, three-valued verdict
- [ ] B4.5 **[P0]** `ust kernel catalogue --markdown` + the CI test that docs and code agree
- [ ] B4.6 **[P0]** Veto rendered into actionable prose

**Checkpoint B4**
- [ ] Safe rollback plan → `verdict=pass`, solver_ms < 500
- [ ] Rollback across a migration → `verdict=veto invariant=K3`, reason names both commits
- [ ] Scale-to-zero plan → `verdict=veto invariant=K1`
- [ ] Facts missing the migration timestamp → `verdict=uncertain`, missing fact named
- [ ] Catalogue output diffs clean against `docs/03-invariants.md` §3.4

**Phase 4 gate:** the tournament decides from evidence and refuses to decide when it
should not; the kernel proves, vetoes, abstains, and explains each.

---

## Phase 5 — Actuation and end-to-end  **[P0]**

Both streams converge. Pair on this; do not split it.

- [ ] 5.1 `actuator/apply.py`: one idempotent handler per action type, namespace-parameterised
- [ ] 5.2 `apply_to_production` with the K10 assertion, pre-record, post-apply verification, kill switch
- [ ] 5.3 Slack reasoning post with the candidate table
- [ ] 5.4 PagerDuty escalation with full comparative evidence
- [ ] 5.5 Every fake in `Deps` replaced by the real implementation; `--fake` still works
- [ ] 5.6 Node-level timeouts + whole-incident watchdog
- [ ] 5.7 End-to-end wiring test on the real cluster

**Checkpoint 5 — THE P0 GATE.** Run `ust run --scenario seed/bad_deploy_data_service --live`:
- [ ] Real PagerDuty alert received via the tunnel
- [ ] Context contains real Loki signatures and the real regression commit
- [ ] Four candidates generated, one of them the correct rollback
- [ ] Three twin namespaces Ready, three twin databases cloned
- [ ] Mirror drop < 5% on all three twins
- [ ] Scoreboard printed; winner is the rollback; margin > 0.15
- [ ] Kernel `verdict=pass`
- [ ] Production rolled back; prod probe healthy within 180 s; `prod_outcome=resolved`
- [ ] Slack post present with the candidate table
- [ ] All twin namespaces and databases gone
- [ ] Run record written and provably immutable

Then `ust run --scenario seed/bad_deploy_with_migration --live`:
- [ ] Kernel `verdict=veto invariant=K3`
- [ ] **No production change** (verify `ust-prod` untouched)
- [ ] PagerDuty note carries all four candidates with scores and the counterexample

- [ ] Commit tagged `p0-complete`

**From here the submission exists even if everything below fails.**

---

## Phase 6B — Shadow mode (Stream B)  **[P1]**

- [ ] 6.B1 Hypothesis generation from the dependency graph; schema validation; safety filter; dedupe
- [ ] 6.B2 Scheduler holding ≤ 1 idle twin; yields immediately on alert
- [ ] 6.B3 Shadow loop → playbook write with evidence; **never actuates production**
- [ ] 6.B4 `ust shadow run --once` and `ust shadow daemon`

**Checkpoint 6B**
- [ ] A schema-valid scenario appears in `scenarios/generated/`
- [ ] One twin forked and torn down; one playbook written with `origin=shadow` and evidence refs
- [ ] `grep -r apply_to_production src/understudy/shadow/` returns nothing
- [ ] `test_shadow_never_actuates` passes

---

## Phase 6A — Corpus and eval harness (Stream A)  **[P1]**

- [ ] 6.A1 Twelve seed scenarios, three per failure class, ground truth authored before any run
- [ ] 6.A1a At least two scenarios whose correct outcome is **escalate**
- [ ] 6.A2 Eval runner: reset → inject → synthetic alert → live loop → record row
- [ ] 6.A3 Counterfactual runner: reset → re-inject → apply runner-up (through the kernel)
- [ ] 6.A4 Six metrics with Wilson intervals
- [ ] 6.A5 Report renderer: `eval/report.json` + `eval/report.md`, weights and manifest included
- [ ] 6.A6 `make reset` canonical state, idempotent, under 60 s, verified by checksum

**Checkpoint 6A**
- [ ] `ust eval run --dry-run` enumerates 12 scenarios with expected outcomes
- [ ] A full single-repeat pass completes: 12 runs, 12 result rows, zero leaked namespaces
- [ ] `ust eval report` emits both files with every metric carrying n and an interval
- [ ] `eval/report.json` twin–prod correlation has point, low, high, n

---

## Phase 7 — Full evaluation, brief, demo  **[P1]**

- [ ] 7.1 `ust eval run --scenarios all --repeats 3`, expect a fix-and-rerun cycle
- [ ] 7.1a Final `eval/report.json` and `eval/report.md` committed
- [ ] 7.2 `ust brief generate` populates the submission brief from the report
- [ ] 7.3 Demo rehearsed end to end three times on a cold machine, timed under 6:00
- [ ] 7.3a Backup recording captured at demo resolution and layout
- [ ] 7.4 Repo hygiene: README accurate, `.env.example` complete, docs regenerated
- [ ] 7.5 Known-limitations section written from product §0.7 and invariants §3.6, unsoftened

**Checkpoint 7**
- [ ] Fresh clone on a clean path reaches `make demo` with only secrets to fill in
- [ ] `ust brief generate --check` exits 0

---

## P2 — only after every P1 box above is ticked

- [ ] Warm namespace pool
- [ ] `formal/protocol.tla` offline TLA+ spec of the loop protocol, checked with TLC
- [ ] Datadog adapter wired to a live free-tier account and shown in the demo
- [ ] Linkerd or Istio as an alternative mirroring path behind the same Protocol
- [ ] Dependency-graph auto-discovery from traffic instead of declaration

Touching any of these while a P1 box is open is an ADR-033 violation. Reject the PR.

---

## Standing guards — verify these are still true at the end of every phase

- [ ] Phase 1 · [ ] Phase 2 · [ ] Phase 3 · [ ] Phase 4 · [ ] Phase 5 · [ ] Phase 6 · [ ] Phase 7

Tick the phase above once all of the following pass at that phase's end:

- No `# type: ignore`, `# noqa`, `# pragma: no cover`, skip or xfail was added without an
  inline reason and a linked issue
- Coverage still 100%, `mypy --strict` still clean, import-linter contracts still kept
- No `# MOCKED:` marker exists without a `docs/MOCKS.md` row, and vice versa
- None of the never-delete tests were weakened or removed:
  `test_twin_writes_never_reach_prod`, `test_twin_egress_denied`,
  `test_actuator_requires_pass_verdict`, `test_shadow_never_actuates`,
  `test_run_store_append_only`, `test_winner_derives_only_from_deterministic_scores`,
  `test_kernel_missing_fact_yields_uncertain`, all `test_kNN_*` negative cases
- No commit landed on `main` without a PR
- Docs still true; anything invalidated this phase was updated in the same PR

---

## Submission checklist

- [ ] Repository public or judge-accessible, `main` green
- [ ] `README.md` explains the idea in one paragraph and links the doc map
- [ ] `eval/report.md` committed with real numbers, intervals and n
- [ ] `docs/08-submission-brief.md` generated, not hand-edited, `--check` clean
- [ ] Formal coverage reported as the honest ratio, with runtime-tier invariants named
- [ ] Known limitations present and unsoftened in the brief
- [ ] `docs/MOCKS.md` contains nothing in a `--live` path
- [ ] Demo recording uploaded; any recorded segment labelled as recorded
- [ ] Five integrations demonstrably real: GitHub, Kubernetes, Prometheus + Loki, PagerDuty, Slack
- [ ] Nothing in the submission claims more than the evidence supports
