# AGENTS.md

Operating instructions for coding agents working in this repository: Claude Code,
Antigravity, Codex, and anything else with write access.

Read this file completely at the start of every session. It is short on purpose. If a rule
here conflicts with something you infer from the code, this file wins and the code is a bug.

---

## 1. Orientation: read before you write

Before your first edit in a session, read:

1. `CHECKLIST.md` — **start here.** The first unchecked box is your work. It is the state
   of the build; `docs/04-build-plan.md` is the instruction for each box.
2. `docs/00-product.md` — what this is and, more importantly, what it is not (§0.6)
3. `docs/01-decisions.md` — every locked decision. **You may not deviate from an ADR.**
4. `docs/02-architecture.md` — component boundaries, contracts, schemas
5. `docs/04-build-plan.md` — the full detail of the step you picked up
6. The `api.py` of every package you will touch or call

If you are about to write code in `kernel/`, also read `docs/03-invariants.md`.
If you are about to write code in `eval/` or `scenarios/`, also read `docs/05-evaluation.md`.

**Do not read the whole codebase.** Read the contracts and the `api.py` files. That is what
they are for.

## 2. The prime directives

**2.1 Never deviate from an ADR.** `docs/01-decisions.md` is locked. If you believe an ADR
is wrong, stop, open an issue titled `ADR-NNN revisit: <reason>`, and ask the human. Do not
implement the better idea and mention it in the PR description.

**2.2 Never widen the trusted base.** The safety story is: a closed action enum, plus the
invariants in `docs/03-invariants.md`, plus the isolation guarantees. Any change that lets
the LLM decide something the kernel currently constrains is a change to the product's
central claim and requires a human decision.

**2.3 Never fake an integration silently.** See §7.

**2.4 Never touch production from twin or shadow code paths.** No module under
`src/understudy/shadow/` or `src/understudy/fleet/` may reference `ust-prod` except to read.
`Actuator.apply_to_production` is callable only from the orchestrator's `actuate` node.
There is a test asserting this; do not delete it.

**2.5 Leave the checkpoint honest.** A build-plan checkpoint is reached when its command
produces its stated output on a real run. Not when the code "should" produce it. If you
cannot run the checkpoint (no cluster, no secrets), say so explicitly in the PR and mark it
`checkpoint: unverified`.

## 3. Workflow for a unit of work

1. Take the first unchecked box in `CHECKLIST.md`. Mark it `[~]` with your name and branch.
2. Read the corresponding step in `docs/04-build-plan.md`, plus the contracts and `api.py`
   for the packages involved.
3. Write the implementation.
4. Write tests to 100% coverage of the lines you added.
5. Run `make check`. Fix everything it reports. Do not skip, xfail, or `# type: ignore`
   your way past it (§5.4).
6. Run the step's checkpoint command if the environment allows, and tick its assertion
   boxes only for what you actually observed.
7. Tick your step's box in `CHECKLIST.md` **in this PR**, and copy the definition-of-done
   block from the top of that file into the PR body.
8. Commit with a conventional-commit message (always including a commit description
   describing the change), push the branch, open a PR. Never merge the PR; wait for
   human review.
9. Update `docs/` if your change made any of it untrue. A PR that invalidates a document
   and does not update it will be rejected.

Never tick a checkpoint box you did not verify, and never edit a checkpoint's stated
expectation to match what you observed. A mismatch is information; take it to the human
(§8, item 5).

## 4. Branches, commits, PRs

**Branches — Conventional Branch format.**
```
<type>/<short-kebab-description>
feat/fleet-namespace-fork
fix/mirror-queue-overflow-count
docs/invariant-catalogue-regen
test/kernel-k3-negative-cases
chore/pre-commit-detect-secrets
refactor/tournament-scorer-pure
```
Types: `feat`, `fix`, `docs`, `test`, `chore`, `refactor`, `perf`, `build`, `ci`.

