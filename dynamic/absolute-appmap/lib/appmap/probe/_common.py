"""Helpers shared by the passive and schema probes."""
from __future__ import annotations

import functools
import time
from urllib.parse import urlsplit

from ..direct import DirectClient


def with_source(client: DirectClient, source: str, attribute: bool | str = "ok") -> DirectClient:
    """A client whose recorded exchanges carry ``source`` (origin stays ``passive``).

    ``attribute`` as in ``record_exchange``: guesses attribute only 2xx responses."""
    return DirectClient(client.gate, functools.partial(client.rec, source=source,
                                                       attribute=attribute), client.events)


def expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def base_origins(cfg) -> list[str]:
    seen: list[str] = []
    for b in cfg.base_urls:
        p = urlsplit(b)
        o = f"{p.scheme}://{p.netloc}"
        if o not in seen:
            seen.append(o)
    return seen
