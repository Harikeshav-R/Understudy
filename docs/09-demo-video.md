# 09 — The Two-Minute Demo Video

`docs/06-demo.md` is the six-minute live demo. This is the submitted video, and it is a
different artifact with a different constraint. Everything here overrides §6 where they
conflict.

**The hard number: voiceover runs at roughly 160 words per minute.** The script below is
351 words, which lands at about **2:11**. Every sentence has to earn its place against three
others you will cut. See §9.3.1 for the 2:00-exactly option and what it costs.

---

## 9.1 What has to survive the cut

At two minutes you cannot show the loop twice at full length, and you cannot explain the
architecture. Rank what matters:

1. **The veto.** A system overruling its own empirical winner is the single most
   memorable thing here, and it is the only moment that demonstrates the safety property
   rather than asserting it. It gets the most screen time.
2. **The fleet fork.** Three environments standing up simultaneously is the shot that makes
   the idea legible without words.
3. **The scoreboard resolving.** Proof that arbitration is real and measured.
4. **The placement claim.** One beat, after the veto, saying where this sits relative to
   how sandboxes are normally used: before deployment, versus inside the acting agent's
   loop. This is the originality argument (15%) and it lands only once the audience has
   watched the mechanism work. See §9.3.2 for why it is framed on placement and not on
   subject.
5. **The numbers.** Four seconds of `eval/report.md` is what converts "nice demo" into
   "reliability & evaluation, 25%".
6. **The limitation, said out loud.** To judges who sell sandbox fidelity infrastructure,
   this is credibility, not weakness. Ten seconds.

What gets cut: architecture explanation, integration list, shadow mode as a beat, the
playbook library, context-gathering detail, anything about how it is built. Those live in
the repository and the brief. **The video's job is to make a judge want to open the repo.**

**Both beats stay.** It is tempting to show only the veto, but the veto is unreadable
without the clean run first — the audience has to see the loop work before seeing it refuse
to work. Beat one is compressed to 38 seconds, not dropped.

## 9.2 Shot list

| Time | On screen | Voiceover | Burned-in text |
|---|---|---|---|
| 0:00–0:11 | Grafana p99 flat and green, full width | *cold open, see script* | `UNDERSTUDY` (2s, then gone) |
| 0:11–0:19 | Same panel, p99 spikes through the SLO line. Cut to PagerDuty incident for 2s | incident | `real deploy · real alert` |
| 0:19–0:29 | Candidate list appearing, punch-in | candidates | `4 candidates incl. "do nothing"` |
| 0:29–0:42 | **Fleet fork.** `kubectl` watch: three namespaces, twelve pods going Ready. Speed-ramped | the fork | `3 twins · live traffic mirrored · 4× speed` |
| 0:42–0:54 | Scoreboard TUI filling in, then resolving. Kernel line: PASS. Grafana recovers | tournament + act | `winner: rollback · kernel: PASS` |
| 0:54–1:03 | Reset. Same Grafana spike. Small on-screen diff showing the migration commit | setup for beat 2 | `same incident · one difference` |
| 1:03–1:13 | Scoreboard resolves again, rollback winning clearly. Hold on the margin | the tournament is right | `rollback wins in all 3 twins` |
| 1:13–1:29 | **Kernel line flips to VETO.** Punch-in hard on the reason text and the counterexample | the veto | `VETO · K3 · rollback crosses a schema migration` |
| 1:29–1:36 | Grafana flat, `ust-prod` unchanged. PagerDuty note with all four candidates | escalation | `production untouched` |
| 1:36–1:48 | **Zoom out.** The full four-pane layout at rest: twins gone, prod green, agent idle. The only shot where the whole system is on screen at once | placement | `sandbox before deploy → sandbox in the loop` |
| 1:48–1:59 | `eval/report.md` headline table, punch-in on three rows | the numbers | numbers stay on screen |
| 1:59–2:11 | Static end card: repo URL | the limitation | `github.com/…` |

Twelve shots. Anything that cannot be told in one of them does not go in the video.

