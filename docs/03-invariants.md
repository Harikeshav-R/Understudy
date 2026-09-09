# 03 — Invariants and the Safety Kernel

The kernel is the component most likely to be overclaimed and the one a judge whose company
sells verification-adjacent infrastructure is most likely to interrogate. This document
states exactly what it proves, over what model, and where the model stops being the world.

---

## 3.1 What the kernel is

A function:

```
verify(plan: RemediationPlan, facts: list[Fact]) -> KernelVerdict
```

It builds an SMT problem from the facts and the plan's declared diff, asserts the negation
of each PROOF-tier invariant in turn, and asks Z3 for a model. If Z3 returns `unsat`, the
invariant holds for every state consistent with the facts: the invariant is *proved* over
the model. If `sat`, Z3 has produced a counterexample and the kernel vetoes, attaching the
unsat core of the surrounding constraints as the explanation. If `unknown` or the 5-second
timeout fires, the verdict is UNCERTAIN.

RUNTIME-tier invariants are not proved. They are asserted at execution time by the actuator
and observed in the twins; a violation is a hard disqualification in the tournament and a
failure at actuation. They are listed here with the same rigour so the coverage metric in
`docs/05-evaluation.md` §5.4 is honest about the ratio.

## 3.2 Verdict semantics

| Verdict | Meaning | Effect |
|---|---|---|
| `PASS` | Every PROOF invariant discharged `unsat` on its negation; every required fact present | Actuator may apply to `ust-prod` |
| `VETO` | At least one PROOF invariant has a counterexample | No production action. Escalate with the counterexample. |
| `UNCERTAIN` | A required fact is missing, or the solver returned unknown/timed out | No production action. Escalate with the missing-fact list. |

`PASS` is the only verdict that permits actuation, and this is asserted in the actuator
itself, not just in the graph edge — the actuator refuses a plan whose verdict is not PASS
regardless of who calls it. Belt and braces, because this is the one place a graph-wiring
mistake would be catastrophic.

## 3.3 The fact base

Facts are extracted before verification and are the kernel's entire view of the world. A
missing required fact yields UNCERTAIN rather than a default value; defaults in a safety
component are how safety components lie.

| Fact | Source | Type | Used by |
|---|---|---|---|
| `replicas[svc]` | K8s API, `ust-prod` | int | K1 |
| `min_replicas[svc]` | config `slo.yaml` | int | K1 |
| `healthy_replicas[svc]` | K8s API | int | K1 |
| `plan_target_namespaces` | plan | set[str] | K2 |
| `authorized_namespace` | config | str (`ust-prod`) | K2 |
| `last_migration_commit_time` | GitHub | datetime | K3 |
| `rollback_target_commit_time` | GitHub | datetime | K3 |
| `target_contains_migration` | GitHub | bool | K3 |
| `dependents[svc]` | dependency graph | set[str] | K4 |
| `declared_blast_set` | plan | set[str] | K4 |
| `observed_blast_set` | tournament evidence | set[str] | K4 |
| `in_flight_plan_targets` | store (active runs) | set[ResourceRef] | K5 |
| `plan_targets` | plan | set[ResourceRef] | K5 |
| `twin_egress_policy_present` | K8s API | bool | K6 |
| `prod_mutations_in_window` | store | int | K7 |
| `mutation_budget` | config | int | K7 |
| `evidence_age_seconds` | tournament | float | K8 |
| `max_drop_ratio` | tournament | float | K8 |
| `probe_sample_count` | tournament | int | K8 |
| `plan_has_inverse` | plan | bool | K9 |
| `inverse_targets` | plan | set[ResourceRef] | K9 |

## 3.4 Invariant catalogue

Each has an id, a prose statement, a tier, the facts it needs, and its SMT shape. The
catalogue in this document is generated from `src/understudy/kernel/invariants/` by
`ust kernel catalogue --markdown`; if they diverge, the code is authoritative and this
section is stale, which CI will catch (`tests/unit/test_catalogue_matches_docs.py`).

---

