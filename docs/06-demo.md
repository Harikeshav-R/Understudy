# 06 — Demo

Target length: **6 minutes**. Two beats, one of which is a failure the system catches. The
second beat is the one that wins the criterion, so it gets the better half of the time.

Demo clarity is 10% of the rubric, but the demo is also how the other 90% is observed. A
judge who cannot follow the demo cannot award technical execution.

---

## 6.1 Screen layout

One screen, four panes, fixed for the whole demo. No window switching, no scrolling to find
things.

```
┌──────────────────────────────┬──────────────────────────────┐
│ (1) Understudy log           │ (2) Cluster view             │
│     structured, incident_id  │     watch on namespaces      │
│     colourised               │     + pods across ust-*      │
├──────────────────────────────┼──────────────────────────────┤
│ (3) Tournament scoreboard    │ (4) Grafana: prod p99 +      │
│     live TUI, updates as     │     error rate, one panel,   │
│     evidence arrives         │     large, annotated         │
└──────────────────────────────┴──────────────────────────────┘
```

Slack and PagerDuty are shown briefly on a fifth surface (phone or second screen) at the
moments they receive something, then dropped. Do not narrate a browser tab hunt.

Pane 3 is the visual centrepiece. Build it as a real TUI (`rich.Live`) reading the run
state, not a screenshot. It is the thing that makes the tournament legible in two seconds.

## 6.2 Pre-flight (before recording, not on camera)

```bash
make reset && ust eval verify-reset     # must print CANONICAL
make up
ust doctor                              # all green
ust tunnel                              # PagerDuty webhook live
make loadgen RPS=30 DURATION=900 &      # steady traffic for the whole demo
```

Wait 60 seconds after `make loadgen` so the Grafana panel shows a stable healthy baseline
before anything happens. A flat green line before the incident is what makes the spike
readable.

## 6.3 Beat one — clean tournament resolution (≈3 min)

**0:00 — Frame it in two sentences.** "Most incident-response agents test their fix by
applying it to production. This one rehearses every candidate fix on live replicas first,
runs them against each other, and then has to get past a formal safety check before it can
act."

**0:20 — Break production.**
```bash
ust scenario inject bad_deploy_data_service
```
Pane 4 shows p99 climbing through the SLO line. Say what was done: a real bad deploy, an
N+1 query regression rolled out to `data-service`.

**0:35 — The alert.** PagerDuty fires. Pane 1 shows `alert received`. Show the PagerDuty
incident for three seconds.

**0:45 — Context.** Pane 1: error signatures from Loki, the offending commit from GitHub
with its PR, the dependency graph. Note out loud that these are real API calls to a real
repository.

**1:00 — Candidates.** Four appear in pane 3: rollback to the pre-regression digest,
restart `data-service`, scale `data-service` +2, and no-action. Say why no-action is always
on the ballot.

**1:15 — The fork.** Pane 2 shows three namespaces appearing and pods going Ready, plus
three databases cloned. This is the moment that looks impressive; give it a beat of silence
rather than talking over it. Note the mirror registration line: production traffic is now
fanning out to all three.

**1:45 — Rehearsal and scoreboard.** Pane 3 fills in live: recovery time, blast radius,
downstream impact, mirror drop ratio, composite. Restart looks good early and then
regresses; scale masks the latency without fixing it; rollback recovers and holds. Point at
the drop-ratio column and say that a candidate whose twin missed traffic is disqualified
rather than scored.

**2:30 — Decision.** Winner: rollback, margin above threshold. Kernel: PASS, with the
invariants listed and the solver time shown. Production is rolled back. Pane 4's p99 returns
under the line. Slack post appears with the full candidate table.

**2:55 — Teardown.** Pane 2: the three twin namespaces disappear. "It cleaned up after
itself" is a small line that lands well with people who operate clusters.

## 6.4 Beat two — the veto (≈2.5 min)

This is the beat that demonstrates the safety property, and it is more convincing than any
number of successful fixes, because it shows the system overruling its own empirical winner.

