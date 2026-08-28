"""Lightweight Langfuse observability for the agent loop.

Keys are read from the environment (``LANGFUSE_PUBLIC_KEY``,
``LANGFUSE_SECRET_KEY``, optional ``LANGFUSE_HOST``), the same pattern as
provider keys. When the keys are absent the module degrades to no-op context
managers so tracing is never a hard dependency for local development.

Spans are created with :meth:`Langfuse.start_observation` so a span started
inside another span nests under it in the trace view — that is what makes the
Orchestrator -> Worker -> Checker/Gate -> Executor loop visually inspectable
instead of a flat list of LLM calls.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterator

_LANGUFUSE_CLIENT = None
_INITIALIZED = False


class _ObservationProxy:
    """Wraps a real Langfuse observation and tolerates an ``output`` kwarg on
    ``end()`` (which Langfuse's own ``end()`` does not accept)."""

    def __init__(self, observation):
        self._observation = observation
        self._ended = False

    def update(self, *args: Any, **kwargs: Any) -> None:
        if self._ended:
            return
        self._observation.update(*args, **kwargs)

    def end(self, output: Any = None, **kwargs: Any) -> None:
        if self._ended:
            return
        self._ended = True
        if output is not None:
            self._observation.update(output=output)
        self._observation.end(**kwargs)


class _NoopObservation:
    """Stand-in for a real Langfuse observation when tracing is disabled."""

    def end(self, *args: Any, **kwargs: Any) -> None:
        pass

    def update(self, *args: Any, **kwargs: Any) -> None:
        pass


def is_enabled() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


def get_client():
    """Return the lazily-initialized Langfuse client, or None if disabled."""
    global _LANGUFUSE_CLIENT, _INITIALIZED
    if _INITIALIZED:
        return _LANGUFUSE_CLIENT
    _INITIALIZED = True
    if not is_enabled():
        return None
    try:
        from langfuse import Langfuse

        _LANGUFUSE_CLIENT = Langfuse(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=os.environ.get("LANGFUSE_HOST") or "https://cloud.langfuse.com",
        )
    except Exception:
        _LANGUFUSE_CLIENT = None
    return _LANGUFUSE_CLIENT


def flush() -> None:
    client = get_client()
    if client is not None:
        try:
            client.flush()
        except Exception:
            pass


def trace(name: str, *, input: Any = None, metadata: dict[str, Any] | None = None):
    """Start a top-level trace. Yields a proxy when enabled, a no-op otherwise."""
    client = get_client()
    if client is None:
        return _noop_observation()
    return _observation(client, name, "trace", input=input, metadata=metadata)


def span(name: str, *, input: Any = None, output: Any = None, metadata: dict[str, Any] | None = None):
    """Start a nested span under the current trace. Yields a no-op when disabled."""
    client = get_client()
    if client is None:
        return _noop_observation()
    return _observation(client, name, "span", input=input, output=output, metadata=metadata)


@contextmanager
def _noop_observation() -> Iterator[_NoopObservation]:
    yield _NoopObservation()


@contextmanager
def _observation(
    client,
    name: str,
    as_type: str,
    *,
    input: Any,
    output: Any = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[_ObservationProxy]:
    kwargs: dict[str, Any] = {"name": name, "as_type": as_type, "metadata": metadata or {}}
    if input is not None:
        kwargs["input"] = input
    if output is not None:
        kwargs["output"] = output
    try:
        observation = client.start_observation(**kwargs)
    except Exception:
        yield _NoopObservation()
        return
    proxy = _ObservationProxy(observation)
    try:
        yield proxy
    finally:
        if not proxy._ended:
            try:
                observation.end()
            except Exception:
                pass


@asynccontextmanager
async def atrace(name: str, *, input: Any = None, metadata: dict[str, Any] | None = None) -> AsyncIterator[Any]:
    """Async variant of :func:`trace` for use inside the async agent loop."""
    with trace(name, input=input, metadata=metadata) as obs:
        yield obs


@asynccontextmanager
async def aspan(name: str, *, input: Any = None, output: Any = None, metadata: dict[str, Any] | None = None) -> AsyncIterator[Any]:
    """Async variant of :func:`span` for use inside the async agent loop."""
    with span(name, input=input, output=output, metadata=metadata) as obs:
        yield obs
