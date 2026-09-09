# 07 — Glossary

Terms used with a precise meaning. If a document, a commit message or a code comment uses
one of these loosely, that is a defect.

**Ambiguous.** A tournament outcome, not a tie-break. Occurs when the winner's margin over
the runner-up is below `AMBIGUITY_MARGIN` or when evidence is incomplete. Always escalates.
See ADR-018.

**Blast radius.** A number in [0,1]: the request-share-weighted count of services in the
mutated workload's dependency closure that measurably degraded after a candidate was
applied. Not a synonym for "how many things it touched". See `docs/02-architecture.md` §2.6.

**Candidate.** One `RemediationPlan` under evaluation in a tournament. `NO_ACTION` is always
a candidate.

**Declared blast set.** The services the planner asserts *in advance* a plan will affect.
Compared against the observed blast set by invariant K4.

**Evidence.** A `CandidateEvidence` object: probes, recovery, blast, drop stats, and a
completeness flag. Evidence is what the tournament scores and what the kernel's freshness
invariant checks. "The agent thinks it worked" is not evidence.

**Evidence complete.** Mirror drop ratio ≤ 5% and probe sample count ≥ 60. Incomplete
evidence disqualifies a candidate rather than penalising it.

**Fan-out gateway.** Our own application-layer mirroring component. **Not** a service mesh.
Never describe it as mesh mirroring (ADR-012).

**Fidelity gap.** The unquantified-in-general difference between a twin's behaviour and
production's. We do not close it; we measure it via twin–prod correlation and bound our
exposure to it via invariant K8. See `docs/00-product.md` §0.7.

**Fork.** Creating a twin: rendering production's current workloads at pinned digests into
a fresh namespace and cloning its database from the current snapshot template. Not a
process checkpoint (ADR-008).

**Incident.** One pass through the control loop, identified by `incident_id`, ending in
EXECUTED, ESCALATED or FAILED. Shadow-mode passes are not incidents; they are shadow runs.

**Invariant.** A property the system must never violate, stated in prose and encoded either
as an SMT constraint (PROOF tier) or as a runtime assertion plus policy (RUNTIME tier). Not
a metric, not a threshold, not a preference.

**Mirror drop ratio.** Fraction of production requests that were not delivered to a given
twin because its bounded queue was full. A fidelity signal, not a bug, and reported per
twin per run.

**PASS / VETO / UNCERTAIN.** The kernel's three verdicts. Only PASS permits production
actuation. UNCERTAIN means facts were missing or the solver did not decide; it is not a soft
pass. See `docs/03-invariants.md` §3.2.

**Playbook.** A stored remediation template with an embedded incident signature and
references to the run records that support it. A playbook match enters a tournament as one
candidate; it never bypasses one (ADR-019).

**PROOF tier.** An invariant discharged by Z3 over the fact model. "Proved" always means
"proved over the model described in `docs/03-invariants.md` §3.3", never "proved about the
cluster".

**Production.** The `ust-prod` namespace and its database. The only place actuation counts,
and the only place the mutation budget applies.

**Regret rate.** The share of runs where the tournament's runner-up would have succeeded on
production and the winner did not. Measured by an actual counterfactual run, never inferred
(`docs/05-evaluation.md` §5.3).

**Run record.** The append-only `RunRecord` row. The evidentiary basis for every number in
the submission. Nothing in the system updates one.

**Shadow mode.** The continuous background loop that generates speculative failure
hypotheses from the dependency graph, rehearses them on idle twin capacity, and writes
playbooks. It never actuates production.

**Signature.** The normalised description of an incident used for playbook retrieval:
failure class plus top error fingerprints plus affected service plus deploy proximity.

**Snapshot template.** The `snapshot_template` database in `twin-postgres`, refreshed from
production every 60 seconds by staging-and-rename, and used as the `TEMPLATE` source for
twin database clones (ADR-009).

**Tournament.** The scored comparison of all candidates on shared evidence. Deterministic
and authoritative; the LLM judge runs alongside it and decides nothing (ADR-017).

**Twin.** One forked environment holding exactly one candidate, in namespace
`ust-twin-<incident>-<candidate>`, with its own cloned database and no egress to production
or the internet.

**Twin–prod correlation.** P(production resolved | twin predicted recovery), reported with a
Wilson interval and n. The single number that says what a twin's verdict is worth.

**Warm-up window.** The first 20 seconds after a fork, excluded from scoring because twins
start cold and cold-start latency is not information about a candidate.