### K1 — Replica floor
**Tier:** PROOF
**Statement.** No plan may leave any service with fewer healthy replicas than its
configured minimum, accounting for the plan's own effect.
**SMT shape.**
```
∀ svc ∈ services:
    post_replicas[svc] = replicas[svc] + delta(plan, svc)
    assert post_replicas[svc] ≥ min_replicas[svc]
```
where `delta` is non-zero only for `SCALE_WORKLOAD` on the target, and for
`RESTART_WORKLOAD` is modelled as a transient reduction of `maxUnavailable` from the
Deployment's rolling-update strategy — that transient is what makes restart unsafe during a
partial outage, and modelling it is the point.
**Why it exists.** The obvious "scale down to stop the error storm" candidate is exactly
the kind of empirically-attractive, operationally-fatal fix a tournament will rank highly.

---

### K2 — Namespace scope
**Tier:** PROOF
**Statement.** Every resource a plan mutates lies in the single authorized namespace for
its execution context. A plan destined for production touches only `ust-prod`; a plan
destined for a twin touches only that twin's namespace.
**SMT shape.** `∀ r ∈ plan_targets: namespace(r) = authorized_namespace`
**Why it exists.** This is the machine-checked half of ADR-002. The RBAC boundary would
also stop it, but a plan that *tries* should be vetoed before it is attempted, and the
attempt itself is diagnostic.

---

### K3 — Migration boundary
**Tier:** PROOF
**Statement.** A `ROLLBACK_DEPLOY` may not target a commit that predates the most recent
schema migration, and may not itself be a commit containing a migration.
**SMT shape.**
```
plan.action = ROLLBACK_DEPLOY ⟹
    rollback_target_commit_time ≥ last_migration_commit_time
  ∧ ¬target_contains_migration
```
**Why it exists.** Rolling application code back across a migration leaves code that does
not understand the live schema. It is the classic incident-response own-goal, it will score
beautifully in a twin whose data happens not to exercise the changed column, and no metric
would catch it. This invariant is the clearest single demonstration of why the kernel
exists, and the demo's veto beat (`docs/06-demo.md` §6.4) uses it.

---

### K4 — Blast-radius containment
**Tier:** PROOF
**Statement.** A plan's observed blast set must be a subset of its declared blast set, and
its declared blast set must be a subset of the dependency-graph reachable set of its
targets.
**SMT shape.**
```
observed_blast_set ⊆ declared_blast_set ⊆ ⋃_{t ∈ plan_targets} dependents*(service(t))
```
**Why it exists.** Forces the planner to state its expected impact in advance and makes
"the fix did something we did not predict" a vetoable event rather than a post-mortem
finding. Note the direction: a plan that affects *fewer* services than declared passes;
one that affects services it did not declare does not.

---

### K5 — Single writer
**Tier:** PROOF
**Statement.** No two plans may hold overlapping target resources concurrently, across
incident handling and shadow mode.
**SMT shape.** `plan_targets ∩ in_flight_plan_targets = ∅`
**Why it exists.** Shadow mode runs continuously (ADR-020). Without this, a speculative
injection and a real remediation can collide on the same Deployment. The fact is read from
the store under a transaction that also inserts the claim, so the check and the claim are
atomic.

---

### K6 — Twin egress containment
**Tier:** RUNTIME (policy-backed)
**Statement.** No twin may reach `ust-prod` or the public internet.
**Enforcement.** NetworkPolicy at fork time; `egress-stub` redirection; in-service
middleware. The kernel asserts `twin_egress_policy_present` as a precondition for accepting
any fork, and the fleet controller refuses to mark a twin ready without it.
**Why it is RUNTIME and not PROOF.** Proving a NetworkPolicy denies a flow requires
modelling the CNI's policy semantics, which is a research project. We check that the policy
object exists and matches a golden spec, and we test the denial empirically in
`tests/integration/test_twin_egress_denied.py`. The brief says exactly this.

---

### K7 — Production mutation budget
**Tier:** PROOF (over the counter model) + RUNTIME (enforced at actuation)
**Statement.** At most `mutation_budget` (default 3) production mutations in any rolling
15-minute window.
**SMT shape.** `prod_mutations_in_window + 1 ≤ mutation_budget`
**Why it exists.** An agent in a flapping-alert loop can otherwise remediate the same
service into oblivion. A budget converts a runaway into an escalation.

---

