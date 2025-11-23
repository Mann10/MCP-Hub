import asyncio
from typing import Awaitable, Callable, Iterable, Type, TypeVar

T = TypeVar("T")


async def async_retry(
    func: Callable[[], Awaitable[T]],
    *,
    retries: int,
    base_delay: float,
    exceptions: Iterable[Type[BaseException]],
) -> T:
    """
    Simple async retry helper with exponential backoff.

    param func: async callable returning a value
    param retries: number of retries (not counting the first attempt)
    param base_delay: initial backoff delay in seconds
    param exceptions: iterable of exception types considered transient
    """
    attempt = 0
    delay = base_delay
    exc_types = tuple(exceptions)

    while True:
        try:
            return await func()
        except exc_types:
            if attempt >= retries:
                raise
            await asyncio.sleep(delay)
            delay *= 2
            attempt += 1