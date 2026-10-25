"""Detect a single-page-app framework and whether it exposes a client router."""
from __future__ import annotations

from playwright.async_api import Page

DETECT_JS = """() => {
  const w = window;
  const vueApp = () => {
    for (const el of document.querySelectorAll('body *')) if (el.__vue_app__) return el.__vue_app__;
    return null;
  };
  const sk = Object.keys(w).some(k => k.startsWith('__sveltekit'));
  const app = vueApp();
  const vueRouter = !!(app && app.config && app.config.globalProperties.$router);
  let framework = 'unknown', has_router = false;
  if (w.__NEXT_DATA__ || w.next) { framework = 'next'; has_router = true; }
  else if (w.__NUXT__ || w.$nuxt || document.getElementById('__nuxt')) {
    framework = 'nuxt'; has_router = true;
  }
  else if (sk) { framework = 'sveltekit'; has_router = true; }
  else if (app || w.Vue || w.__VUE__ || document.querySelector('[data-v-app]')) {
    framework = 'vue'; has_router = vueRouter || !!(w.$router);
  }
  else if (document.querySelector('[ng-version]') || w.getAllAngularRootElements) {
    framework = 'angular'; has_router = !!document.querySelector('router-outlet');
  }
  else if (w.__REACT_DEVTOOLS_GLOBAL_HOOK__ || document.querySelector('[data-reactroot]')
           || [...document.querySelectorAll('body *')].some(
                el => Object.keys(el).some(k => k.startsWith('__reactContainer$')
                                              || k.startsWith('__reactFiber$')))) {
    framework = 'react';  // React Router exposes no runtime hook
  }
  return {framework, has_router};
}"""


async def detect_spa(page: Page) -> dict:
    """``{framework, has_router}`` for the loaded page (never raises)."""
    try:
        res = await page.evaluate(DETECT_JS)
        return {"framework": res["framework"], "has_router": bool(res["has_router"])}
    except Exception:
        return {"framework": "unknown", "has_router": False}
