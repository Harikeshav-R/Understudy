# Understudy

An incident-response agent that rehearses every fix on a fleet of live production twins,
runs the candidates against each other, and lets a formally specified safety kernel veto
the winner before anything touches production.

Built for the Multi-App AI Agent Hackathon.

---

## The one-paragraph version

Most agentic incident response acts directly on production: the agent's first real-world
test of its own remediation *is* the remediation. Understudy refuses to do that. When an
alert fires, it generates N candidate remediations, forks N isolated twin environments
from synced production state, mirrors live production traffic into all of them in
parallel, scores the candidates against a common invariant set, and then submits the
winner to a Z3-backed safety kernel that can veto it regardless of how well it scored.
Between incidents, the same machinery runs continuously in shadow mode, speculatively
injecting failure modes that have not happened yet so that a validated playbook exists
before it is needed.

## Status

Pre-implementation. Every design decision in `docs/01-decisions.md` is **locked**.
Implementation proceeds phase by phase per `docs/04-build-plan.md`.

## Documentation map

Read in this order if you are new:

| File | What it is | Read when |
|---|---|---|
| `CHECKLIST.md` | Every build step and checkpoint as a tickable box; the state of the build | Start of every working session |
| `OWNERSHIP.md` | Who owns which stream, how that was decided, per-phase split | Before Phase 0 ends; at Phase 6 |
| `docs/00-product.md` | What the product is, the problem, positioning, non-goals | First. Always. |
| `docs/01-decisions.md` | Every locked decision as a numbered ADR, with rejected alternatives | Before proposing any design change |
| `docs/02-architecture.md` | Components, contracts, data flow, schemas, deployment topology | Before writing any code |
| `docs/03-invariants.md` | The safety kernel: invariant catalogue, SMT encoding, veto semantics | Before touching `kernel/` |
| `docs/04-build-plan.md` | Phases, steps, checkpoints, workstream ownership, cut line | Every implementation session |
| `docs/05-evaluation.md` | Metrics, scenario corpus, eval harness, reporting protocol | Before touching `eval/` or `scenarios/` |
| `docs/06-demo.md` | Live demo script, timings, contingency plan | Phase 7 |
| `docs/09-demo-video.md` | The submitted video: shot list, timed script, positioning beat, honesty rules | Phase 7 |
| `docs/07-glossary.md` | Terms used with precise meaning throughout | Whenever a term looks load-bearing |
| `docs/08-submission-brief.md` | The system and reliability brief we submit | Phase 7 |
| `AGENTS.md` | Rules for coding agents: style, tests, commits, stop-and-ask | Every session, before the first edit |

## Quick start

```bash
make bootstrap      # tooling, venv, pre-commit, k3d cluster
make up             # demo stack + observability + Understudy runtime
make demo           # run the scripted incident end to end
make eval           # run the full scenario corpus, emit report
make down
```

Nothing above works until the phase that builds it lands. `docs/04-build-plan.md` states
which checkpoint makes each command real.

## Constraints that shaped everything

- Runs entirely on one M3 Pro MacBook (18 GB RAM). No cloud spend, ever.
- Free-tier only for PagerDuty, Datadog, GitHub, Slack.
- Python everywhere.
- Two people building in parallel, so component boundaries are contracts, not suggestions.

See `docs/01-decisions.md` ADR-001 through ADR-004 for how these constraints were resolved.
