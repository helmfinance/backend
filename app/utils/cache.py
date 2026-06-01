import time
from functools import wraps
from threading import Lock
from typing import Any, Callable

from fastapi import Response


def cache_for(seconds: int):
    """FastAPI dependency. Sets Cache-Control header on the response.

    Usage:
        @app.get("/foo", dependencies=[Depends(cache_for(300))])
    """
    def dep(response: Response) -> None:
        response.headers["Cache-Control"] = f"public, max-age={seconds}"
    return dep


# ─── Server-side in-memory TTL cache ─────────────────────────────────────────
# Used to short-circuit expensive endpoints (live chain reads, multi-RPC) that
# would otherwise take 20+ seconds per request. Cache is per-worker (single
# uvicorn worker on Railway), so consistency across instances isn't a concern
# for demo scale.

_store: dict[Any, tuple[float, Any]] = {}
_lock = Lock()


def memoize(seconds: int):
    """Server-side TTL cache. Wraps any callable; key = (fn name, args, kwargs).

    Usage:
        @memoize(10)
        def expensive_query(agent_id: int) -> dict: ...
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapped(*args, **kwargs):
            # Skip non-hashable args (e.g. DB sessions) — only hash the args
            # whose types are known-stable. For simplicity, build key from
            # primitives; if a complex arg sneaks in, just bypass cache.
            try:
                key = (fn.__qualname__, args, tuple(sorted(kwargs.items())))
                hash(key)
            except TypeError:
                return fn(*args, **kwargs)

            now = time.monotonic()
            with _lock:
                hit = _store.get(key)
                if hit and hit[0] > now:
                    return hit[1]
            value = fn(*args, **kwargs)
            with _lock:
                _store[key] = (now + seconds, value)
            return value
        return wrapped
    return decorator


def memoize_invalidate(prefix: str | None = None) -> int:
    """Drop cache entries whose function qualname starts with ``prefix``.
    Pass ``None`` to clear everything. Returns count cleared."""
    with _lock:
        if prefix is None:
            n = len(_store)
            _store.clear()
            return n
        keys = [k for k in _store if k[0].startswith(prefix)]
        for k in keys:
            _store.pop(k, None)
        return len(keys)
