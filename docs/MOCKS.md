# Mock Registry

Every `# MOCKED:` marker in the source has exactly one row here, and every row has exactly
one marker. `make check` fails on a mismatch in either direction (`AGENTS.md` §7.3).

This file exists so that "what in this system is not real?" has a one-screen answer, for us
during the build and for a judge afterwards.

**Fakes are not mocks.** `<package>/fakes.py` implementations are a deliberate part of the
architecture (ADR-007), used by unit tests and `ust demo --fake`. They are not listed here.
This file is only for places where a path that is *supposed* to be real is not.

| Module | What is mocked | Why | Real path | Issue | Status |
|---|---|---|---|---|---|
| _(none yet)_ | | | | | |

## Rules

- A row is added in the same PR as the marker.
- A row is removed in the same PR that removes the marker.
- Nothing in a `--live` path may appear here at submission time. If something does, it is
  named out loud in the demo and in `docs/08-submission-brief.md` §B8.
- "Temporarily mocked to get the tests passing" is not a reason. Either it is a fake behind
  a Protocol, or it is real, or the work is not done.