### K8 — Evidence sufficiency and freshness
**Tier:** PROOF
**Statement.** A plan may only be cleared on evidence that is fresh, dense, and
high-fidelity.
**SMT shape.**
```
evidence_age_seconds ≤ MAX_EVIDENCE_AGE (default 300)
∧ probe_sample_count  ≥ MIN_PROBE_SAMPLES (default 60)
∧ max_drop_ratio      ≤ MIRROR_DROP_CEILING (default 0.05)
```
**Why it exists.** This is the invariant that turns the fidelity-gap limitation
(`docs/00-product.md` §0.7) from an acknowledged weakness into a checked precondition. The
kernel refuses to launder a bad rehearsal into a production action.

---

### K9 — Reversibility
**Tier:** PROOF
**Statement.** Every plan except `NO_ACTION` declares an inverse whose target set equals
its own.
**SMT shape.** `plan.action ≠ NO_ACTION ⟹ plan_has_inverse ∧ inverse_targets = plan_targets`
**Why it exists.** If the fix makes things worse, the system must be able to undo exactly
what it did, and no more. Equality rather than subset is deliberate: an inverse that touches
fewer resources leaves partial state behind.

---

### K10 — Actuation authorisation
**Tier:** RUNTIME
**Statement.** The actuator applies to `ust-prod` only when it holds a `KernelVerdict` with
`verdict == PASS` whose `plan_id` matches the plan it is applying and whose age is under 60
seconds.
**Enforcement.** Asserted in `Actuator.apply_to_production`, tested in
`tests/unit/test_actuator_requires_pass.py`, and covered by the offline TLA+ protocol spec
if P2 lands.
**Why it is RUNTIME.** It is a property of the program's control flow, not of the cluster
state, so it belongs to the code and the protocol spec rather than to Z3.

---

## 3.5 Coverage summary

| Tier | Invariants | Count |
|---|---|---|
| PROOF | K1, K2, K3, K4, K5, K7, K8, K9 | 8 |
| RUNTIME | K6, K10 | 2 |

**Reported coverage: 8/10 proof-backed.** This number goes in the brief exactly as it is,
with the two runtime ones named and their reasons given. Inflating it by relabelling K6 as
"proved" would be the single easiest thing for a judge to catch, and the honest 8/10 is a
better number than a suspicious 10/10.

P0 requires K1, K2, K3, K9 proved and K6, K10 enforced. K4, K5, K7, K8 are P1.

## 3.6 The abstraction gap, enumerated

What the kernel proves is a property of a model. Here is precisely where the model departs
from the cluster. Every line of this belongs in the submission brief.

1. **The model is a snapshot.** Facts are read at time T and the plan is applied at T+δ. If
   the cluster changes in between, the proof is about a state that no longer holds. K8's
   freshness bound caps δ; it does not eliminate it.
2. **Kubernetes semantics are modelled, not implemented.** `delta()` for a rolling restart
   uses the Deployment's declared `maxUnavailable`. The real controller may differ under
   node pressure, eviction, or PDB interaction. We model the declared behaviour.
3. **Network policy is not proved (K6).** See above.
4. **The dependency graph is partly declared.** `dependencies.yaml` is authored; `graph`
   cross-checks it against observed traffic and raises if they disagree, but a dependency
   that exists and has never been exercised is invisible to both.
5. **Data-layer effects are out of the model.** The kernel reasons about workloads,
   config and deploy refs. It does not model database state, so a remediation whose harm is
   purely in the data would pass K1–K9. Nothing in this system currently mutates data
   deliberately, which is why the action enum is closed (ADR-016), but that closure is the
   guarantee, not a proof.
6. **The LLM is outside the trusted base entirely.** The planner may propose anything; the
   kernel's job is to make that irrelevant. Every claim of safety in this project rests on
   the closed action space plus K1–K10, never on the planner behaving.

## 3.7 Adding an invariant

1. Create `src/understudy/kernel/invariants/kNN_<slug>.py` implementing the `Invariant`
   protocol: `id`, `tier`, `statement`, `required_facts`, `build(ctx) -> z3.BoolRef`.
2. Add a positive and a negative unit test in `tests/unit/kernel/test_kNN_*.py`. The
   negative test must assert the counterexample Z3 returns, not just that it vetoed.
3. Run `ust kernel catalogue --markdown --write` to regenerate §3.4.
4. If it is PROOF-tier, update §3.5 counts. CI fails if the counts and the code disagree.
