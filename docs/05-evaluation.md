# 05 — Evaluation and Reliability

Reliability and evaluation carries 25% of the score, and it is the judges' own company
thesis. This document specifies what we measure, how, and what would falsify each claim.

The governing principle: **every number in the submission brief is produced by
`make eval` and traceable to run records in the database.** No number is estimated,
extrapolated, or typed by a person (ADR-031).

---

## 5.1 The scenario corpus

`scenarios/seed/` — 12 hand-authored scenarios, 3 per failure class.
`scenarios/generated/` — scenarios produced by shadow mode, schema-validated, reported
separately so no metric is inflated by cases the system wrote for itself.

### Schema

```yaml
id: bad_deploy_data_service
origin: seed                      # seed | generated
failure_class: bad_deploy
description: data-service rolled out with an N+1 query regression in GET /items
injection:
  kind: deploy_image              # deploy_image | fault_endpoint | config_change | scale
  target: data-service
  params: {image_tag: regression}
  settle_seconds: 30
alert_template:
  title: "p99 latency SLO breach on edge-gateway"
  service: edge-gateway
  severity: critical
expected_remediation_class: rollback_deploy   # or "escalate"
expected_escalation: false
ground_truth_note: >
  The only correct fix is rolling data-service back to the pre-regression digest.
  Restart and scale both mask latency briefly and then regress.
```

### Seed corpus manifest

| # | id | class | injection | correct outcome |
|---|---|---|---|---|
| 1 | `bad_deploy_data_service` | bad_deploy | `:regression` image on data-service | rollback |
| 2 | `bad_deploy_auth_cache` | bad_deploy | auth cache poisoning variant | rollback |
| 3 | `bad_deploy_with_migration` | bad_deploy | regression whose rollback crosses a migration | **escalate (K3 veto)** |
| 4 | `pool_exhaustion_data` | resource_exhaustion | connection pool at 1 | scale or config revert |
| 5 | `memory_leak_auth` | resource_exhaustion | leak fault on auth-service | restart |
| 6 | `worker_backlog` | resource_exhaustion | worker stall, queue growth | restart or scale |
| 7 | `latency_data_service` | dependency_degradation | 300 ms injected on data-service | scale |
| 8 | `error_rate_auth` | dependency_degradation | 20% error injection on auth | rollback or flag disable |
| 9 | `hard_down_dependency` | dependency_degradation | data-service scaled to 0 externally | **escalate (no candidate recovers)** |
| 10 | `flag_bad_value` | config_drift | feature flag enabling the slow path | disable_flag |
| 11 | `config_pool_size` | config_drift | ConfigMap pool size set to 1 | revert_config |
| 12 | `flag_plus_latency` | config_drift | flag change coinciding with unrelated latency | disable_flag, with NO_ACTION a strong rival |

Two of twelve have escalation as the correct outcome. That ratio is deliberate: it is high
enough to make escalation precision measurable and low enough that a system which escalates
everything scores badly on the other metrics.

**Ground truth authorship.** `expected_remediation_class` is decided when the scenario is
written, before any run, and is not revised after seeing results. If a run reveals the
ground truth was wrong, the scenario is amended and **every previous result for it is
discarded and re-run**, with the amendment recorded in `scenarios/CHANGELOG.md`. Silently
retuning ground truth to match the system's behaviour would invalidate the whole exercise.

## 5.2 Metric 1 — Twin–prod correlation

**The question.** When a twin says a remediation works, does it work on production?

**Definition.** Over paired observations where the tournament produced a winner and that
winner was applied to production:

```
correlation = P(prod_outcome = resolved | twin predicted recovery)
```

Reported as a Wilson score interval at 95%, with n stated.

**Why Wilson.** At n≈36 and proportions near 0.9, the normal approximation produces
intervals that misrepresent the tail and can exceed 1.0. Wilson does not (ADR-032).

**Also reported, because the single number hides things:**
- The converse: `P(prod resolved | twin predicted no recovery)` — the false-negative side.
- A 2×2 table of twin prediction against production outcome.
- The same number split by `failure_class`, since correlation is almost certainly not
  uniform: `bad_deploy` should correlate well, `resource_exhaustion` less so because twins
  are cold and have different cache states.

**What would falsify the product claim.** A correlation whose interval includes 0.5. If
that happens we report it and say plainly that the twin's verdict is not yet trustworthy at
this sample size, and identify which failure class dragged it down. That is a better
submission than a suspiciously clean 100%.

## 5.3 Metric 2 — Tournament efficiency

**The question.** Did the tournament's winner actually beat the alternatives, or would a
runner-up have done better?

**This requires a counterfactual, and we measure it rather than assume it.** For each
scenario run:

1. Run the loop normally. Winner W is applied to production; record its outcome.
2. `make reset`. Re-inject the identical scenario (deterministic, same seed).
3. Apply the runner-up R directly to production, bypassing the tournament but **not** the
   kernel — if the kernel vetoes R, that is recorded as `runner_up_prod_success = false`
   with reason `vetoed`, which is itself informative.
4. Record R's outcome.

```
tournament_efficiency = P(W succeeded | at least one of {W, R} succeeded)
regret_rate           = P(R succeeded AND W failed)
```

`regret_rate` is the number that matters and the one we lead with. A regret rate near zero
with a non-trivial n is the strongest single piece of evidence that the tournament earns its
cost.

**Cost.** This doubles evaluation wall-clock. Accepted (ADR/Phase 6.A3). The alternative —
inferring what the runner-up "would have done" from its twin score — is circular: it uses
the twin's verdict to validate the tournament that trusts the twin's verdict.

