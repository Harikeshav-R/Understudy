"""Unit tests for retry decorator."""

import pytest

from understudy.common.retry import retry


def test_retry_eventual_success() -> None:
    calls = 0

    @retry(max_attempts=3, min_wait=0.01, max_wait=0.05, retry_exceptions=(ValueError,))
    def flaky_func() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ValueError("temporary error")
        return "success"

    result = flaky_func()
    assert result == "success"
    assert calls == 3


def test_retry_exhaustion_raises() -> None:
    calls = 0

    @retry(max_attempts=2, min_wait=0.01, max_wait=0.02, retry_exceptions=(ValueError,))
    def failing_func() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("permanent error")

    with pytest.raises(ValueError, match="permanent error"):
        failing_func()
    assert calls == 2


def test_retry_non_retryable_exception_raises_immediately() -> None:
    calls = 0

    @retry(max_attempts=3, min_wait=0.01, max_wait=0.02, retry_exceptions=(ValueError,))
    def specific_func() -> None:
        nonlocal calls
        calls += 1
        raise KeyError("unexpected error")

    with pytest.raises(KeyError):
        specific_func()
    assert calls == 1


def test_retry_default_exceptions_understudy_error() -> None:
    from understudy.common.errors import UnderstudyError

    calls = 0

    @retry(max_attempts=3, min_wait=0.01, max_wait=0.02)
    def domain_func() -> str:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise UnderstudyError("transient domain error")
        return "recovered"

    assert domain_func() == "recovered"
    assert calls == 2

    # Non-UnderstudyError should raise immediately without retry
    calls = 0

    @retry(max_attempts=3, min_wait=0.01, max_wait=0.02)
    def non_domain_func() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("standard error")

    with pytest.raises(ValueError, match="standard error"):
        non_domain_func()
    assert calls == 1