The placement shot at 1:36 is the only moment the four-pane layout belongs on screen. It is
a deliberate zoom-out after the closest punch-in in the video, and the contrast is what
makes it read as a conclusion rather than another step.

## 9.3 The script

Read at a measured pace. Do not rush to fit more in; cut words instead.

**0:00 — Cold open (30 words)**
> An agent that fixes production usually tests its fix by applying it. If the guess is
> wrong, the incident just got worse. Understudy rehearses first, on live replicas.

**0:12 — The incident (22 words)**
> A real regression ships to a real service. Latency breaches the SLO. PagerDuty fires, and
> the agent picks it up.

**0:20 — Candidates (26 words)**
> It proposes four remediations, not one. Roll back, restart, scale, and do nothing —
> because sometimes doing nothing is correct, so it is always on the ballot.

**0:30 — The fork (34 words)**
> Each candidate gets its own replica of production, forked from synced state, with
> production traffic mirrored into all three simultaneously. They are rehearsed against the
> same traffic at the same moment.

**0:40 — Tournament and action (32 words)**
> Recovery time, blast radius, downstream impact. Rollback wins on measurement. A formal
> safety kernel clears it, and only then does anything touch production. Latency recovers.

**0:50 — Setup for the veto (24 words)**
> Now the same incident with one difference. The commit we would roll back to sits on the
> other side of a schema migration.

**1:00 — The tournament is right (26 words)**
> Same loop. Rollback wins again, and wins clearly — it recovered in all three twins. Every
> measurement says this is the correct fix.

**1:15 — The veto (44 words)**
> The kernel vetoes it. Rolling back across that migration would leave application code
> running against a schema it does not understand. That is an invariant, proved by Z3 in
> milliseconds — not a metric, so nothing in the tournament could have caught it.

**1:29 — Escalation (20 words)**
> Production is untouched. A human gets every candidate, every score, and the
> counterexample that stopped it.

**1:36 — Placement (31 words)**
> Sandboxes normally sit before deployment: you test the agent, then ship it. This one lives
> inside the loop. It rehearses every decision, at incident time, against production as it
> is right now.

**1:48 — The numbers (30 words)**
> Across thirty-six runs: twin-to-production correlation with a confidence interval,
> measured regret against real counterfactual runs, and eight of ten safety invariants
> machine-checked.

**1:59 — The limitation (32 words)**
> The twins share one snapshot and one traffic stream, so they are not independent. We did
> not solve that. We measured it — that is what the correlation number is.

Total: 351 words, about 2:11 at 160 words per minute.

Ending on the limitation is deliberate and it is the highest-leverage ten seconds in the
video. Every other submission will end on a flourish.

### 9.3.1 If the submission requires 2:00 exactly

351 words in 120 seconds is 176 wpm. That is deliverable but it sounds hurried, and it
removes the pauses the veto beat needs — the three seconds of silence after `VETO` appears
is doing real work.

Prefer running to 2:11. If a hard 2:00 cap exists, cut in this order and stop as soon as you
are under, rather than trimming everything evenly:

1. Candidates beat, 26 → 10 words: *"It proposes four remediations, not one — including
   doing nothing."* Saves 16.
2. Escalation beat, 20 → 11 words: *"Production is untouched. A human gets every candidate
   and the counterexample."* Saves 9.
3. Fork beat, drop the final sentence. Saves 12.

That reaches 314 words, about 1:58. Never cut from the veto, the placement beat, or the
limitation.

### 9.3.2 Why placement, and not subject

The tempting framing is: *they twin the services your product depends on; we twin your
product itself.* Do not use it. That is a claim about subject matter, and nothing in Arga's
positioning rules out twinning your own services — so a judge can dissolve the entire
originality argument with one sentence, live, and they are the only people who can.

The claim that holds regardless of what their product does is about **placement**: a
pre-ship sandbox validates an agent's policy once, against yesterday's system. A runtime
twin validates a decision, every time, against the system as it exists at the moment of the
incident. Those catch different classes of failure. That is the same argument as
`docs/00-product.md` §0.4 and it is defensible without knowing a thing about their roadmap.

