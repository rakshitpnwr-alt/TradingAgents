"""Point-in-time archive of every payload the agents actually saw.

Why this exists: the analysts read live news, social and market data, and the
run keeps only their prose. The inputs are then gone forever, so a decision can
never be re-examined against what was actually in front of the model, and no
honest backtest of the agents' own reasoning is possible. Storing the raw
payloads costs almost nothing now and cannot be recovered later.

One JSONL file per UTC day per source, under ``archive_dir``::

    ~/.tradingagents/archive/2026-09-30/vendor.jsonl
    ~/.tradingagents/archive/2026-09-30/reddit.jsonl

Each line is one fetch: when it happened, what was asked, and the payload
verbatim. Archiving is strictly best-effort — every failure is swallowed, so a
full disk or a read-only path can degrade the archive but never break a run.
"""

from __future__ import annotations

import functools
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Payloads are normally a few KB. A pathological one is still written, but the
# record notes its size so a reader can spot it without parsing the whole line.
_MAX_REPR = 2_000_000


def _archive_dir() -> Path | None:
    """Resolve the archive root from config, or None when archiving is off."""
    try:
        from .config import get_config

        cfg = get_config()
        if not cfg.get("archive_enabled", True):
            return None
        root = cfg.get("archive_dir")
        if not root:
            root = os.path.join(os.path.expanduser("~"), ".tradingagents", "archive")
        return Path(root)
    except Exception:
        return None


def _jsonable(value):
    """Best-effort conversion to something json.dumps will accept."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    try:
        import pandas as pd

        if isinstance(value, pd.DataFrame):
            return {"__type__": "DataFrame", "csv": value.to_csv()}
        if isinstance(value, pd.Series):
            return {"__type__": "Series", "csv": value.to_csv()}
    except Exception:
        pass
    return repr(value)[:_MAX_REPR]


def record(source: str, call: str, args=(), kwargs=None, payload=None) -> None:
    """Append one fetch record. Never raises."""
    try:
        root = _archive_dir()
        if root is None:
            return
        now = datetime.now(timezone.utc)
        day_dir = root / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)

        body = _jsonable(payload)
        entry = {
            "fetched_at": now.isoformat(),
            "source": source,
            "call": call,
            "args": _jsonable(list(args)),
            "kwargs": _jsonable(kwargs or {}),
            "payload": body,
            "payload_chars": len(body) if isinstance(body, str) else None,
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with (day_dir / f"{source}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:  # pragma: no cover - archiving is never load-bearing
        logger.debug("archive: skipped %s/%s (%s)", source, call, exc)


def archived(source: str):
    """Decorate a fetcher so its return value is archived point-in-time.

    Applied to functions with several return paths (the social fetchers) and to
    the vendor router, so one decorator covers every branch. An exception
    propagates untouched after being recorded, because a failed fetch is itself
    part of what the agents saw.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            call = args[0] if (args and isinstance(args[0], str)) else fn.__name__
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                record(source, str(call), args, kwargs,
                       payload=f"<raised {type(exc).__name__}: {exc}>")
                raise
            record(source, str(call), args, kwargs, payload=result)
            return result

        return wrapper

    return decorator
