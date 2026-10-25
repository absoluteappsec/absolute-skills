"""Structural DOM fingerprints and state ids."""
from __future__ import annotations

import hashlib

SKELETON_JS = """() => {
  const SKIP = new Set(['SCRIPT', 'STYLE', 'SVG', 'NOSCRIPT', 'TEMPLATE']);
  const MAX_DEPTH = 12;
  const token = (el) => {
    const r = el.getAttribute('role');
    return el.tagName.toLowerCase() + (r ? '[' + r + ']' : '');
  };
  // Returns the token list for el's subtree; identical consecutive sibling subtrees collapse.
  const walk = (el, depth) => {
    const out = [token(el)];
    if (depth >= MAX_DEPTH || SKIP.has(el.tagName.toUpperCase())) return out;
    let prev = null;
    for (const child of el.children) {
      if (SKIP.has(child.tagName.toUpperCase())) {
        const t = [token(child)];
        if (prev !== t.join('\\u0000')) { out.push(...t); prev = t.join('\\u0000'); }
        continue;
      }
      const sub = walk(child, depth + 1);
      const key = sub.join('\\u0000');
      if (key === prev) continue;
      prev = key;
      out.push(...sub);
    }
    return out;
  };
  return document.body ? walk(document.body, 0) : [];
}"""


def dom_fingerprint(tokens: list[str]) -> str:
    return hashlib.sha1("\n".join(tokens).encode()).hexdigest()[:16]


def state_id(route_pattern: str, fingerprint: str, role: str) -> str:
    return "st-" + hashlib.sha1(f"{role}|{route_pattern}|{fingerprint}".encode()).hexdigest()[:12]
