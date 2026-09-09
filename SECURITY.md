# Security Policy

## Supported Versions

Understudy is under active development. Security updates are applied to the latest
release branch on `main`.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

---

## Security Architecture & Invariants

Understudy is designed around a zero-trust execution model for AI-generated remediation
actions:

1. **Closed Action Enum:** The LLM cannot execute arbitrary shell commands or code in
   production. It may only synthesize typed plans conforming to `contracts/models.py`.
2. **Safety Kernel Verification (Z3):** Every candidate plan that wins tournament scoring
   must be formally verified against system invariants before production actuation is
   permitted (Invariant K10).
3. **Triple-Layer Twin Egress Isolation:**
   - Kubernetes `NetworkPolicy` denies egress from twin namespaces (except to the mirror gateway).
   - CoreDNS rewrite rules direct all outbound traffic to an egress stub.
   - Workload environment variables point sensitive external endpoints to mock sinks.
4. **Read-Only Production Boundary:** Fleet controllers and twin workers have read-only
   access to `ust-prod`. Only the orchestrator's `actuate` node holds actuation permissions.
5. **No Secret Ingestion:** Secrets and credentials are never stored in run records,
   playbooks, logs, or commit history.

---

## Reporting a Vulnerability

We take the security of Understudy seriously. If you discover a security vulnerability or
an isolation bypass, please report it privately rather than opening a public issue.

### How to Report
1. Open a **GitHub Security Advisory** under the repository's "Security" tab, or
2. Email the maintainers at `security@understudy.dev` with:
   - A description of the vulnerability and its potential impact.
   - Step-by-step reproduction instructions or a proof-of-concept.
   - Any proposed remediation or mitigation.

### Response Commitment
- **Initial Acknowledgement:** Within 48 hours.
- **Triage & Assessment:** Within 5 business days.
- **Resolution & Disclosure:** Coordinated release and public advisory once a fix is verified.
