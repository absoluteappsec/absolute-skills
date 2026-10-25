"""Page settledness: no in-flight requests and no DOM mutations for a quiet period."""
from __future__ import annotations

import asyncio
import time

from playwright.async_api import Page, Request

# Streams never finish; they must not block settling.
_LONG_LIVED = {"eventsource", "websocket", "media"}

MUTATION_JS = """
if (!window.__appmapSettle) {
  const s = window.__appmapSettle = {last: Date.now(), inflight: 0};
  const origFetch = window.fetch;
  if (origFetch) window.fetch = function () {
    s.inflight++;
    const done = () => { s.inflight--; s.last = Date.now(); };
    return origFetch.apply(this, arguments).then((r) => { done(); return r; },
                                                 (e) => { done(); throw e; });
  };
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function () {
    s.inflight++;
    this.addEventListener('loadend', () => { s.inflight--; s.last = Date.now(); });
    return origSend.apply(this, arguments);
  };
  const start = () => new MutationObserver(() => { s.last = Date.now(); })
    .observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
  start();
}
"""


async def install(page: Page) -> None:
    """Install the mutation observer for future navigations of ``page``'s context."""
    await page.context.add_init_script(MUTATION_JS)


async def wait_settled(page: Page, quiet_ms: int, timeout_ms: int = 15000) -> bool:
    inflight: set[Request] = set()
    last_net = time.monotonic()

    def started(req: Request) -> None:
        nonlocal last_net
        if req.resource_type not in _LONG_LIVED:
            inflight.add(req)
        last_net = time.monotonic()

    def finished(req: Request) -> None:
        nonlocal last_net
        inflight.discard(req)
        last_net = time.monotonic()

    page.on("request", started)
    page.on("requestfinished", finished)
    page.on("requestfailed", finished)
    deadline = time.monotonic() + timeout_ms / 1000
    quiet = quiet_ms / 1000
    try:
        while time.monotonic() < deadline:
            try:
                idle_ms, js_inflight = await page.evaluate(
                    "() => { %s const s = window.__appmapSettle;"
                    " return [Date.now() - s.last, s.inflight]; }" % MUTATION_JS)
            except Exception:  # navigation in progress: not settled yet
                await asyncio.sleep(0.05)
                continue
            if (not inflight and not js_inflight and idle_ms >= quiet_ms
                    and time.monotonic() - last_net >= quiet):
                return True
            await asyncio.sleep(0.05)
        return False
    finally:
        page.remove_listener("request", started)
        page.remove_listener("requestfinished", finished)
        page.remove_listener("requestfailed", finished)
