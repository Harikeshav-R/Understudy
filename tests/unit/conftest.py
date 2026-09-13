"""Unit-test isolation from any real Kubernetes cluster."""

from collections.abc import Generator

import pytest
from kubernetes import config as k8s_config


@pytest.fixture(autouse=True)
def forbid_real_kube_config(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Fail loudly when a unit test tries to load real Kubernetes configuration.

    A developer machine has a kubeconfig and CI does not, so a test that forgets to inject a
    fake API client passes locally and fails only in CI with an opaque ConfigException. Both
    loaders raise the same assertion here, so the omission surfaces immediately and
    identically in both places. Tests that deliberately exercise the client factories patch
    these loaders themselves, which takes precedence inside their `with` block.
    """

    def _refuse(*args: object, **kwargs: object) -> None:
        _ = (args, kwargs)
        raise AssertionError(
            "unit test attempted to load real Kubernetes configuration; pass a fake client "
            "instead (core_api=, apps_api=, networking_api=, rbac_api=)"
        )

    monkeypatch.setattr(k8s_config, "load_incluster_config", _refuse)
    monkeypatch.setattr(k8s_config, "load_kube_config", _refuse)
    yield
