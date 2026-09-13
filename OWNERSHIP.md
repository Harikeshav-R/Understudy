# OWNERSHIP

Who builds what, decided by evidence rather than preference, and recorded here so that
"who owns this?" never costs a conversation mid-build.

Fill in §1 before Phase 0 ends. Everything after it is already decided.

---

## 1. The assignment (fill this in)

```
Stream A — Platform:     Hari
Stream B — Reasoning:    Rahul
Decided on:              13 Sept 2026
Decided by:              [ ] probe results   [x] prior experience   [ ] both
Probe results:           A-probe: ______ / ______     B-probe: ______ / ______
```

Once written, this holds through Phase 4. See §7 for the one legitimate rebalance point.

---

## 2. What each stream actually demands

Not "backend versus AI". The split is drawn along which scarce skill the work needs.

**Stream A — Platform.** Kubernetes past `kubectl apply`: a pod that will not schedule, a
Service with no endpoints, a NetworkPolicy that must genuinely deny rather than appear to.
Docker image builds and a local registry. Enough Postgres administration for `pg_dump`,
restore, template cloning and the connection-lifecycle trap in ADR-009. Async HTTP
plumbing for the mirror gateway, with backpressure that must not touch production latency.

**Stream B — Reasoning.** Structured LLM work: schema-constrained output, retry-on-invalid,
normalisation against a live system, a LangGraph state machine. Z3 and SMT encoding.
Pure-function discipline and property-based testing. Enough statistics to report a Wilson
interval without misstating what it means.

**The asymmetry that decides it.** Kubernetes is the least substitutable skill in this
build and Z3 is the most learnable. Nine invariants over a small API surface is a day of
reading; a NetworkPolicy that silently fails to deny, or a `CREATE DATABASE ... TEMPLATE`
that fails intermittently because something held a connection open, can cost two days to
someone who has not been there before.

**Therefore:** whoever has genuine Kubernetes exposure takes Stream A. If both do, the one
with more LLM and agent experience takes B. If neither does, the person carrying the larger
share of the build takes A, because A is the P0 critical path.

## 3. The diagnostic

Answer cold. "I have read about it" counts as no.

**Stream A signals**

1. Have you debugged a CrashLoopBackOff, or a Service with no endpoints, on a cluster you owned?
2. Have you written a NetworkPolicy and confirmed it denied something?
3. Have you run a Postgres backup and restore, and managed roles and databases?

**Stream B signals**

4. Have you built with an LLM beyond calling the API — retries on schema failure, validated
   structured output, a multi-step graph?
5. Any exposure to a constraint solver, SAT/SMT, or a proof assistant?
6. Are you comfortable with precision, recall, and confidence intervals?

**Tiebreaker**

7. Which two-hour hunt annoys you less: working out why a pod will not start, or why a
   prompt returns invalid JSON one time in five?

Question 7 is more predictive than it looks. Both streams have long debugging tails; the one
you find tolerable is the one you will still be effective on at hour forty.

## 4. The probes

Since time is not a constraint, buy the information rather than estimate it. Two hours,
both people, in parallel, before Phase 0 ends. This is the project's own thesis applied to
staffing: rehearse before committing, on evidence.

Work in a scratch directory, not the repo. Probe code is thrown away.

### A-probe — isolation on a real cluster

Stand up k3d. Get one FastAPI pod serving HTTP in namespace `probe-a`. Create namespace
`probe-b` with a second pod. Write a NetworkPolicy that blocks `probe-b` from reaching
`probe-a`.

**Acceptance:**

```bash
kubectl -n probe-b exec deploy/client -- curl -s --max-time 3 http://svc.probe-a:8000/healthz
# must time out, non-zero exit — and must have succeeded before the policy was applied
```

You have to demonstrate both states: reachable, then denied. A policy that was never
verified to change anything proves nothing.

**Record:** wall-clock minutes, and whether you needed to look up how NetworkPolicy
selectors work.

### B-probe — a proved veto

Write a standalone Z3 Python script. Model a service with `replicas = 2` and
`min_replicas = 1`. Model a plan that sets replicas to 0. Prove the plan violates the
minimum, and print the counterexample.

**Acceptance:** the script prints `VIOLATION` plus the model values, and printing `SAFE`
for a plan scaling to 3 instead. Roughly thirty lines. Use `z3.Solver`, `push`/`pop`, and
assert the *negation* of the invariant — that pattern is exactly what `kernel/verify.py`
does in Phase 4.

**Record:** wall-clock minutes, and whether the assert-the-negation idea clicked or had to
be looked up.

### Reading the results

