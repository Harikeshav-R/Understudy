"""Safety kernel package."""

from understudy.kernel.catalogue import (
    CATALOGUE_INVARIANTS,
    generate_catalogue_markdown,
    get_catalogue_invariants,
    get_invariant_counts,
    render_entry_markdown,
    update_docs_catalogue,
    verify_catalogue_matches_docs,
)
from understudy.kernel.dsl import Invariant, KernelContext, MissingFact
from understudy.kernel.facts import (
    FactExtractor,
    K8sFactSource,
    WorkloadReaderFactAdapter,
    extract_facts,
    load_facts_json,
    save_facts_json,
)
from understudy.kernel.invariants.k01_replica_floor import K1ReplicaFloor
from understudy.kernel.invariants.k02_namespace_scope import K2NamespaceScope
from understudy.kernel.invariants.k03_migration_boundary import K3MigrationBoundary
from understudy.kernel.invariants.k04_blast_containment import K4BlastContainment
from understudy.kernel.invariants.k05_single_writer import K5SingleWriter
from understudy.kernel.invariants.k06_twin_egress import K6TwinEgressContainment
from understudy.kernel.invariants.k07_mutation_budget import K7MutationBudget
from understudy.kernel.invariants.k08_evidence_sufficiency import K8EvidenceSufficiency
from understudy.kernel.invariants.k09_reversibility import K9Reversibility
from understudy.kernel.invariants.k10_actuation_authorisation import K10ActuationAuthorisation
from understudy.kernel.verify import (
    PROOF_INVARIANTS,
    Z3SafetyKernel,
    render_veto_reason,
    verify,
)

__all__ = [
    "CATALOGUE_INVARIANTS",
    "PROOF_INVARIANTS",
    "FactExtractor",
    "Invariant",
    "K1ReplicaFloor",
    "K2NamespaceScope",
    "K3MigrationBoundary",
    "K4BlastContainment",
    "K5SingleWriter",
    "K6TwinEgressContainment",
    "K7MutationBudget",
    "K8EvidenceSufficiency",
    "K8sFactSource",
    "K9Reversibility",
    "K10ActuationAuthorisation",
    "KernelContext",
    "MissingFact",
    "WorkloadReaderFactAdapter",
    "Z3SafetyKernel",
    "extract_facts",
    "generate_catalogue_markdown",
    "get_catalogue_invariants",
    "get_invariant_counts",
    "load_facts_json",
    "render_entry_markdown",
    "render_veto_reason",
    "save_facts_json",
    "update_docs_catalogue",
    "verify",
    "verify_catalogue_matches_docs",
]
