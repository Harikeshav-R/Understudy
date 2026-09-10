"""Retry utilities built on tenacity."""

from collections.abc import Callable
from typing import Any, TypeVar

from tenacity import (
    retry as tenacity_retry,
)
from tenacity import (
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from understudy.common.errors import UnderstudyError

F = TypeVar("F", bound=Callable[..., Any])


def retry(
    max_attempts: int = 3,
    min_wait: float = 0.1,
    max_wait: float = 2.0,
    retry_exceptions: tuple[type[Exception], ...] = (UnderstudyError,),
) -> Callable[[F], F]:
    """Configurable retry decorator built on tenacity.

    Defaults to retrying only `UnderstudyError`. Callers retrying raw external I/O
    (e.g. `httpx` errors, `ConnectionError`, `TimeoutError`) must pass `retry_exceptions`
    explicitly.
    """
    return tenacity_retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=min_wait, max=max_wait),
        retry=retry_if_exception_type(retry_exceptions),
        reraise=True,
    )