**Do not name Arga Labs in the video.** State the architectural claim and let them recognise
it — a judge who builds sandbox infrastructure makes the connection in under a second, and
it lands better as their own inference than as your compliment. Naming them belongs in the
written brief, where you can be precise and they can check it.

## 9.4 Production rules

**Capture.** 1080p minimum, 30fps, one fixed window layout. Record the full real run at
full length first; edit afterwards. Never restage a step to make it look better.

**Punch-ins, not four panes.** The four-pane layout from §6.1 is for a live audience on a
large screen. On a two-minute video watched in a browser tab, four panes are four
unreadable panes. Record the full layout, then crop and scale to the active region in the
edit. One thing readable at a time.

**Speed ramps must be labelled.** The fork wait and the rehearsal window are genuinely 60
to 200 seconds. Speed them up and burn `4× speed` on screen while you do. An unlabelled
time cut in a video about reliability is the exact thing this audience notices, and it
costs you more than the seconds it saves.

**Burned-in text.** Assume the judge watches muted at least once. Every key claim appears
on screen as text, not only in narration. Keep it to five words, high contrast,
bottom-third, and hold it for at least two seconds.

**Audio.** Voiceover recorded separately from a script, in one take per section, normalised.
No music, or music at a level you have to strain to notice. Never narrate live while
operating — it produces filler and dead air.

**Cursor.** Highlight it or hide it. A hunting cursor reads as improvisation.

**No title sequence.** Two seconds of a name, maximum. No talking head, no "hi, I'm…", no
agenda slide. The first spoken sentence is the problem statement.

## 9.5 Honesty rules

These are not optional and they are what the audience is specifically equipped to detect.

- Every frame comes from a real run against the real cluster. No mockups, no reenactments,
  no hand-edited terminal output.
- Speed changes labelled. Cuts between beats are obvious cuts, not disguised as continuity.
- The numbers on screen come from the committed `eval/report.json`. If n is 36, the video
  says thirty-six.
- Nothing in the video is a `# MOCKED:` path. Check `docs/MOCKS.md` before you export.
- If the tournament picks something other than rollback on the take you use, narrate what
  actually won. Do not re-shoot until it agrees with the script.
- The placement beat describes how sandboxes are *normally* used, not what any specific
  company sells. Before Phase 7, read Arga Labs' current positioning from their own site and
  confirm the line still describes the category honestly. Misdescribing a judge's product to
  their face costs more than the sentence is worth.

## 9.6 Getting a usable take

Record five or six complete runs of each beat and edit from the best. Runs are cheap; the
system is deterministic by design, so this is not fishing for a lucky result, it is picking
the cleanest capture of a reproducible one.

Beat two is the one that must be flawless, so shoot it first while you have patience, and
shoot it more times than beat one.

Watch the cut once on a phone with sound off. If the story does not survive that, the
burned-in text is wrong.

## 9.7 Checklist before export

- [ ] Under 2:12, including the end card (or under 2:00 via §9.3.1 if a hard cap applies)
- [ ] Opens on the problem within 5 seconds, no title sequence
- [ ] The fork shot is legible and labelled with its speed
- [ ] The veto gets at least 20 seconds and the reason text is readable when paused
- [ ] `eval/report.md` numbers on screen match the committed report exactly
- [ ] The placement beat is present, framed on placement rather than subject (§9.3.2)
- [ ] Arga Labs is not named anywhere in the video
- [ ] Arga's current positioning verified from their own site; the placement line still holds
- [ ] The limitation is spoken and is the final thing said
- [ ] Repo URL on the end card, correct and public
- [ ] Watchable muted: every claim has on-screen text
- [ ] Every speed ramp labelled; no unlabelled time compression
- [ ] Nothing shown is a mocked path
- [ ] Exported at 1080p; file plays in a browser without download

## 9.8 What the video is not responsible for

The video makes a judge open the repository. The repository closes the argument.
`docs/06-demo.md` §6.8 lists what must land in the first minute of browsing, and the brief
carries the full evidence. Do not try to fit the architecture, the integrations, or the
evaluation methodology into 120 seconds. Trying is how a two-minute video becomes a
four-minute video nobody finishes.
