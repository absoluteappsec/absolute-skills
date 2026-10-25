-- absolute-appmap SQLite schema v1.0.0
-- *_event columns hold events.jsonl ids. JSON columns are TEXT (json.dumps).
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS roles (
  name TEXT PRIMARY KEY, login_type TEXT, auth_ok INTEGER, last_login_event TEXT);

CREATE TABLE IF NOT EXISTS states (
  id TEXT PRIMARY KEY, role TEXT, url TEXT, route_pattern TEXT, dom_fingerprint TEXT,
  title TEXT, first_seen_event TEXT, screenshot TEXT, dom_snapshot TEXT,
  visits INTEGER NOT NULL DEFAULT 1);

CREATE TABLE IF NOT EXISTS locator_sets (
  id INTEGER PRIMARY KEY, primary_strategy TEXT);

CREATE TABLE IF NOT EXISTS locators (
  id INTEGER PRIMARY KEY, locator_set_id INTEGER NOT NULL REFERENCES locator_sets(id),
  strategy TEXT NOT NULL CHECK (strategy IN ('testid','role_name','label','text','css','xpath')),
  value TEXT, rank INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS transitions (
  id INTEGER PRIMARY KEY, from_state TEXT, to_state TEXT, role TEXT, action_type TEXT,
  action_label TEXT, locator_set_id INTEGER, event_id TEXT, exchange_ids TEXT);

CREATE TABLE IF NOT EXISTS routes (
  id INTEGER PRIMARY KEY, pattern TEXT NOT NULL, host TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('page','spa_route')),
  UNIQUE(host, pattern, kind));

CREATE TABLE IF NOT EXISTS endpoints (
  id INTEGER PRIMARY KEY, method TEXT NOT NULL, host TEXT NOT NULL, path_pattern TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('rest','graphql_op','websocket','sse','static','page')),
  graphql_op TEXT NOT NULL DEFAULT '', graphql_type TEXT, content_types TEXT,
  UNIQUE(method, host, path_pattern, graphql_op));

CREATE TABLE IF NOT EXISTS params (
  id INTEGER PRIMARY KEY, endpoint_id INTEGER NOT NULL REFERENCES endpoints(id),
  location TEXT NOT NULL CHECK (location IN ('path','query','body','header','cookie')),
  name TEXT NOT NULL, types TEXT, examples TEXT, required_evidence TEXT,
  UNIQUE(endpoint_id, location, name));

CREATE TABLE IF NOT EXISTS exchanges (
  id INTEGER PRIMARY KEY, endpoint_id INTEGER REFERENCES endpoints(id), role TEXT,
  origin TEXT NOT NULL CHECK (origin IN ('ui','replay','cross_role','schema','passive','bundle','approved','login')),
  event_id TEXT, method TEXT, url TEXT, req_headers TEXT, req_body TEXT, req_body_path TEXT,
  status INTEGER, resp_headers TEXT, resp_body TEXT, resp_body_path TEXT, content_type TEXT,
  duration_ms REAL, ts TEXT);

CREATE TABLE IF NOT EXISTS responses (
  id INTEGER PRIMARY KEY, endpoint_id INTEGER NOT NULL REFERENCES endpoints(id),
  role TEXT NOT NULL, statuses TEXT, content_types TEXT, body_shape TEXT,
  min_size INTEGER, max_size INTEGER, sample_exchange_ids TEXT,
  UNIQUE(endpoint_id, role));

CREATE TABLE IF NOT EXISTS forms (
  id INTEGER PRIMARY KEY, state_id TEXT, inferred INTEGER NOT NULL DEFAULT 0, method TEXT,
  action_endpoint_id INTEGER, purpose TEXT, submit_locator_set_id INTEGER, succeeded INTEGER);

CREATE TABLE IF NOT EXISTS fields (
  id INTEGER PRIMARY KEY, form_id INTEGER NOT NULL REFERENCES forms(id), name TEXT, type TEXT,
  widget TEXT, constraints TEXT, options TEXT, locator_set_id INTEGER, values_tried TEXT,
  validation_messages TEXT);

CREATE TABLE IF NOT EXISTS flows (
  id INTEGER PRIMARY KEY, name TEXT, role TEXT, description TEXT, preconditions TEXT);

CREATE TABLE IF NOT EXISTS flow_steps (
  id INTEGER PRIMARY KEY, flow_id INTEGER NOT NULL REFERENCES flows(id), ordinal INTEGER NOT NULL,
  transition_id INTEGER, exchange_id INTEGER);

CREATE TABLE IF NOT EXISTS value_links (
  id INTEGER PRIMARY KEY, producer_exchange_id INTEGER, producer_json_path TEXT,
  consumer_exchange_id INTEGER, consumer_location TEXT, value_hash TEXT);

CREATE TABLE IF NOT EXISTS role_access (
  id INTEGER PRIMARY KEY, item_kind TEXT NOT NULL CHECK (item_kind IN ('route','endpoint')),
  item_id INTEGER NOT NULL, role TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('allowed','redirect_login','forbidden','not_found','different_content','error','not_tested')),
  evidence_exchange_id INTEGER,
  UNIQUE(item_kind, item_id, role));

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY, item_kind TEXT NOT NULL, item_id INTEGER NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('ui','spa_router','bundle','openapi','graphql','sitemap','robots','well_known','source_map','replay','wordlist')),
  UNIQUE(item_kind, item_id, source));

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, role TEXT, state_id TEXT, category TEXT, label TEXT, reasons TEXT,
  action_path TEXT, predicted_request TEXT,
  decision TEXT NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending','approve','deny','skip')),
  decided_by TEXT, executed_event TEXT,
  created_by_run INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY, kind TEXT, detail TEXT, url TEXT, role TEXT, event_id TEXT);

CREATE TABLE IF NOT EXISTS external_links (
  id INTEGER PRIMARY KEY, url TEXT NOT NULL, found_on TEXT NOT NULL DEFAULT '', role TEXT,
  UNIQUE(url, found_on));

-- url / action_path are NOT NULL with defaults so UNIQUE dedups (ruling R6)
CREATE TABLE IF NOT EXISTS frontier (
  id INTEGER PRIMARY KEY, role TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('url','action')),
  url TEXT NOT NULL DEFAULT '', action_path TEXT NOT NULL DEFAULT '[]',
  depth INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','skipped','queued_approval','failed')),
  source TEXT,
  UNIQUE(role, kind, url, action_path));

-- Crawl-internal (like frontier): which anchors point at a frontier URL, so the
-- navigate transition can be recorded once the URL's state is known. A row with
-- from_state '' marks the URL as visited and holds the state it landed on.
CREATE TABLE IF NOT EXISTS frontier_links (
  id INTEGER PRIMARY KEY, role TEXT NOT NULL, url TEXT NOT NULL,
  from_state TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '',
  locators TEXT, to_state TEXT,
  UNIQUE(role, url, from_state, label));
