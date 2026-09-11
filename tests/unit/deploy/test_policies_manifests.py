"""Unit tests for deploy/policies manifests and templates."""

from pathlib import Path
from typing import Any

import yaml


def _load_manifests(file_path: Path) -> list[dict[str, Any]]:
    """Load all YAML documents from a manifest file."""
    assert file_path.exists(), f"{file_path} must exist"
    with file_path.open() as f:
        docs = list(yaml.safe_load_all(f))
    return [d for d in docs if d is not None]


def test_understudy_prod_manifest() -> None:
    """Validate understudy-prod ServiceAccount, Role, and RoleBinding."""
    path = Path("deploy/policies/understudy-prod.yaml")
    docs = _load_manifests(path)
    assert len(docs) == 3

    by_kind = {d["kind"]: d for d in docs}
    assert "ServiceAccount" in by_kind
    assert "Role" in by_kind
    assert "RoleBinding" in by_kind

    # 1. ServiceAccount assertions
    sa = by_kind["ServiceAccount"]
    assert sa["metadata"]["name"] == "understudy-prod"
    assert sa["metadata"]["namespace"] == "ust-prod"
    assert sa["metadata"]["labels"]["environment"] == "production"

    # 2. Role assertions: strictly scoped to deployments and configmaps in ust-prod
    role = by_kind["Role"]
    assert role["metadata"]["name"] == "understudy-prod"
    assert role["metadata"]["namespace"] == "ust-prod"

    allowed_rules = role["rules"]
    assert len(allowed_rules) == 2

    # Verify apps rule
    apps_rule = next(r for r in allowed_rules if "apps" in r.get("apiGroups", []))
    assert set(apps_rule["resources"]) == {"deployments", "deployments/scale"}
    assert set(apps_rule["verbs"]) == {"get", "list", "watch", "patch", "update"}

    # Verify core configmaps rule
    core_rule = next(r for r in allowed_rules if "" in r.get("apiGroups", []))
    assert set(core_rule["resources"]) == {"configmaps"}
    assert set(core_rule["verbs"]) == {"get", "list", "watch", "patch", "update"}

    # Assert no other resource kinds or apiGroups are permitted (e.g. no secrets, pods)
    for rule in allowed_rules:
        assert "secrets" not in rule.get("resources", [])
        assert "pods" not in rule.get("resources", [])
        assert "*" not in rule.get("resources", [])
        assert "*" not in rule.get("verbs", [])

    # 3. RoleBinding assertions
    rb = by_kind["RoleBinding"]
    assert rb["metadata"]["name"] == "understudy-prod"
    assert rb["metadata"]["namespace"] == "ust-prod"
    assert rb["roleRef"]["kind"] == "Role"
    assert rb["roleRef"]["name"] == "understudy-prod"
    assert rb["subjects"] == [
        {"kind": "ServiceAccount", "name": "understudy-prod", "namespace": "ust-prod"}
    ]


def test_understudy_twin_cluster_role() -> None:
    """Validate cluster-scoped understudy-twin ClusterRole."""
    path = Path("deploy/policies/understudy-twin.yaml")
    docs = _load_manifests(path)
    assert len(docs) == 1

    cr = docs[0]
    assert cr["kind"] == "ClusterRole"
    assert cr["metadata"]["name"] == "understudy-twin"
    assert cr["rules"] == [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}]


def test_twin_rbac_template() -> None:
    """Validate twin-rbac-template rendering with target namespace."""
    path = Path("deploy/policies/twin-rbac-template.yaml")
    assert path.exists()
    content = path.read_text()

    rendered = content.replace("${TWIN_NAMESPACE}", "ust-twin-inc-0")
    docs = [d for d in yaml.safe_load_all(rendered) if d is not None]
    assert len(docs) == 2

    by_kind = {d["kind"]: d for d in docs}
    sa = by_kind["ServiceAccount"]
    assert sa["metadata"]["name"] == "understudy-twin"
    assert sa["metadata"]["namespace"] == "ust-twin-inc-0"

    rb = by_kind["RoleBinding"]
    assert rb["metadata"]["name"] == "understudy-twin"
    assert rb["metadata"]["namespace"] == "ust-twin-inc-0"
    assert rb["roleRef"]["kind"] == "ClusterRole"
    assert rb["roleRef"]["name"] == "understudy-twin"
    assert rb["subjects"][0]["namespace"] == "ust-twin-inc-0"