**Commits — Conventional Commits, with a scope that is a package name.**
```
feat(fleet): fork twin namespaces at pinned image digests
fix(mirror): count dropped requests when the twin queue is full
test(kernel): add negative case for K3 migration boundary
docs(evaluation): specify the counterfactual protocol
refactor(tournament)!: make the scorer a pure function

BREAKING CHANGE: Tournament.score no longer performs I/O; callers must
pass CandidateEvidence explicitly.
```
Rules:
- Subject in the imperative, lower case, no trailing period, ≤ 72 characters.
- Scope is a package under `src/understudy/` or one of `services`, `deploy`, `docs`, `ci`.
- Always provide a commit description describing the change. Never commit with only a subject line. The body explains *why*, not what. The diff says what.
- One logical change per commit. If the subject needs an "and", split it.
- Reference the build-plan step in the body: `Implements build-plan step A2.3.`
- `!` and a `BREAKING CHANGE:` footer for any contract change.

**PRs.**
- **Never commit to `main`. Never push to `main`. Always open a PR.** No exceptions,
  including for docs and typos.
- **Never auto-merge or merge a PR.** Agents must never merge pull requests or enable
  auto-merge; review and merge are strictly human actions.
- **Merge commits**, not squash, not rebase-merge. History keeps the individual commits.
- PR title follows Conventional Commits, same as a commit subject.
- PR body uses `.github/pull_request_template.md`, which requires:
  - the build-plan step implemented,
  - the checkpoint command and its actual output (or `checkpoint: unverified` with a reason),
  - ADRs relied on,
  - anything mocked, with justification,
  - docs updated (or "none required" with a reason).
- CI (`make check`) must be green. Never merge red. Never use `--no-verify`.
- One PR per build-plan step, unless steps are trivially coupled.

## 5. Code rules

**5.1 Python 3.12, typed, formatted.** `ruff format` and `ruff check` are authoritative;
`mypy --strict` must pass with no ignores. Line length 100.

**5.2 Respect the import boundaries.** Enforced by import-linter in CI:
- `contracts` imports nothing internal.
- `common` imports only `contracts`.
- Only `orchestrator` may import `langgraph`.
- Only `store` may import `psycopg` or `sqlalchemy`.
- Only `fleet`, `actuator` and `graph` may import `kubernetes`.
- No package may import a sibling's internals — only its `api.py` and `contracts`.

If your change needs a new edge in that graph, it needs an ADR.

**5.3 Program against Protocols, never concrete classes.** Every component dependency
arrives through `Deps`. If you find yourself constructing a `FleetController` inside
`tournament/`, stop; you have broken the parallel-build boundary.

**5.4 No silencing.** Forbidden without an inline comment naming the specific reason and a
linked issue: `# type: ignore`, `# noqa`, `# pragma: no cover`, `pytest.mark.skip`,
`pytest.mark.xfail`, broadening a type to `Any`, catching bare `Exception`. Fixing the code
is the default; silencing is an escalation.

**5.5 Errors are typed and specific.** Raise from the `UnderstudyError` hierarchy in
`common/errors.py`. Never raise bare `Exception` or `RuntimeError`. Never swallow an
exception in a component; the orchestrator's error edge exists to handle them.

**5.6 No defaults in the safety path.** In `kernel/`, a missing fact raises `MissingFact`
and produces UNCERTAIN. Never `facts.get(name, some_default)`. A default in the safety
component is a lie with a nice syntax.

**5.7 Determinism where it is claimed.** Anything the eval depends on — load generation,
fault injection, fakes, scenario selection — takes a seed and is reproducible. Never call
`random` without a seeded generator. Never call `datetime.now()` directly; use the `Clock`
from `common`.

**5.8 Logging.** `structlog`, JSON, one event per line, `incident_id` bound at the top of
every loop. Log events are nouns and verbs (`twin_forked`, `kernel_veto`), not sentences.
Never log secrets, tokens, or full request bodies.

**5.9 Async.** The whole runtime is `asyncio`. No blocking I/O in an async function; use
`httpx.AsyncClient`, `psycopg` async, and `asyncio.to_thread` for the Kubernetes client
where a sync call is unavoidable. Every network call has an explicit timeout — there is no
such thing as an un-timed-out call in this codebase.

