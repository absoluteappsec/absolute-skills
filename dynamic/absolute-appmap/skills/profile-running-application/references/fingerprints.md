# Technology fingerprints

How `appmap profile` detects technologies, and how to add a detector. Detection is data-driven:
the rules live in `lib/appmap/resources/fingerprints.yaml`; the engine
(`lib/appmap/profile/fingerprint.py`) runs them over evidence extracted from the run
(`lib/appmap/profile/evidence.py`). Adding a technology normally needs no code change.

## Evidence the rules see

Only what the run directory already holds — nothing is re-fetched. Each stored exchange
yields:

| Kind | Text the rule matches against |
|---|---|
| `header` | Response headers as `name: value` lines (credential values already redacted) |
| `cookie` | One `Set-Cookie` **name** per record (values are never read) |
| `html` | HTML response bodies |
| `error_page` | HTML with status ≥ 400 or a debug/error marker (Django DEBUG, Werkzeug debugger, Spring Whitelabel, tracebacks, …) |
| `script` | JavaScript bodies (bundles, vendored libraries) |
| `css` | Stylesheet bodies |
| `source_map` | A source map's `sources` paths (and the first lines of `sourcesContent`) |
| `schema` | OpenAPI/Swagger documents (JSON or YAML) and GraphQL introspection results |

Plus one `spa` record (the SPA route patterns the crawl found) and one `auth` record per role:
`role=<name> login_type=<type> auth_ok=<0|1> fields=<login body field names> token_headers=<auth/CSRF header names seen>`
— field and header **names** only.

Bodies are scanned up to 2 MiB each; a missing body means the technology may still be found
(e.g. from a header) but without a version.

## Rule format

```yaml
rules:
  - tech: Django                     # display name; rules with the same tech merge
    category: server_framework       # see the category list below
    evidence: [error_page, html]     # kinds this rule reads
    match: '<th>Django Version:</th>' # Python regex; a hit = detection
    version: '<th>Django Version:</th>\s*<td>\s*(\d+\.\d+(?:\.\d+)?)\s*</td>'  # one capture group, or null
    confidence: confirmed            # confirmed | likely
    implies: [{tech: Python, category: language}]  # or null
```

Categories: `language`, `server_framework`, `frontend_framework`, `js_library`,
`css_framework`, `web_server`, `app_server`, `cdn_proxy`, `auth`, `api_style`, `third_party`.

Confidence:

- `confirmed` only for an explicit name/version string from an authoritative source (a header,
  a library banner, a debug/error page, a schema, a generator meta tag).
- `likely` for a single indirect marker (cookie name, DOM marker, script or stylesheet URL,
  source-map path).
- Two **different** rules matching the same `tech` make it `confirmed`; one rule matching many
  exchanges stays at its own level. `implies` adds the implied technology as `likely` (Django →
  Python) unless it is detected directly.

Each detected technology keeps up to three evidence entries (`{ref, kind, snippet}`), one per
distinct rule first; snippets are at most 200 characters and re-redacted. Every version any
rule extracted is listed, sorted — conflicting versions are all kept.

## Writing a good rule

- **Anchor versions.** A version regex must be tied to a banner, header or path form
  (`/*! jQuery v(\d+…)`, `Server: nginx/(\d+…)`, `/vue@(\d+…)/`) — never a bare `v1.2` or
  `version: "2"` anywhere in a bundle; application code is full of those.
- **Exclude siblings.** Packages sharing a banner style (React's banner on scheduler/react-is;
  jQuery UI, Bootstrap Icons, `@vue/*`) must not match the parent library.
- **Stay linear-time.** Bodies can be 2 MiB and hostile: start patterns with a literal, never
  use unbounded `[^>]+` / `[^"]*` scans inside tags, and use `[ \t]*` (not `\s*`) after `^` in
  multi-line patterns.
- **Descriptive only.** A rule names what is there; it never encodes a judgment (no
  "outdated" or "vulnerable" rules, no CVE mapping).
- Cookie and DOM markers are `likely` — they can be renamed or copied.

## Adding a detector

1. Capture a real sample (headers as JSON, an HTML page, a JS banner, a source map) into
   `tests/data/fingerprints/` with the suffix that selects its kind: `.headers.json` (header +
   cookie), `.error.html`, `.html`, `.js`, `.css`, `.map`, `.schema.json` / `.schema.yaml`,
   `.auth.txt`. Strip anything sensitive from it.
2. Add the exact expected detections to `tests/data/fingerprints/expected.yaml`
   (`[technology, version or null, confidence]` per detection in that sample).
3. Add the rule(s) to `lib/appmap/resources/fingerprints.yaml` under the matching section.
4. Run `.venv/bin/pytest tests/test_profile_fingerprint.py` — the rule table is validated on
   load (known category and kinds, compilable regexes, exactly one version group) and every
   sample must produce exactly its expected set, so a too-broad rule shows up as an unexpected
   detection in another sample.