**Limitation, stated in the brief.** We compare winner against runner-up only, not against
all N−1 alternatives, because the run cost scales linearly. The runner-up is the strongest
rival, so this is the tightest comparison available at fixed budget.

## 5.4 Metric 3 — Formal verification coverage

**The question.** How much of the safety kernel is actually machine-checked?

```
proof_coverage = |PROOF-tier invariants| / |all invariants|
```

Currently **8/10** (see `docs/03-invariants.md` §3.5). Reported as the table itself, not as
a bare fraction, with each RUNTIME invariant's reason for being runtime given in one line.

**Also reported:**
- Solver latency distribution (`solver_ms` p50/p99) across all runs — a formal check that
  takes 4.8 seconds is not a runtime check in any useful sense.
- Verdict distribution: how many PASS, VETO, UNCERTAIN across the corpus.
- Per-invariant firing counts: which invariants ever actually vetoed anything. An invariant
  that never fires across the whole corpus is reported as such, honestly, because it means
  either the corpus does not exercise it or it is vacuous.

**What would falsify.** An invariant that fires on every run is probably mis-specified, not
vigilant. A `solver_ms` p99 near the timeout means the encoding is too loose.

## 5.5 Metric 4 — Escalation precision and recall

**The question.** When the system declines to act, was declining right?

Ground truth comes from `expected_escalation` in the corpus.

```
escalation_precision = P(escalation was correct | system escalated)
escalation_recall    = P(system escalated | escalation was correct)
```

Both with Wilson intervals and n.

Both matter and they trade off. Precision alone rewards a system that never escalates;
recall alone rewards one that always does. We report both plus the confusion matrix, and we
report the **false-action rate** — cases where the system acted on production when the
ground truth said escalate — as the single worst outcome class, because it is.

Escalation reasons are broken down by cause: kernel veto, kernel uncertain, tournament
ambiguous, no viable candidate, timeout. A system escalating mostly on timeouts is a
different system from one escalating mostly on vetoes.

## 5.6 Metric 5 — LLM judge agreement (P1)

**The question.** How often does an LLM ranking the same evidence agree with measurement?

```
judge_agreement = P(LLM top-1 = deterministic top-1)
```

Also: Spearman rank correlation between the two orderings across all candidates, and a
breakdown of the disagreements — when the judge disagreed, which one was right on
production?

This metric costs nothing extra (the judge runs on evidence already collected) and it is
genuinely interesting to the audience: it is a small, honest measurement of whether an LLM
evaluator can substitute for instrumentation in this domain. Our prior is that it agrees
often and fails specifically on the disqualification rules, because those are bookkeeping
rather than judgement.

## 5.7 Metric 6 — Mirror fidelity

**The question.** Did the twins actually see production's traffic?

Per twin, per run:
- `drop_ratio = dropped / (delivered + dropped)`
- request-count ratio twin/prod
- path-distribution divergence (total variation distance over endpoint frequencies)
- delivery latency p99 relative to production's own p99

Reported as distributions across all runs, plus the count of candidates disqualified by the
`MIRROR_DROP_CEILING`. This is the metric that keeps the fidelity-gap discussion in
`docs/00-product.md` §0.7 quantitative rather than rhetorical, and it is the fact base for
invariant K8.

## 5.8 The harness

```bash
make reset                                     # canonical pre-incident state
ust eval run --scenarios seed --repeats 3      # 36 runs + 36 counterfactuals
ust eval run --scenarios generated --repeats 1 # shadow-discovered, reported separately
ust eval report                                # writes eval/report.{json,md}
```

**Isolation between runs.** `make reset` restores images to `:good`, replicas to baseline,
ConfigMaps to defaults, the production database from the canonical dump, clears all faults,
and reaps orphaned twins. It is verified by `ust eval verify-reset`, which asserts a
checksum over cluster state. A run that starts from a non-canonical state is discarded, not
adjusted.

**Determinism.** The load generator seed, the scenario injection, and the planner
temperature (0) are fixed. The planner is still an LLM and will vary; that variance is
itself measured and reported as *planner action-type stability* across repeats, rather than
suppressed.

**Failure handling.** A run that errors out is recorded with `outcome=FAILED` and is
included in the denominator of nothing except a separately reported harness-failure rate.
Silently retrying failed runs until they succeed would poison every metric.

## 5.9 Report structure

`eval/report.md` sections, in order:

1. Run manifest — commit SHA, date, corpus version, scoring weights, config snapshot
2. Headline table — six metrics, point estimate, 95% interval, n
3. Twin–prod correlation, with the 2×2 table and the per-class breakdown
4. Tournament efficiency and regret, with the counterfactual protocol restated
5. Formal coverage table, solver latency, per-invariant firing counts
6. Escalation confusion matrix and reason breakdown
7. Judge agreement and disagreement analysis
8. Mirror fidelity distributions
9. Seed versus generated scenario comparison
10. Harness failure rate and every discarded run, with its reason
11. Threats to validity — the enumerated list from `docs/00-product.md` §0.7 and
    `docs/03-invariants.md` §3.6, plus small-n caveats

Section 10 exists because a reliability report that never lost a run is not a reliability
report.

## 5.10 What we do not claim

Written here so that no phase quietly starts claiming it:

- We do not claim the twin is a faithful model of production. We claim we measured how
  faithful it was, on this stack, on this corpus.
- We do not claim the kernel makes the system safe. We claim it discharges eight stated
  invariants over a stated model with a stated abstraction gap.
- We do not claim the corpus is representative of real incidents. Twelve hand-written
  scenarios on a four-service stack are a fixture, not a population.
- We do not claim the numbers generalise beyond n. Every proportion carries its interval
  precisely so that nobody has to take our word for the precision.
