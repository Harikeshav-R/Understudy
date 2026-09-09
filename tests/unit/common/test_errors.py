"""Unit tests for the UnderstudyError hierarchy."""

from understudy.common.errors import (
    ActuationError,
    ConfigError,
    EvidenceError,
    FleetError,
    KernelError,
    UnderstudyError,
)


def test_understudy_error_hierarchy() -> None:
    base = UnderstudyError("base failure", details={"key": "val"})
    assert isinstance(base, Exception)
    assert base.message == "base failure"
    assert base.details == {"key": "val"}
    assert "UnderstudyError" in repr(base)

    cfg = ConfigError("bad config")
    assert isinstance(cfg, UnderstudyError)
    assert cfg.details == {}

    assert issubclass(FleetError, UnderstudyError)
    assert issubclass(KernelError, UnderstudyError)
    assert issubclass(ActuationError, UnderstudyError)
    assert issubclass(EvidenceError, UnderstudyError)