def test_twin_network_policy_golden_spec() -> None:
    """Validate twin NetworkPolicy golden spec adheres to ADR-014 and Invariant K6."""
    path = Path("deploy/policies/twin-network-policy.yaml")
    docs = _load_manifests(path)
    assert len(docs) == 1

    np = docs[0]
    assert np["kind"] == "NetworkPolicy"
    assert np["metadata"]["name"] == "twin-egress-containment"
    assert np["spec"]["podSelector"] == {}
    assert np["spec"]["policyTypes"] == ["Egress"]

    egress_rules = np["spec"]["egress"]
    assert len(egress_rules) == 3

    # Rule 1: Intra-namespace pods
    assert egress_rules[0] == {"to": [{"podSelector": {}}]}

    # Rule 2: DNS in kube-system
    dns_rule = egress_rules[1]
    assert dns_rule["to"][0]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "kube-system"
    }
    dns_ports = {(p["protocol"], p["port"]) for p in dns_rule["ports"]}
    assert ("UDP", 53) in dns_ports
    assert ("TCP", 53) in dns_ports

    # Rule 3: Backing services in ust-system
    sys_rule = egress_rules[2]
    assert sys_rule["to"][0]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "ust-system"
    }
    sys_ports = {(p["protocol"], p["port"]) for p in sys_rule["ports"]}
    assert ("TCP", 5432) in sys_ports
    assert ("TCP", 8000) in sys_ports
    assert ("TCP", 8080) in sys_ports

    # Crucial security assertions: ust-prod and public internet MUST NOT be present
    for rule in egress_rules:
        rule_str = str(rule)
        assert "ust-prod" not in rule_str
        assert "production" not in rule_str
        assert "0.0.0.0/0" not in rule_str


def test_twin_network_policy_template() -> None:
    """Validate twin-network-policy.template.yaml renders correctly."""
    path = Path("deploy/policies/twin-network-policy.template.yaml")
    assert path.exists()
    content = path.read_text()

    rendered = content.replace("${TWIN_NAMESPACE}", "ust-twin-inc-0")
    docs = [d for d in yaml.safe_load_all(rendered) if d is not None]
    assert len(docs) == 1
    np = docs[0]
    assert np["kind"] == "NetworkPolicy"
    assert np["metadata"]["namespace"] == "ust-twin-inc-0"
    assert np["metadata"]["name"] == "twin-egress-containment"


def test_twin_network_policy_golden_matches_template() -> None:
    """The golden copy (applied with `kubectl apply -n <namespace>`, so it carries no
    namespace of its own) and the template (rendered at fork time, so it carries
    `namespace: ${TWIN_NAMESPACE}`) must agree on everything else. Neither file is
    generated from the other, so nothing else catches the two drifting apart if an
    egress rule is edited in one and not mirrored in the other."""
    golden = _load_manifests(Path("deploy/policies/twin-network-policy.yaml"))[0]
    template_content = Path("deploy/policies/twin-network-policy.template.yaml").read_text()
    rendered = template_content.replace("${TWIN_NAMESPACE}", "ust-twin-inc-0")
    template = next(d for d in yaml.safe_load_all(rendered) if d is not None)

    assert "namespace" not in golden["metadata"]
    assert template["metadata"]["namespace"] == "ust-twin-inc-0"

    golden_metadata = {k: v for k, v in golden["metadata"].items() if k != "namespace"}
    template_metadata = {k: v for k, v in template["metadata"].items() if k != "namespace"}
    assert golden_metadata == template_metadata
    assert golden["spec"] == template["spec"]
    assert golden["apiVersion"] == template["apiVersion"]
    assert golden["kind"] == template["kind"]
