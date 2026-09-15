"""Unit tests for the UnderstudyError hierarchy."""

from understudy.common.errors import (
    ActuationError,
    ConfigError,
    EvidenceError,
    FleetError,
    KernelError,
    OrchestratorError,
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
    assert issubclass(OrchestratorError, UnderstudyError)

    from understudy.common.errors import (
        MissingFact,
        ObservabilityError,
        PlannerError,
        PlaybookConfirmationError,
        PlaybookEmbeddingError,
        PlaybookError,
        SignalsError,
        StoreError,
    )

    assert issubclass(MissingFact, KernelError)
    assert issubclass(StoreError, UnderstudyError)
    assert issubclass(SignalsError, UnderstudyError)
    assert issubclass(ObservabilityError, SignalsError)
    assert issubclass(PlannerError, UnderstudyError)
    assert issubclass(PlaybookError, UnderstudyError)
    assert issubclass(PlaybookEmbeddingError, PlaybookError)
    assert issubclass(PlaybookConfirmationError, PlaybookError)

    mf_single = MissingFact("last_migration_commit_time")
    assert mf_single.fact_name == "last_migration_commit_time"
    assert mf_single.missing_facts == ["last_migration_commit_time"]
    assert mf_single.details == {
        "fact_name": "last_migration_commit_time",
        "missing_facts": ["last_migration_commit_time"],
    }
    assert "last_migration_commit_time" in str(mf_single)

    mf_multi = MissingFact(["k1_fact", "k2_fact"], message="Multiple facts missing")
    assert mf_multi.fact_name == "k1_fact"
    assert mf_multi.missing_facts == ["k1_fact", "k2_fact"]
    assert mf_multi.message == "Multiple facts missing"

    mf_empty = MissingFact([])
    assert mf_empty.fact_name == ""
    assert mf_empty.missing_facts == []