Whoever got through the **A-probe** more cleanly takes Stream A. A-probe performance
dominates, because A is the critical path and its failure modes are the expensive ones. If
A-probe times are close, B-probe breaks the tie in the obvious direction.

If **neither** finished the A-probe in two hours, that is the most useful thing the probe
could have told you. Add a Phase 0.5: one day of shared Kubernetes work — get the four demo
services running by hand, break them, fix them — before splitting. Discovering this on day
one costs a day; discovering it at Checkpoint A2 costs the fleet controller.

## 5. Per-phase ownership

| Phase | Stream A owns                                                                          | Stream B owns                                                          | Notes                                                                     |
|-------|----------------------------------------------------------------------------------------|------------------------------------------------------------------------|---------------------------------------------------------------------------|
| 0     | —                                                                                      | —                                                                      | **Both, together.** Contracts, Protocols, fakes, tooling. Non-negotiable. |
| 1     | A1.1–A1.8: cluster, four services, faults, manifests, observability, policies, loadgen | B1.1–B1.5: state, nodes, graph, checkpointing, `--fake` demo           | Fully disjoint                                                            |
| 2     | A2.1–A2.5: fleet controller, DB templating, teardown                                   | B2.1–B2.6: store, PagerDuty, Prometheus/Loki, GitHub, dependency graph | Fully disjoint                                                            |
| 3     | A3.1–A3.6: mirror gateway, registry client, backpressure                               | B3.1–B3.6: planner, validation, inverses, playbooks                    | Fully disjoint                                                            |
| 4     | A4.1–A4.5: probes, blast, scorer, judge, arbiter                                       | B4.1–B4.6: kernel, facts, nine invariants, catalogue                   | Fully disjoint                                                            |
| 5     | —                                                                                      | —                                                                      | **Both, paired.** Actuation and end-to-end. Do not split this.            |
| 6     | 6.A1–6.A6: corpus, eval runner, counterfactual, metrics, report, `make reset`          | 6.B1–6.B4: shadow mode                                                 | Rebalance point — see §7                                                  |
| 7     | —                                                                                      | —                                                                      | **Both.** Eval runs, brief, demo rehearsal, hygiene.                      |

Phases 0, 5 and 7 are shared on purpose. Phase 0 is where the contracts are agreed, Phase 5
is where the streams first touch, and Phase 7 is the submission. Anything gained by
splitting them is lost to reconciliation.

## 6. If the split turns out lopsided

If one person is substantially weaker on both axes, **do not give them Stream A.** Give
them the safety kernel: steps B4.1, B4.2, and B4.3a–B4.3d.

It is the most self-contained work in the build. Nine small files, pure functions, a
fixture in and a verdict out. No cluster, no network, no database, no dependency on
anyone's unwritten code. It is also P0 and it is what the demo's second beat runs on, so it
is real work rather than busywork, and it is the one component someone can finish without
ever being blocked.

The remainder of Stream B — planner, playbooks, signals, orchestrator — then folds into the
stronger person's load alongside A, and Phase 6 rebalances.

## 7. Rebalancing

**One legitimate moment: the start of Phase 6.** By then every contract is frozen and both
6A and 6B start from finished interfaces, so either person can take either. If the split
turned out uneven through Phases 1–4, correct it here and record the change in §1.

**Never rebalance mid-phase.** Handing over half-finished work inside a phase destroys the
parallelism Phase 0 exists to buy, and the receiving person pays the cost of reconstructing
context that was never written down.

**If you are blocked on the other stream**, you are either violating ADR-006 or the phase
boundary is wrong. Say so out loud rather than reaching across. The fix is a Protocol and a
fake, not a peek at someone's branch.

## 8. Working rules across the boundary

- Stay inside your stream's packages. Need something outside them? Use its `api.py` and its
  fake (`AGENTS.md` §11).
- Found a bug in the other stream's code? File it. Fix it only if it blocks you, and say so
  in the PR.
- Contract changes require both people. Two streams are coding against those types right
  now (`AGENTS.md` §8, item 2).
- Review each other's PRs at phase boundaries at minimum. The checkpoint assertions in
  `CHECKLIST.md` are what you are reviewing against, not style.
- `CHECKLIST.md` carries in-progress state: mark your box `[~]` with name and branch when
  you pick it up. That file, not memory, is where "who has this" lives.

## 9. Agent assignment

Claude Code, Antigravity and Codex all work within one stream's packages at a time. An
agent session inherits the stream of the person running it, and the import-linter contracts
in CI are what stop an agent from wandering across the boundary regardless.

Do not run two agents on the same package concurrently. Do run them on the two streams
concurrently — that is the entire point of the Phase 0 contract work.