**5.10 Configuration.** Read from `Settings`. No magic numbers in code: thresholds, weights,
timeouts and ceilings live in `config/*.yaml` and are named in `docs/02-architecture.md`. If
you introduce a tunable, document it there in the same PR.

**5.11 Comments.** Explain why, not what. The place a comment is genuinely required: any
non-obvious invariant, any workaround for external behaviour (name the system and the
behaviour), and every SMT encoding.

## 6. Testing

**6.1 100% line and branch coverage, enforced.** `pytest --cov=understudy
--cov-branch --cov-fail-under=100`. This is a hard gate, not an aspiration.

**6.2 What that means in practice.** 100% coverage is achieved by writing testable code, not
by writing tests for untestable code. If a function is hard to cover, it is doing too much:
separate the I/O from the logic. The scorer, the kernel encoders, the blast calculator, the
plan validator and the metric computations are all pure functions for exactly this reason.

**6.3 Test layers.**
- `tests/unit/` — no network, no cluster, no database. Fakes and fixtures only. Must run in
  under 60 seconds total. This is the suite that gates every PR.
- `tests/integration/` — marked `@pytest.mark.integration`. Needs `make up`. Real Postgres,
  real cluster, real API clients against test namespaces.
- `tests/e2e/` — marked `@pytest.mark.e2e`. Full loop, one scenario, real everything.

**6.4 Tests that must never be deleted or weakened.** These encode the product's claims:
- `test_twin_writes_never_reach_prod`
- `test_twin_egress_denied`
- `test_actuator_requires_pass_verdict` (K10)
- `test_shadow_never_actuates`
- `test_run_store_append_only`
- `test_winner_derives_only_from_deterministic_scores`
- `test_kernel_missing_fact_yields_uncertain`
- every `tests/unit/kernel/test_kNN_*` negative case
If one of these fails, the fix is in the source, never in the test. If you believe such a
test is wrong, stop and ask (§8).

**6.5 Negative tests for invariants assert the counterexample.** Asserting "it vetoed" is
not enough; assert *which* invariant and *why*. A veto for the wrong reason is a bug that a
weak test hides.

**6.6 Property tests where the property is real.** The scorer is monotone in recovery time.
A disqualified candidate never wins. The inverse of an inverse is the original plan. Use
`hypothesis` for these.

**6.7 No test may depend on wall-clock sleeping** beyond 100 ms. Use `FrozenClock`.

## 7. Mocking, faking, and stubbing

The reliability claim collapses if a judge finds a fake integration presented as real.

**7.1 Fakes are legitimate and live in `<package>/fakes.py`.** They implement the same
Protocol, are deterministic given a seed, and are used by unit tests and `ust demo --fake`.

**7.2 Anything faked in a path that is supposed to be real must be marked.** Use the exact
marker so it is greppable:

```python
# MOCKED: Datadog metrics returned from a fixture because the free tier rate-limits
# the hot loop. Real path: signals/datadog.py::DatadogAdapter. Tracked in #47.
```

**7.3 Register it.** Every `# MOCKED:` marker has a row in `docs/MOCKS.md` with the module,
the reason, the real path, and the issue. `make check` runs a script that fails if a marker
exists without a row, or a row without a marker.

**7.4 Never mock in a demo path.** `ust demo --live` and `ust run --live` use real
integrations only. If something cannot be real, the demo says so out loud
(`docs/06-demo.md` §6.7).

**7.5 Never fabricate a number.** Do not write a plausible metric value into a fixture, a
report, a docstring or a doc. Every number in `eval/` and in `docs/08-submission-brief.md`
comes from a run. If you need a placeholder, use an obviously invalid sentinel and a
`# MOCKED:` marker.

## 8. Stop and ask

Halt and ask the human rather than deciding. These are not judgement calls to be
improvised around.

1. You want to deviate from an ADR, or an ADR appears to contradict another.
2. A contract in `src/understudy/contracts/` needs to change. Two people are coding against
   it right now.
3. One of the never-delete tests in §6.4 fails and you cannot see how the source is wrong.
4. You cannot reach 100% coverage without `# pragma: no cover`.
5. A checkpoint's stated expected output differs from what you actually observe. Do not
   adjust the documented expectation to match reality; the discrepancy is information.
