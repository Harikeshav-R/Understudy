# Contributing to Understudy

Thank you for contributing to Understudy! This project is built under strict architectural
and reliability constraints. All contributors—both humans and coding agents—must adhere
to the rules outlined below and in [`AGENTS.md`](./AGENTS.md).

---

## 1. Ground Rules & Principles

1. **Read `AGENTS.md` first.** It is authoritative. If code conflicts with `AGENTS.md`,
   `AGENTS.md` wins and the code is a bug.
2. **Never deviate from an ADR.** Architectural Decision Records in `docs/01-decisions.md`
   are locked. If an ADR needs revision, open an issue titled `ADR-NNN revisit: <reason>`
   and get approval before writing code.
3. **Never widen the trusted base.** The safety story relies on a closed action enum,
   formal invariants (`docs/03-invariants.md`), and strict isolation guarantees.
4. **Zero cloud spend.** Understudy runs locally on k3d within an 18 GB RAM budget
   (ADR-001, ADR-003). Free-tier services only.
5. **No fake integrations in real paths.** Real paths use real integrations; any mocks
   used for testing must be registered in `docs/MOCKS.md`.

---

## 2. Getting Started

### Prerequisites
- macOS (Apple Silicon recommended) or Linux
- Docker Desktop or Docker Engine allocated at least 12 GB RAM
- Python 3.12 (managed via `uv`)
- `uv` (>= 0.4)
- `git`
- `k3d` and `kubectl` (for Phase 1+ cluster workflows)

### Setup
Clone the repository and sync dependencies:

```bash
git clone https://github.com/Harikeshav-R/Understudy.git
cd Understudy

# Install all dependencies and create virtual environment
uv sync --all-groups

# Verify environment and preflight checks
make check
```

---

## 3. Development Workflow

### Step-by-Step Task Execution
1. Consult [`CHECKLIST.md`](./CHECKLIST.md). The first unchecked box represents the next
   unit of work.
2. Read the corresponding step specification in [`docs/04-build-plan.md`](./docs/04-build-plan.md)
   and the contract definitions in `src/understudy/contracts/`.
3. Create a feature branch following the Conventional Branch format:
   ```bash
   git checkout -b <type>/<short-kebab-description>
   ```
   Valid types: `feat`, `fix`, `docs`, `test`, `chore`, `refactor`, `perf`, `build`, `ci`.
4. Implement the changes following our code standards.
5. Write unit tests to achieve **100% line and branch coverage** on all new code.
6. Verify locally with:
   ```bash
   make check
   ```
7. Commit with Conventional Commits, push your branch, and open a Pull Request.

---

## 4. Coding Standards

- **Language:** Python 3.12 exclusively.
- **Formatting and Linting:** `ruff` is authoritative. Maximum line length is 100 characters.
- **Type Checking:** `mypy --strict` must pass with zero errors and no ignores.
- **Import Boundaries:** Enforced via `import-linter`.
  - `contracts` imports nothing internal.
  - `common` imports only `contracts`.
  - Only `orchestrator` may import `langgraph`.
  - Only `store` may import `psycopg` or `sqlalchemy`.
  - Only `fleet`, `actuator`, and `graph` may import `kubernetes`.
  - Sibling packages communicate only through `api.py` Protocols and `contracts`.
- **Async:** Asyncio throughout. No blocking I/O in async routines. Network calls must have
  explicit timeouts.
- **Protocols over Concrete Classes:** Depend on abstractions injected via `Deps`.
- **No Silencing:** `# type: ignore`, `# noqa`, `# pragma: no cover`, `pytest.mark.skip`,
  and `pytest.mark.xfail` are strictly forbidden without an inline reason and linked issue.

---

## 5. Testing & Coverage

- **100% Line and Branch Coverage:**
  ```bash
  uv run pytest --cov=understudy --cov-branch --cov-fail-under=100
  ```
- **Test Layers:**
  - `tests/unit/`: Hermetic, fast (sub-60s suite), zero network/cluster I/O.
  - `tests/integration/`: Marked with `@pytest.mark.integration`. Requires `make up`.
  - `tests/e2e/`: Marked with `@pytest.mark.e2e`. Full end-to-end scenarios.
- **Never delete or weaken protected tests:** The invariants and isolation tests listed in
  `AGENTS.md` §6.4 are permanent safeguards.

---

## 6. Commits & Pull Requests

### Commits
- Format: `<type>(<scope>): <subject>`
- Scope must be a package name under `src/understudy/` or one of `services`, `deploy`,
  `docs`, `ci`.
- Always provide a descriptive commit body explaining *why* the change was made.
- Reference the build-plan step in the body (e.g., `Implements build-plan step 0.1.`).

### Pull Requests
- **Never push or commit directly to `main`.** Always open a PR.
- Use the standard PR template (`.github/pull_request_template.md`).
- Fill in every section:
  - Step implemented
  - Checkpoint command and output
  - ADRs relied on
  - Mock registry disclosures
  - Updated documentation
  - Definition of Done checklist
- All CI checks (`make check`) must be green before merging.
- Merges are completed using **merge commits** (not squash or rebase) to preserve
  commit history and provenance.
