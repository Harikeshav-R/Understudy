# 00 — Product

## 0.1 Name

**Understudy.**

An understudy learns the whole part, rehearses it fully, and goes on only when the
performance actually needs them. That is the system: N candidate remediations rehearse
the incident in isolated twins, one goes on stage, and a director with veto authority can
pull it even after a good rehearsal.

Alternatives considered and rejected: *Prospero* (too oblique, and the storm metaphor
implies the system causes the incident), *Rehearsal* (a noun that reads as a feature, not
a product), *Umbra* (collides with the author's existing Penumbra project).

Naming conventions derived from it:
- Python package: `understudy`
- CLI: `ust`
- Kubernetes namespaces: `ust-prod`, `ust-twin-<incident_id>-<candidate_id>`, `ust-system`
- Metric prefix: `understudy_`
- Env var prefix: `UNDERSTUDY_`
- Log/trace correlation key: `incident_id`

## 0.2 The problem

Agentic incident response, as currently built, has a structural flaw: the agent's plan and
the agent's experiment are the same action. It reads logs, decides on a fix, and applies
that fix to production. If the fix is wrong, the system has just made a live incident
worse, and the evidence that it was wrong arrives only after the damage.

Adding a single staging rehearsal helps and is what a careful team already does manually.
It does not solve the deeper problem, which is that the agent still commits to one guess.
An LLM planner asked to produce a single remediation produces a plausible remediation, not
the best one, and there is no mechanism in the loop that would ever discover a better
alternative existed.

There is a third failure mode underneath both: empirical success in a rehearsal is not the
same as safety. A remediation can resolve the alert, score well on every metric, and still
violate a property the operator would never permit, such as rolling back across a schema
migration or dropping a service below quorum during a partial outage. Metrics do not
encode "never do this." Something else has to.

## 0.3 What Understudy does

Three mechanisms, all inside the agent's own runtime control loop.

**Rehearsal at fleet scale.** Every candidate remediation gets its own twin environment,
forked from the same synced production state, receiving the same live-mirrored production
traffic. The candidates are rehearsed simultaneously, not sequentially, so the comparison
is against a shared traffic sample rather than against different moments in time.

**Empirical arbitration.** Candidates are scored on a single common rubric: invariant
violations, recovery time, blast radius, and downstream impact. The scoring path is
deterministic and auditable. A second, advisory LLM judge scores the same evidence in
parallel; it never decides anything, but the rate at which it disagrees with the
deterministic scorer is itself a reported reliability number.

**Formal veto.** A safety kernel, specified as SMT constraints and discharged by Z3, holds
veto authority over the tournament winner. It reasons over a symbolic model of the
pre-state, the candidate's declared diff, and the twin evidence. It answers PASS, VETO, or
UNCERTAIN. Both VETO and UNCERTAIN route to a human with the full comparative evidence
attached. Empirical performance cannot override it.

**Shadow mode.** Between incidents, idle twin capacity is not idle. A speculative injector
proposes failure modes derived from the live dependency graph, runs them against the
fleet, and files the remediations that worked into a playbook library with their evidence
attached. Over time the library answers "how do you know it works" with an accumulating
body of paired observations rather than a fixed test suite.

## 0.4 Positioning

The judges' company, Arga Labs, sells digital-twin sandbox infrastructure: replicas of
enterprise software that let you test an agent before it ships. Understudy takes the same
premise — agents need a safe place to fail before they act — and moves it from a pre-ship
gate to a permanent organ of the acting agent.

The distinction is not cosmetic. A pre-ship sandbox validates an agent's *policy*, once,
against yesterday's system. A runtime twin fleet validates an agent's *decision*, every
time, against the system as it exists at the moment of the incident. Those catch different
classes of failure. Understudy is a claim about where the sandbox belongs.

We say this plainly in the brief rather than pretending not to have noticed whose
infrastructure thesis we are building on top of.

## 0.5 Who it is for

Any team running production services with an on-call rotation and a deploy history, where
the common remediations are already known (roll back, restart, scale, disable a flag) and
the hard part is choosing correctly under time pressure with incomplete information.

## 0.6 Non-goals

Stated explicitly so no phase drifts into them.

- **Not root-cause analysis.** Understudy chooses a remediation. It does not explain why
  the incident happened beyond what it attaches as context.
- **Not a monitoring product.** It consumes signals from Prometheus and Loki; it does not
  replace them.
- **Not a general chaos-engineering platform.** Fault injection exists to generate
  evaluation scenarios and shadow-mode hypotheses, not as a user-facing feature.
- **Not autonomous on unbounded action space.** The actuator supports exactly the
  remediation classes in §2.7 of `docs/02-architecture.md`. Anything outside that set is
  an escalation by construction.
- **Not multi-tenant, not authenticated, not hardened.** Single operator, local cluster,
  hackathon horizon.
- **Not a fidelity-gap solution.** See §0.7.

## 0.7 The known limitation, stated up front

Forking N twins from the same synced state and feeding them the same mirrored traffic does
not give independent confirmation of a candidate's real-world effect. A defect in the twin
infrastructure itself — a mis-seeded database, a dropped mirror queue, a divergence in the
snapshot — can make several candidates look equally wrong, or equally right, for the same
bad reason. The fleet reduces the variance of picking one bad guess. It does not close the
fidelity gap between a twin and production.

That gap is a genuinely unsolved problem in general. Our response is not to claim we solved
it, but to measure it: the twin–prod correlation metric in `docs/05-evaluation.md` is
exactly the number that tells you how much the twin's verdict is worth, reported with a
confidence interval that widens honestly when the sample is small. Two secondary guards
exist: a mirror-fidelity check that invalidates any candidate whose twin dropped too much
traffic, and an evidence-freshness invariant (K8) that makes the kernel refuse to clear a
plan built on stale or thin evidence.

A demo that hides this is a demo that fails the 25% criterion. A demo that measures it is
the submission.

## 0.8 Success criteria for the build

The project is done when all of the following are true, verifiable by a third party
cloning the repo:

1. `make demo` runs the full incident loop end to end against a real k3d cluster, real
   PagerDuty alert, real GitHub deploy history, real Slack notification, with three twins
   forked live.
2. A second scripted run produces a kernel veto of an empirically winning candidate and
   escalates with comparative evidence.
3. `make eval` runs the full scenario corpus unattended and emits `eval/report.md` plus
   `eval/report.json` containing all six metrics of `docs/05-evaluation.md` with real
   numbers and intervals.
4. `docs/08-submission-brief.md` is populated from that report, not written by hand.
5. Every invariant in `docs/03-invariants.md` is marked PROOF or RUNTIME truthfully, and
   the count matches what the kernel actually discharges.