**3:10 — Set it up honestly.** "Same shape of incident, one difference: the commit we would
roll back to sits on the other side of a schema migration."

```bash
ust scenario inject bad_deploy_with_migration
```

**3:30 — Same loop, same fork, same tournament.** Move faster; the audience has seen it.
Pane 3 resolves: rollback wins again, and wins clearly. It genuinely recovered in every
twin. Say that plainly — the measurement says this is the best fix.

**4:20 — The veto.** Pane 1: `kernel verdict=VETO invariant=K3`. Read the human-readable
reason aloud: the rollback target predates migration `<sha>`, which would leave application
code running against a schema it does not understand. Show that Z3 returned a
counterexample, and that the check took milliseconds.

**4:45 — Why this matters.** "Every metric said this was the right fix. The twins agreed
unanimously. The thing that stopped it was a property nobody measured, because it isn't a
metric — it's an invariant." This is the sentence the demo exists to deliver.

**5:00 — Escalation.** PagerDuty note appears with all four candidates, their scores, the
kernel's reason, and the counterexample. Production is untouched — show pane 4 flat and
pane 2 with no changes to `ust-prod`.

## 6.5 Close (≈1 min)

**5:20 — Shadow mode, in one line with the playbook list on screen.**
```bash
ust playbook list
```
"Between incidents it doesn't idle. It invents failure modes from its own dependency graph,
rehearses them on spare twin capacity, and files the fixes that worked. These four were
written by the system, not by us."

**5:35 — The numbers.** Show `eval/report.md`'s headline table. Twin–prod correlation with
its interval, regret rate, 8/10 proof coverage, escalation precision. Say the n out loud.

**5:50 — The limitation, unprompted.** "The twins are forked from the same state and fed
the same traffic, so they aren't independent — a bug in our twin infrastructure could make
all three wrong the same way. We didn't solve that. We measured it: that's what the
correlation number is, and that's why the kernel refuses to clear a plan built on evidence
with too much mirror drop."

Ending on the limitation rather than a flourish is deliberate. To an audience that sells
sandbox fidelity infrastructure, it is the most credible thing that can be said.

## 6.6 Timing discipline

| Beat | Budget | Hard stop |
|---|---|---|
| Frame + inject + alert | 0:45 | 1:00 |
| Context + candidates + fork | 1:00 | 2:00 |
| Rehearsal + decision + act | 1:15 | 3:10 |
| Veto beat | 2:00 | 5:10 |
| Close | 0:50 | 6:00 |

The fork and the rehearsal window are the only unbounded waits. Both are bounded in code
(120s and 200s). If either runs long on the day, the narration covers it — that is what the
"why no-action is on the ballot" and "why drop ratio disqualifies" lines are for. Have them
memorised as filler.

## 6.7 Contingency

Rehearse each of these once; do not improvise them live.

| Failure | Response |
|---|---|
| Tunnel drops, PagerDuty webhook does not arrive | `ust alert inject --scenario <id>` produces an identical `Alert`. Say "synthetic alert, same code path" and continue. |
| A twin fails to reach Ready | The tournament disqualifies it and proceeds with two. Narrate it as designed behaviour, because it is. |
| Rollback does not win beat one | Do not fix it live. Say what actually won and why the scoreboard shows that, then continue. A demo that reports its own surprise is more credible than one that pretends. |
| Cluster wedged | `make reset` takes under 60s. If it fails, cut to the recorded backup run and narrate over it, saying it is a recording. |
| Out of time | Cut §6.5's playbook line, never the veto beat. |

Record a full successful run in advance at the same resolution and layout. Never present a
recording as live.

## 6.8 What the repository must show a judge who clones it

The demo is 6 minutes; the repository is what gets examined afterwards. Make sure these
land in the first minute of browsing:

- `README.md` explains the idea in one paragraph and links the doc map.
- `eval/report.md` is committed and contains real numbers with intervals.
- `docs/03-invariants.md` §3.6 exists — the abstraction gap, written down.
- `src/understudy/kernel/invariants/` has one readable file per invariant.
- `AGENTS.md` shows the build was disciplined rather than improvised.