6. The work requires a new external dependency, a new external service, or spending money.
   Zero budget is a hard constraint (ADR-001).
7. The work requires more memory than §2.10's budget allows.
8. You need to touch `formal/`, warm pools, or anything else marked P2, and P1 is not
   complete (ADR-033).
9. Ground truth in a scenario appears wrong. Never silently retune it
   (`docs/05-evaluation.md` §5.1).
10. Something in the safety path (`kernel/`, `actuator/production.py`, twin isolation)
    needs a workaround to make a test pass.
11. You are about to delete or weaken a test, an invariant, or an isolation mechanism.
12. You have been blocked on the same failure for more than ~30 minutes of iteration, or
    you are about to attempt the same fix a third time. Report what you tried, what you
    observed, and your best hypothesis. Thrashing quietly is worse than stopping.
13. Secrets are missing or an integration returns auth errors. Never work around it by
    faking the integration.

When you stop, say precisely: what you were doing, what you observed, what you believe the
options are, and which you would choose. Do not stop with an open-ended "how should I
proceed?".

## 9. Things that are never acceptable

- Committing or pushing to `main`.
- Merging a PR or enabling auto-merge (merging is human-only).
- `git push --force` on any shared branch.
- `--no-verify` on commit or push.
- Committing secrets, `.env`, kubeconfigs, tokens, or `config/local.yaml`.
- Commits without a commit description describing the change.
- Editing `eval/report.json` or `eval/report.md` by hand.
- Editing `docs/08-submission-brief.md` by hand.
- Editing generated sections of `docs/03-invariants.md` by hand (use
  `ust kernel catalogue --write`).
- Rewriting `runs` rows, or adding an `UPDATE`/`DELETE` path to `RunStore`.
- Widening a type to make `mypy` pass.
- Deleting a failing test instead of fixing the code.
- Adding a dependency without an ADR line and a `pyproject.toml` group.
- Presenting a simulated result as a measured one, anywhere, ever.

## 10. Environment

```bash
make bootstrap    # uv sync, pre-commit install, k3d cluster, registry
make up           # deploy prod + system, start the agent
make down
make reset        # canonical pre-incident cluster state (used by every eval run)
make check        # ruff + mypy --strict + import-linter + unit tests @ 100% coverage
make check-int    # integration tests (requires make up)
make demo         # scripted demo run
make eval         # full corpus + report
ust doctor        # preflight: docker RAM >= 12GB, tools, secrets
```

Secrets come from `.env` locally (gitignored) or macOS Keychain via `keyring`. Never from
YAML, never from a manifest, never from a commit. `.env.example` lists every required key
and is kept current.

Docker Desktop must be allocated at least 12 GB. `ust doctor` refuses to start otherwise
(ADR-003).

## 11. Working alongside another agent or person

This repository is built by two people in parallel, and possibly by several agents at once.

- Stay inside your build-plan step's packages. If you need something outside them, use its
  `api.py` and its fake; do not implement it yourself.
- If an interface you need does not exist yet, use the Protocol and the fake. The Protocol
  is the promise (ADR-006).
- Rebase your branch on `main` before opening the PR; resolve conflicts in your own branch.
- Never edit another stream's package to "make it work". Open an issue against it.
- If you find a bug outside your packages, file it; fix it only if it blocks you, and say so
  clearly in the PR.
- Assume the other stream's code will land while you work. Depend on contracts, not
  implementations.

## 12. Documentation duties

- Any behaviour change updates the affected doc in the same PR.
- Any new tunable is documented in `docs/02-architecture.md`.
- Any new invariant follows `docs/03-invariants.md` §3.7 and regenerates the catalogue.
- Any new scenario updates the corpus manifest in `docs/05-evaluation.md` §5.1.
- Any decision you were tempted to make silently belongs in `docs/01-decisions.md` as a new
  ADR, proposed to the human first.
- Docs are written in the same register as the rest of this repository: plain, direct,
  mechanism-first. No marketing language, no hedging, no filler.
